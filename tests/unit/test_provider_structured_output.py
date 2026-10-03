"""Provider-specific structured-output transports: OpenAI SDK, OpenRouter and local servers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import httpx2
import openai
import pytest
from pydantic import BaseModel, ValidationError

from smb_kernel.diagnostics import JsonLinesDebugTrace
from smb_kernel.errors import ModelTransportError
from smb_kernel.llm.local_structured_output import LocalLLMError, LocalStructuredOutputClient
from smb_kernel.llm.openai_structured_output import OpenAIStructuredOutputClient
from smb_kernel.llm.openrouter_structured_output import (
    OpenRouterError,
    OpenRouterStructuredOutputClient,
)
from smb_kernel.llm.structured_output import (
    StructuredOutputError,
    StructuredResponseValidationError,
    truncated,
)

OPENROUTER = "smb_kernel.llm.openrouter_structured_output"
LOCAL = "smb_kernel.llm.local_structured_output"


class Answer(BaseModel):
    text: str


class Epic(BaseModel):
    name: str
    outcome: str
    business_case: str


EPIC_JSON = json.dumps(
    {
        "name": "SMB Bundle Offer",
        "outcome": "Bundles can be ordered",
        "business_case": "Supports the stated offer",
    }
)


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _model_error(exc: BaseException) -> ModelTransportError:
    """The first classified failure along the cause chain, as public translation finds it."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ModelTransportError):
            return current
        current = current.__cause__
    raise AssertionError("No classified model failure in the cause chain.")


# --- OpenAI SDK ---------------------------------------------------------------

REQUEST = httpx2.Request("POST", "https://api.openai.test/v1/chat/completions")


def _openai(parse: MagicMock) -> OpenAIStructuredOutputClient:
    sdk = MagicMock()
    sdk.chat.completions.parse = parse
    return OpenAIStructuredOutputClient(sdk, model="gpt-test", timeout_seconds=12.5)


def _reply(parsed: object | None) -> MagicMock:
    choice = MagicMock()
    choice.message.parsed = parsed
    response = MagicMock()
    response.choices = [choice]
    return MagicMock(return_value=response)


def test_openai_parse_sends_the_configured_model_timeout_and_schema() -> None:
    parse = _reply(Answer(text="ok"))

    client = _openai(parse)
    result = client.parse(system_prompt="sys", user_prompt="user", schema_type=Answer)

    assert client.model == "gpt-test"
    assert result == Answer(text="ok")
    kwargs = parse.call_args.kwargs
    assert (kwargs["model"], kwargs["timeout"], kwargs["response_format"]) == (
        "gpt-test",
        12.5,
        Answer,
    )
    assert kwargs["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
    ]


def test_openai_images_are_sent_as_separate_data_url_parts() -> None:
    parse = _reply(Answer(text="ok"))

    _openai(parse).parse(
        system_prompt="sys",
        user_prompt="user",
        schema_type=Answer,
        images=(("image/png", b"\x89PNG"),),
    )

    content = parse.call_args.kwargs["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "user"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (openai.APITimeoutError(request=REQUEST), "timeout"),
        (
            openai.RateLimitError(
                "slow down", response=httpx2.Response(429, request=REQUEST), body=None
            ),
            "rate_limit",
        ),
        (
            openai.AuthenticationError(
                "bad key", response=httpx2.Response(401, request=REQUEST), body=None
            ),
            "authentication",
        ),
        (
            openai.PermissionDeniedError(
                "bad key", response=httpx2.Response(403, request=REQUEST), body=None
            ),
            "authentication",
        ),
        (
            openai.BadRequestError(
                "bad model", response=httpx2.Response(400, request=REQUEST), body=None
            ),
            "configuration",
        ),
        (
            openai.NotFoundError(
                "bad model", response=httpx2.Response(404, request=REQUEST), body=None
            ),
            "configuration",
        ),
        (openai.APIConnectionError(request=REQUEST), "unavailable"),
    ],
)
def test_openai_sdk_failures_become_public_error_kinds_without_provider_text(
    error: openai.OpenAIError, kind: str
) -> None:
    with pytest.raises(StructuredOutputError) as raised:
        _openai(MagicMock(side_effect=error)).parse(
            system_prompt="sys", user_prompt="user", schema_type=Answer
        )

    classified = _model_error(raised.value)
    assert classified.kind == kind
    assert "slow down" not in str(classified) and "bad key" not in str(classified)
    assert classified.__cause__ is error


