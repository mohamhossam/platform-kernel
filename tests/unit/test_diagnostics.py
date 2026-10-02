"""Opt-in single-file diagnostic trace."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from pydantic import BaseModel

from smb_kernel.diagnostics import JsonLinesDebugTrace, NullDebugTrace, build_debug_trace
from smb_kernel.llm.local_structured_output import LocalStructuredOutputClient


class Epic(BaseModel):
    name: str
    outcome: str
    business_case: str


class Colour(Enum):
    RED = "red"


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _details_by_event(path: Path, event: str) -> dict[str, Any]:
    return next(item["details"] for item in _events(path) if item["event"] == event)


def test_null_trace_performs_no_file_io(tmp_path: Path) -> None:
    trace = NullDebugTrace()

    trace.record("ignored", path=str(tmp_path / "should-not-exist.log"))
    trace.close()

    assert trace.enabled is False
    assert trace.path is None
    assert list(tmp_path.iterdir()) == []


def test_build_debug_trace_creates_a_file_only_when_enabled(tmp_path: Path) -> None:
    disabled = build_debug_trace(enabled=False, path=str(tmp_path / "off" / "trace.jsonl"))
    assert isinstance(disabled, NullDebugTrace)
    assert list(tmp_path.iterdir()) == []

    enabled = build_debug_trace(enabled=True, path=str(tmp_path / "on" / "trace.jsonl"))
    try:
        assert isinstance(enabled, JsonLinesDebugTrace)
        assert enabled.enabled is True
        assert enabled.path == str((tmp_path / "on" / "trace.jsonl").resolve())
    finally:
        enabled.close()
    started = _details_by_event(tmp_path / "on" / "trace.jsonl", "trace.session_started")
    assert started["trace_path"] == enabled.path


def test_json_lines_trace_redacts_secrets_binary_images_and_reasoning(tmp_path: Path) -> None:
    path = tmp_path / "debug.log"
    trace = JsonLinesDebugTrace(str(path))

    trace.record(
        "redaction.check",
        authorization="Bearer private",
        openai_api_key="sk-private",
        max_tokens=8192,
        binary=b"image-bytes",
        image_url="data:image/png;base64,cHJpdmF0ZQ==",
        reasoning="hidden chain",
        prompt="Visible requirement text",
    )
    logging.getLogger("smb_kernel.test").warning("Worker event %s", "job-1")
    trace.close()

    details = _details_by_event(path, "redaction.check")
    assert details["authorization"] == "[REDACTED]"
    assert details["openai_api_key"] == "[REDACTED]"
    assert details["max_tokens"] == 8192
    assert str(details["binary"]).startswith("[REDACTED: 11 binary bytes;")
    assert details["image_url"] == "[REDACTED: image/png data URL]"
    assert details["reasoning"] == "[REDACTED: hidden reasoning]"
    assert details["prompt"] == "Visible requirement text"
    python_log = _details_by_event(path, "python.log")
    assert python_log["message"] == "Worker event job-1"


def test_configured_secrets_and_url_credentials_are_scrubbed_everywhere(tmp_path: Path) -> None:
    path = tmp_path / "debug.log"
    trace = JsonLinesDebugTrace(str(path), ("short", "", "short-but-longer"))

    trace.record(
        "scrub.check",
        note="value short-but-longer and short",
        database="postgresql://user:pw@db.example/app",
        url="https://api.example/v1?api_key=abc123&page=2",
    )
    trace.close()

    text = path.read_text(encoding="utf-8")
    assert "short" not in text.replace("[REDACTED]", "")
    details = _details_by_event(path, "scrub.check")
    assert details["note"] == "value [REDACTED] and [REDACTED]"
    assert details["database"] == "postgresql://[REDACTED]@db.example/app"
    assert details["url"] == "https://api.example/v1?api_key=[REDACTED]&page=2"


def test_structured_values_are_serialized_safely(tmp_path: Path) -> None:
    path = tmp_path / "debug.log"
    trace = JsonLinesDebugTrace(str(path))
    moment = datetime(2026, 1, 1, tzinfo=UTC)

    trace.record(
        "values.check",
        model=Epic(name="Offer", outcome="Outcome", business_case="Case"),
        colour=Colour.RED,
        moment=moment,
        nested={"db_password": "pw", "items": [1, "two", None]},
        other=object,
    )
    trace.close()

    details = _details_by_event(path, "values.check")
    assert details["model"] == {"name": "Offer", "outcome": "Outcome", "business_case": "Case"}
    assert details["colour"] == "red"
    assert details["moment"] == moment.isoformat()
    assert details["nested"] == {"db_password": "[REDACTED]", "items": [1, "two", None]}
    assert details["other"] == repr(object)


def test_logged_exceptions_carry_their_chain_and_close_detaches_logging(
    tmp_path: Path,
) -> None:
    path = tmp_path / "debug.log"
    trace = JsonLinesDebugTrace(str(path))
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("smb_kernel.test").exception("Failed")
    trace.close()
    trace.close()
    logging.getLogger("smb_kernel.test").warning("After close")

    logged = [item["details"] for item in _events(path) if item["event"] == "python.log"]
    assert len(logged) == 1
    assert logged[0]["level"] == "ERROR"
    assert "ValueError: boom" in logged[0]["exception_chain"]


def test_local_client_traces_request_raw_response_and_parsed_model(tmp_path: Path) -> None:
    path = tmp_path / "debug.log"
    trace = JsonLinesDebugTrace(str(path))
    completion = json.dumps(
        {
            "name": "SMB Bundle Offer",
            "outcome": "Bundles can be ordered",
            "business_case": "Supports the stated offer",
        }
    )
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "choices": [{"message": {"content": completion, "reasoning": "hidden chain"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30},
    }
    client = LocalStructuredOutputClient(
        base_url="http://127.0.0.1:11434/v1",
        http_client=httpx.Client(),
        model="qwen3-vl:8b",
        timeout_seconds=90,
        reasoning_effort="none",
        debug_trace=trace,
    )

    with patch(
        "smb_kernel.llm.local_structured_output.httpx.Client.post",
        return_value=response,
    ):
        result = client.parse(
            system_prompt="System rules",
            user_prompt="Requirement text",
            schema_type=Epic,
        )
    trace.close()

    assert result.name == "SMB Bundle Offer"
    request = _details_by_event(path, "local_llm.request")
    request_body = request["request_body"]
    assert isinstance(request_body, dict)
    assert request_body["messages"][0]["content"] == "System rules"
    assert request_body["messages"][1]["content"] == "Requirement text"
    raw_response = _details_by_event(path, "local_llm.response")
    payload = raw_response["payload"]
    assert isinstance(payload, dict)
    assert payload["choices"][0]["message"]["content"] == completion
    assert payload["choices"][0]["message"]["reasoning"] == "[REDACTED: hidden reasoning]"
    parsed = _details_by_event(path, "local_llm.parsed")
    assert parsed["parsed"]["name"] == "SMB Bundle Offer"