def test_openai_length_finish_is_marked_as_truncated() -> None:
    error = openai.LengthFinishReasonError(completion=MagicMock())
    with pytest.raises(StructuredOutputError, match="truncated") as raised:
        _openai(MagicMock(side_effect=error)).parse(
            system_prompt="sys", user_prompt="user", schema_type=Answer
        )

    assert _model_error(raised.value).kind == "invalid_output"
    assert truncated(raised.value)


def test_openai_content_filter_is_invalid_output_not_truncation() -> None:
    error = openai.ContentFilterFinishReasonError()
    with pytest.raises(StructuredOutputError, match="filtered") as raised:
        _openai(MagicMock(side_effect=error)).parse(
            system_prompt="sys", user_prompt="user", schema_type=Answer
        )

    assert _model_error(raised.value).kind == "invalid_output"
    assert not truncated(raised.value)


def test_openai_schema_mismatch_becomes_safe_validation_feedback() -> None:
    with pytest.raises(ValidationError) as invalid:
        Answer.model_validate({"text": 7})

    with pytest.raises(StructuredResponseValidationError) as raised:
        _openai(MagicMock(side_effect=invalid.value)).parse(
            system_prompt="sys", user_prompt="user", schema_type=Answer
        )

    assert str(raised.value) == "Response validation failed (string_type)."
    # Public translation finds invalid_output; the pydantic error stays beneath it for logs.
    classified = _model_error(raised.value)
    assert classified.kind == "invalid_output"
    assert classified.__cause__ is invalid.value


@pytest.mark.parametrize("parsed", [None])
def test_openai_empty_or_refused_output_is_invalid_output(parsed: object | None) -> None:
    with pytest.raises(StructuredOutputError) as raised:
        _openai(_reply(parsed)).parse(system_prompt="sys", user_prompt="user", schema_type=Answer)

    assert _model_error(raised.value).kind == "invalid_output"


def test_openai_no_choices_is_invalid_output() -> None:
    response = MagicMock()
    response.choices = []

    with pytest.raises(StructuredOutputError) as raised:
        _openai(MagicMock(return_value=response)).parse(
            system_prompt="sys", user_prompt="user", schema_type=Answer
        )

    assert _model_error(raised.value).kind == "invalid_output"


# --- OpenRouter -------------------------------------------------------------


def _openrouter(*, trace: JsonLinesDebugTrace | None = None) -> OpenRouterStructuredOutputClient:
    return OpenRouterStructuredOutputClient(
        base_url="https://router.example.test/api/v1",
        http_client=httpx.Client(),
        api_key="sk-or-private",
        model="google/gemma-4-31b-it:free",
        timeout_seconds=120,
        max_output_tokens=8192,
        data_collection="deny",
        debug_trace=trace,
    )


def _response(
    payload: object,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.text = json.dumps(payload)
    response.json.return_value = payload
    return response


def _failing(status_code: int, headers: dict[str, str] | None = None) -> MagicMock:
    request = httpx.Request("POST", "https://router.example.test/api/v1/chat/completions")
    failed = httpx.Response(status_code, headers=headers, request=request)
    response = _response({}, status_code=status_code, headers=headers)
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "rate limited", request=request, response=failed
    )
    return response


def _parse_openrouter(client: OpenRouterStructuredOutputClient | None = None) -> Epic:
    return (client or _openrouter()).parse(
        system_prompt="System rules",
        user_prompt="Requirement text",
        schema_type=Epic,
    )


def test_openrouter_client_sends_authenticated_privacy_first_json_mode_with_images() -> None:
    response = _response(
        {"choices": [{"finish_reason": "stop", "message": {"content": EPIC_JSON}}]}
    )

    with patch(f"{OPENROUTER}.httpx.Client.post", return_value=response) as post:
        client = _openrouter()
        result = client.parse(
            system_prompt="System rules",
            user_prompt="Requirement text",
            schema_type=Epic,
            images=(("image/png", b"image-bytes"),),
        )

    assert client.model == "google/gemma-4-31b-it:free"
    assert result.name == "SMB Bundle Offer"
    assert post.call_args.args[0] == "https://router.example.test/api/v1/chat/completions"
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer sk-or-private"
    body = post.call_args.kwargs["json"]
    assert body["model"] == "google/gemma-4-31b-it:free"
    assert body["response_format"] == {"type": "json_object"}
    assert body["provider"] == {"require_parameters": True, "data_collection": "deny"}
    assert body["max_tokens"] == 8192
    assert "JSON Schema" in body["messages"][0]["content"]
    assert '"business_case"' in body["messages"][0]["content"]
    image_url = body["messages"][1]["content"][1]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")
    assert "reasoning" not in body


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "invalid response object"),
        ({}, "no choices"),
        ({"choices": [None]}, "invalid choice"),
        (
            {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
            "truncated",
        ),
        (
            {"choices": [{"finish_reason": "content_filter", "message": {"content": "{}"}}]},
            "did not complete",
        ),
        ({"choices": [{"message": None}]}, "no assistant message"),
        ({"choices": [{"message": {"refusal": "No"}}]}, "refused"),
        ({"choices": [{"message": {"content": " "}}]}, "empty completion"),
        ({"choices": [{"message": {"content": "not-json"}}]}, "did not match"),
        ({"choices": [{"message": {"content": "{}"}}]}, "did not match"),
        ({"error": {"code": 402, "message": "no credits"}}, "upstream provider failure"),
    ],
)
def test_openrouter_client_rejects_unusable_responses(payload: object, message: str) -> None:
    with (
        patch(f"{OPENROUTER}.httpx.Client.post", return_value=_response(payload)),
        pytest.raises(OpenRouterError, match=message) as raised,
    ):
        _parse_openrouter()
    # Only a cut-off answer is marked as one, so a reader can ask for less.
    assert truncated(raised.value) is (message == "truncated")


def test_openrouter_embedded_provider_error_keeps_its_safe_kind() -> None:
    payload = {"error": {"code": 402, "message": "no credits"}}
    with (
        patch(f"{OPENROUTER}.httpx.Client.post", return_value=_response(payload)),
        pytest.raises(OpenRouterError) as raised,
    ):
        _parse_openrouter()
    assert _model_error(raised.value).kind == "payment"


@pytest.mark.parametrize("status_code", [401, 500, 503])
def test_openrouter_client_maps_http_failures(status_code: int) -> None:
    with (
        patch(f"{OPENROUTER}.httpx.Client.post", return_value=_failing(status_code)),
        patch(f"{OPENROUTER}.time.sleep"),
        pytest.raises(OpenRouterError, match="request failed"),
    ):
        _parse_openrouter()


def test_openrouter_client_retries_server_errors_with_backoff_then_gives_up() -> None:
    with (
        patch(f"{OPENROUTER}.httpx.Client.post", return_value=_failing(503)) as post,
        patch(f"{OPENROUTER}.time.sleep") as sleep,
        pytest.raises(OpenRouterError, match="request failed"),
    ):
        _parse_openrouter()

    assert post.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 2.0]


def test_openrouter_client_does_not_wait_for_a_long_server_retry_after() -> None:
    with (
        patch(
            f"{OPENROUTER}.httpx.Client.post",
            return_value=_failing(503, {"Retry-After": "60"}),
        ) as post,
        patch(f"{OPENROUTER}.time.sleep") as sleep,
        pytest.raises(OpenRouterError, match="request failed"),
    ):
        _parse_openrouter()

    post.assert_called_once()
    sleep.assert_not_called()


def test_openrouter_client_retries_short_rate_limit_then_succeeds() -> None:
    rate_limited = _response({}, status_code=429, headers={"Retry-After": "2"})
    completion = json.dumps(
        {"name": "Offer", "outcome": "Outcome", "business_case": "Business case"}
    )
    succeeded = _response({"choices": [{"message": {"content": completion}}]})

    with (
        patch(
            f"{OPENROUTER}.httpx.Client.post",
            side_effect=[rate_limited, succeeded],
        ) as post,
        patch(f"{OPENROUTER}.time.sleep") as sleep,
    ):
        result = _parse_openrouter()

    assert result.name == "Offer"
    assert post.call_count == 2
    sleep.assert_called_once_with(2.0)


def test_openrouter_client_reports_long_rate_limit_without_waiting() -> None:
    with (
        patch(
            f"{OPENROUTER}.httpx.Client.post",
            return_value=_failing(429, {"Retry-After": "120"}),
        ) as post,
        patch(f"{OPENROUTER}.time.sleep") as sleep,
        pytest.raises(OpenRouterError, match="Retry after 120 seconds"),
    ):
        _parse_openrouter()

    post.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize("headers", [None, {"Retry-After": "tomorrow"}])
def test_openrouter_client_does_not_multiply_rate_limit_without_retry_after(
    headers: dict[str, str] | None,
) -> None:
    with (
        patch(f"{OPENROUTER}.httpx.Client.post", return_value=_failing(429, headers)) as post,
        patch(f"{OPENROUTER}.time.sleep") as sleep,
        pytest.raises(OpenRouterError, match="Retry later"),
    ):
        _parse_openrouter()

    post.assert_called_once()
    sleep.assert_not_called()


def test_openrouter_client_retries_transport_errors_then_reports_them() -> None:
    with (
        patch(
            f"{OPENROUTER}.httpx.Client.post",
            side_effect=httpx.ConnectError("router is offline"),
        ) as post,
        patch(f"{OPENROUTER}.time.sleep") as sleep,
        pytest.raises(OpenRouterError, match="router is offline"),
    ):
        _parse_openrouter()

    assert post.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 2.0]


def test_openrouter_client_never_retries_a_timeout() -> None:
    with (
        patch(
            f"{OPENROUTER}.httpx.Client.post",
            side_effect=httpx.ReadTimeout("slow"),
        ) as post,
        patch(f"{OPENROUTER}.time.sleep") as sleep,
        pytest.raises(OpenRouterError, match="request failed"),
    ):
        _parse_openrouter()

    post.assert_called_once()
    sleep.assert_not_called()


def test_openrouter_client_rejects_non_json_http_response() -> None:
    response = _response({})
    response.json.side_effect = ValueError("not json")

    with (
        patch(f"{OPENROUTER}.httpx.Client.post", return_value=response),
        pytest.raises(OpenRouterError, match="non-JSON"),
    ):
        _parse_openrouter()


def test_openrouter_trace_redacts_authorization_and_image_bytes(tmp_path: Path) -> None:
    path = tmp_path / "debug.log"
    trace = JsonLinesDebugTrace(str(path))
    completion = json.dumps(
        {"name": "Offer", "outcome": "Outcome", "business_case": "Business case"}
    )
    response = _response({"choices": [{"message": {"content": completion}}]})

    with patch(f"{OPENROUTER}.httpx.Client.post", return_value=response):
        _openrouter(trace=trace).parse(
            system_prompt="System rules",
            user_prompt="Requirement text",
            schema_type=Epic,
            images=(("image/png", b"image-bytes"),),
        )
    trace.record("secret.check", openrouter_api_key="sk-or-private")
    trace.close()

    events = _events(path)
    request_body = next(
        item["details"]["request_body"] for item in events if item["event"] == "openrouter.request"
    )
    assert request_body["messages"][1]["content"][1]["image_url"]["url"] == (
        "[REDACTED: image/png data URL]"
    )
    secret = next(item for item in events if item["event"] == "secret.check")
    assert secret["details"]["openrouter_api_key"] == "[REDACTED]"
    assert "sk-or-private" not in path.read_text(encoding="utf-8")
    parsed = next(item for item in events if item["event"] == "openrouter.parsed")
    assert parsed["details"]["parsed"]["name"] == "Offer"


# --- Local OpenAI-compatible server -------------------------------------------


def _local_response(payload: object) -> MagicMock:
    response = MagicMock()
    response.json.return_value = payload
    return response


def _local(
    *, reasoning_effort: str | None = None, trace: JsonLinesDebugTrace | None = None
) -> LocalStructuredOutputClient:
    return LocalStructuredOutputClient(
        base_url="http://127.0.0.1:1234/v1",
        http_client=httpx.Client(),
        model="local-model",
        timeout_seconds=90,
        reasoning_effort=reasoning_effort,
        debug_trace=trace,
    )


def test_local_client_sends_json_schema_and_parses_content() -> None:
    with patch(
        f"{LOCAL}.httpx.Client.post",
        return_value=_local_response({"choices": [{"message": {"content": EPIC_JSON}}]}),
    ) as post:
        client = LocalStructuredOutputClient(
            base_url="http://127.0.0.1:1234/v1/",
            http_client=httpx.Client(),
            model="local-model",
            timeout_seconds=90,
            reasoning_effort=None,
        )
        result = client.parse(
            system_prompt="system",
            user_prompt="user",
            schema_type=Epic,
        )

    assert client.model == "local-model"
    assert client.input_budget_tokens == 8192 - 4096
    assert result.name == "SMB Bundle Offer"
    call = post.call_args
    assert call.args[0] == "http://127.0.0.1:1234/v1/chat/completions"
    assert call.kwargs["timeout"] == 90
    assert call.kwargs["json"]["model"] == "local-model"
    assert call.kwargs["json"]["response_format"]["json_schema"]["schema"]["title"] == "Epic"
    assert call.kwargs["json"]["max_tokens"] == 4096
    assert "reasoning_effort" not in call.kwargs["json"]


def test_local_client_sends_images_as_data_url_parts() -> None:
    with patch(
        f"{LOCAL}.httpx.Client.post",
        return_value=_local_response({"choices": [{"message": {"content": EPIC_JSON}}]}),
    ) as post:
        _local().parse(
            system_prompt="system",
            user_prompt="user",
            schema_type=Epic,
            images=(("image/png", b"safe-image-bytes"),),
        )

    content = post.call_args.kwargs["json"]["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "user"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_local_client_disables_reasoning_and_accepts_ollama_reasoning_fallback() -> None:
    with patch(
        f"{LOCAL}.httpx.Client.post",
        return_value=_local_response(
            {"choices": [{"message": {"content": "", "reasoning": EPIC_JSON}}]}
        ),
    ) as post:
        client = LocalStructuredOutputClient(
            base_url="http://127.0.0.1:11434/v1",
            http_client=httpx.Client(),
            model="qwen3-vl:8b",
            timeout_seconds=90,
            reasoning_effort="none",
        )
        result = client.parse(
            system_prompt="system",
            user_prompt="user",
            schema_type=Epic,
        )

    assert result.name == "SMB Bundle Offer"
    assert post.call_args.kwargs["json"]["reasoning_effort"] == "none"


def test_local_client_ignores_reasoning_output_unless_reasoning_is_disabled() -> None:
    with (
        patch(
            f"{LOCAL}.httpx.Client.post",
            return_value=_local_response(
                {"choices": [{"message": {"content": "", "reasoning": EPIC_JSON}}]}
            ),
        ),
        pytest.raises(LocalLLMError, match="empty completion"),
    ):
        _local(reasoning_effort="low").parse(
            system_prompt="system", user_prompt="user", schema_type=Epic
        )


def test_local_client_reports_output_token_truncation_explicitly() -> None:
    with patch(
        f"{LOCAL}.httpx.Client.post",
        return_value=_local_response(
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": '{"name":"unfinished'},
                    }
                ]
            }
        ),
    ):
        client = LocalStructuredOutputClient(
            base_url="http://127.0.0.1:11434/v1",
            http_client=httpx.Client(),
            model="qwen3-vl:8b",
            timeout_seconds=90,
            reasoning_effort="none",
        )

        with pytest.raises(LocalLLMError, match="output was truncated") as raised:
            client.parse(
                system_prompt="system",
                user_prompt="user",
                schema_type=Epic,
            )
    # A reader that can ask for less tells a cut-off answer apart.
    assert truncated(raised.value)


def test_local_client_rejects_oversized_context_without_dropping_human_input() -> None:
    client = LocalStructuredOutputClient(
        base_url="http://127.0.0.1:11434/v1",
        http_client=httpx.Client(),
        model="qwen3-vl:8b",
        timeout_seconds=90,
        reasoning_effort="none",
        context_window_tokens=1024,
        max_output_tokens=256,
    )

    with pytest.raises(LocalLLMError, match="Human clarifications were not discarded"):
        client.parse(
            system_prompt="system",
            user_prompt="human decision " * 300,
            schema_type=Epic,
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "no choices"),
        ({"choices": []}, "no choices"),
        ({"choices": [{}]}, "no assistant message"),
        ({"choices": [{"message": {"content": "  "}}]}, "empty completion"),
        ({"choices": [{"message": {"content": "not-json"}}]}, "did not match"),
        ([], "invalid response object"),
        ({"choices": [None]}, "invalid choice"),
        (
            {"choices": [{"finish_reason": "tool_calls", "message": {"content": "{}"}}]},
            "did not complete",
        ),
        ({"choices": [{"message": {"refusal": "No", "content": EPIC_JSON}}]}, "refused"),
        ({"choices": [{"finish_reason": "error", "message": {}}]}, "provider failure"),
    ],
)
def test_local_client_rejects_malformed_responses(payload: object, message: str) -> None:
    with patch(f"{LOCAL}.httpx.Client.post", return_value=_local_response(payload)):
        with pytest.raises(LocalLLMError, match=message):
            _local().parse(system_prompt="system", user_prompt="user", schema_type=Epic)


def test_local_client_maps_connection_failures() -> None:
    with patch(
        f"{LOCAL}.httpx.Client.post",
        side_effect=httpx.ConnectError("server is offline"),
    ):
        with pytest.raises(LocalLLMError, match="server is offline"):
            _local().parse(system_prompt="system", user_prompt="user", schema_type=Epic)


def test_local_client_rejects_non_json_http_response() -> None:
    response = MagicMock()
    response.json.side_effect = ValueError("not json")
    with patch(f"{LOCAL}.httpx.Client.post", return_value=response):
        with pytest.raises(LocalLLMError, match="non-JSON"):
            _local().parse(system_prompt="system", user_prompt="user", schema_type=Epic)


def test_local_schema_failure_is_traced_and_keeps_safe_feedback(tmp_path: Path) -> None:
    path = tmp_path / "debug.log"
    trace = JsonLinesDebugTrace(str(path))
    with (
        patch(
            f"{LOCAL}.httpx.Client.post",
            return_value=_local_response({"choices": [{"message": {"content": "{}"}}]}),
        ),
        pytest.raises(LocalLLMError, match="did not match Epic") as raised,
    ):
        _local(trace=trace).parse(system_prompt="system", user_prompt="user", schema_type=Epic)
    trace.close()

    assert isinstance(raised.value.__cause__, StructuredResponseValidationError)
    assert _model_error(raised.value).kind == "invalid_output"
    failure = next(
        item for item in _events(path) if item["event"] == "local_llm.schema_validation_failed"
    )
    assert failure["details"]["completion_source"] == "content"
