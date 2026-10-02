"""Shared chat-completions and embedding transports driven by configured profiles."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from smb_kernel.diagnostics import JsonLinesDebugTrace, NullDebugTrace
from smb_kernel.errors import KnowledgeGenerationError, ModelTransportError
from smb_kernel.llm.compatible_transport import (
    CompatibleOutputError,
    CompatibleStructuredOutputClient,
    ConfiguredKnowledgeEmbedding,
    GoogleEmbeddingTokenCounter,
)
from smb_kernel.llm.profiles import EmbeddingProfile, ModelProfile
from smb_kernel.llm.structured_output import StructuredResponseValidationError, truncated

SLEEP = "smb_kernel.llm.compatible_transport.time.sleep"

# The shipped example profiles of the requirement portal's config/llm.yaml.
SHIPPED_PROFILES: dict[str, dict[str, Any]] = {
    "gemini-flash-lite": {
        "provider": "Google Gemini",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-3.1-flash-lite",
        "api_key_env": "GEMINI_API_KEY",
        "images": True,
        "structured_output": "json_schema",
        "context_tokens": 1048576,
        "output_tokens": 8192,
        "reasoning_effort": "minimal",
        "timeout_seconds": 120,
    },
    "openai": {
        "provider": "OpenAI",
        "endpoint": "https://api.openai.com/v1",
        "model": "gpt-4.1-mini",
        "api_key_env": "OPENAI_API_KEY",
        "images": True,
        "context_tokens": 1047576,
    },
    "openrouter": {
        "provider": "OpenRouter",
        "endpoint": "https://openrouter.ai/api/v1",
        "model": "google/gemini-3.1-flash-lite",
        "api_key_env": "OPENROUTER_API_KEY",
        "images": True,
        "structured_output": "json_object",
        "context_tokens": 1048576,
        "request_options": {
            "provider": {
                "require_parameters": True,
                "data_collection": "deny",
                "allow_fallbacks": False,
            }
        },
    },
    "ollama": {
        "provider": "Ollama",
        "endpoint": "http://127.0.0.1:11434/v1",
        "model": "smb-qwen3-vl:8b-16k",
        "images": False,
        "context_tokens": 16384,
        "output_tokens": 8192,
        "reasoning_effort": "none",
        "ollama_reasoning_fallback": True,
        "timeout_seconds": 300,
    },
    "compatible-template": {
        "provider": "Compatible endpoint",
        "endpoint": "https://your-api.example/v1",
        "model": "your-model-id",
        "api_key_env": "COMPATIBLE_API_KEY",
        "images": False,
        "context_tokens": 32768,
    },
}


class Result(BaseModel):
    answer: str


def response(content: str = '{"answer":"ok"}', **kwargs: object) -> dict[str, object]:
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": content}, **kwargs}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _profile(**updates: Any) -> ModelProfile:
    return ModelProfile(provider="Test", endpoint="http://localhost/v1", model="test", **updates)


def _returning(payload: object) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, json=payload))


def _parse(profile: ModelProfile, transport: httpx.MockTransport) -> Result:
    with httpx.Client(transport=transport) as http:
        return CompatibleStructuredOutputClient(profile, http, NullDebugTrace()).parse(
            system_prompt="app", user_prompt="source", schema_type=Result
        )


@pytest.mark.parametrize("name", list(SHIPPED_PROFILES))
def test_shared_profile_transport_contract(name: str) -> None:
    p = ModelProfile.model_validate(SHIPPED_PROFILES[name]).model_copy(
        update={"api_key": "private-key"}
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = CompatibleStructuredOutputClient(p, http, NullDebugTrace())
        assert client.model == p.model
        assert client.configuration_fingerprint == p.fingerprint
        assert (
            client.parse(
                system_prompt="Application instructions",
                user_prompt="Untrusted document",
                schema_type=Result,
            ).answer
            == "ok"
        )
    sent = json.loads(requests[0].content)
    assert sent["model"] == p.model and sent[p.output_parameter] == p.output_tokens
    assert sent["messages"][0]["content"].startswith("Application instructions")
    assert sent["messages"][1]["content"] == "Untrusted document"
    assert sent["response_format"]["type"] == p.structured_output
    assert requests[0].headers["authorization"] == "Bearer private-key"
    assert requests[0].url.path.endswith("/chat/completions")
    if name == "openrouter":
        assert sent["provider"]["allow_fallbacks"] is False
        assert "OUTPUT CONTRACT" in sent["messages"][0]["content"]
    if p.reasoning_effort is not None:
        assert sent["reasoning_effort"] == p.reasoning_effort


def test_unauthenticated_profile_sends_no_authorization_header() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response())

    assert _parse(_profile(), httpx.MockTransport(handler)).answer == "ok"
    assert "authorization" not in requests[0].headers
    sent = json.loads(requests[0].content)
    assert sent["response_format"]["json_schema"]["name"] == "Result"
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert "reasoning_effort" not in sent


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [None]},
        response(""),
        response("not json"),
        response('{"answer":7}'),
        response('{"answer":"ok"}', finish_reason="length"),
        {"choices": [{"message": {"refusal": "unsafe", "content": '{"answer":"ok"}'}}]},
    ],
)
def test_malformed_refused_and_truncated_completions(payload: object) -> None:
    with pytest.raises(ModelTransportError) as failure:
        _parse(_profile(), _returning(payload))
    assert failure.value.kind == "invalid_output"
    # Only a cut-off answer is marked as one, so a reader can ask for less.
    assert truncated(failure.value) is (
        payload == response('{"answer":"ok"}', finish_reason="length")
    )


@pytest.mark.parametrize("reason", ["content_filter", "tool_calls", "function_call"])
def test_incomplete_finish_reasons_are_invalid_output(reason: str) -> None:
    with pytest.raises(ModelTransportError) as failure:
        _parse(_profile(), _returning(response(finish_reason=reason)))
    assert failure.value.kind == "invalid_output"
    assert not truncated(failure.value)


def test_non_json_body_is_invalid_output() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"<html>"))
    with pytest.raises(ModelTransportError) as failure:
        _parse(_profile(), transport)
    assert failure.value.kind == "invalid_output"


def test_schema_mismatch_keeps_safe_correction_feedback() -> None:
    with pytest.raises(ModelTransportError) as failure:
        _parse(_profile(), _returning(response('{"answer":7}')))
    assert isinstance(failure.value.__cause__, StructuredResponseValidationError)
    assert "7" not in str(failure.value.__cause__)


@pytest.mark.parametrize(
    ("code", "kind"), [(429, "rate_limit"), (402, "payment"), (401, "authentication")]
)
def test_embedded_provider_error_is_classified_not_parsed(
    code: int, kind: str, tmp_path: Path
) -> None:
    path = tmp_path / "trace.jsonl"
    trace = JsonLinesDebugTrace(str(path))
    payload = {
        "choices": [
            {
                "finish_reason": "error",
                "message": {"content": '{"answer":"partial'},
                "error": {"code": code, "message": "provider text private-test-key"},
            }
        ]
    }
    with httpx.Client(transport=_returning(payload)) as http:
        with pytest.raises(ModelTransportError) as failure:
            CompatibleStructuredOutputClient(_profile(), http, trace).parse(
                system_prompt="app", user_prompt="source", schema_type=Result
            )
    trace.close()
    assert failure.value.kind == kind
    assert "private-test-key" not in str(failure.value)
    assert "model.provider_failed" in path.read_text(encoding="utf-8")


def test_images_rejected_before_request_and_sent_when_supported() -> None:
    p = _profile()
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = CompatibleStructuredOutputClient(p, http, NullDebugTrace())
        with pytest.raises(CompatibleOutputError, match="image"):
            client.parse(
                system_prompt="app",
                user_prompt="source",
                schema_type=Result,
                images=(("image/png", b"sanitized-image"),),
            )
        assert not sent
        CompatibleStructuredOutputClient(
            p.model_copy(update={"images": True}), http, NullDebugTrace()
        ).parse(
            system_prompt="app",
            user_prompt="source",
            schema_type=Result,
            images=(("image/png", b"sanitized-image"),),
        )
    assert sent[0]["messages"][1]["content"][0] == {"type": "text", "text": "source"}
    assert sent[0]["messages"][1]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


def test_timeout_is_not_retried() -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("secret provider details", request=request)

    with pytest.raises(ModelTransportError) as failure:
        _parse(_profile(), httpx.MockTransport(handler))
    assert count == 1 and failure.value.kind == "timeout"
    assert "secret" not in str(failure.value)


def test_connection_failure_is_unavailable_without_provider_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret provider details", request=request)

    with pytest.raises(ModelTransportError) as failure:
        _parse(_profile(), httpx.MockTransport(handler))
    assert failure.value.kind == "unavailable"
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize(
    "status,attempts,kind",
    [
        (429, 3, "rate_limit"),
        (503, 3, "unavailable"),
        (401, 1, "authentication"),
        (400, 1, "configuration"),
    ],
)
def test_bounded_retries_and_safe_error_kinds(
    status: int, attempts: int, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    delays: list[float] = []
    monkeypatch.setattr(SLEEP, delays.append)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, text="private-key", headers={"retry-after": "999"})

    with pytest.raises(ModelTransportError) as failure:
        _parse(_profile(), httpx.MockTransport(handler))
    assert len(requests) == attempts and failure.value.kind == kind
    assert all(delay <= 10 for delay in delays)
    assert "private-key" not in str(failure.value)


def test_retry_uses_backoff_when_retry_after_is_not_a_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    monkeypatch.setattr(SLEEP, delays.append)
    replies = iter(
        [
            httpx.Response(503, headers={"retry-after": "soon"}),
            httpx.Response(502),
            httpx.Response(200, json=response()),
        ]
    )

    assert _parse(_profile(), httpx.MockTransport(lambda request: next(replies))).answer == "ok"
    assert delays == [1.0, 2.0]


def test_ollama_reasoning_fallback_remains_narrow() -> None:
    payload = {"choices": [{"message": {"content": "", "reasoning": '{"answer":"ok"}'}}]}
    p = ModelProfile(
        provider="Ollama",
        endpoint="http://localhost/v1",
        model="test",
        reasoning_effort="none",
        ollama_reasoning_fallback=True,
    )
    assert _parse(p, _returning(payload)).answer == "ok"
    with pytest.raises(ModelTransportError):
        _parse(p.model_copy(update={"ollama_reasoning_fallback": False}), _returning(payload))
    with pytest.raises(ValidationError):
        ModelProfile(
            provider="Google",
            endpoint="https://example.test/v1",
            model="test",
            reasoning_effort="none",
            ollama_reasoning_fallback=True,
        )


def test_diagnostics_redact_credentials_and_capture_usage(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    trace = JsonLinesDebugTrace(str(path), ("private-key",))
    p = _profile(api_key="private-key")
    with httpx.Client(transport=_returning(response())) as http:
        CompatibleStructuredOutputClient(p, http, trace).parse(
            system_prompt="app", user_prompt="source", schema_type=Result
        )
    trace.record("config", config=p, authorization="Bearer private-key")
    trace.close()
    text = path.read_text()
    assert "private-key" not in text
    assert p.fingerprint in text and '"total_tokens":15' in text and "duration_ms" in text


def test_invalid_output_is_traced_with_only_numeric_usage(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    trace = JsonLinesDebugTrace(str(path))
    payload = response('{"answer":7}')
    payload["usage"] = {"total_tokens": 15, "provider_note": "private text", "cost": "1.0"}
    with httpx.Client(transport=_returning(payload)) as http:
        with pytest.raises(ModelTransportError):
            CompatibleStructuredOutputClient(_profile(), http, trace).parse(
                system_prompt="app", user_prompt="source", schema_type=Result
            )
    trace.close()
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    invalid = next(item for item in events if item["event"] == "model.output_invalid")
    assert invalid["details"]["usage"] == {"total_tokens": 15}


def test_non_mapping_usage_is_dropped(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    trace = JsonLinesDebugTrace(str(path))
    payload = response()
    payload["usage"] = ["private"]
    with httpx.Client(transport=_returning(payload)) as http:
        CompatibleStructuredOutputClient(_profile(), http, trace).parse(
            system_prompt="app", user_prompt="source", schema_type=Result
        )
    trace.close()
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    completed = next(item for item in events if item["event"] == "model.completed")
    assert completed["details"]["usage"] == {}


def test_provider_extras_and_output_parameter_are_transmitted() -> None:
    p = _profile(
        output_parameter="max_completion_tokens",
        request_options={"temperature": 0, "extra_body": {"custom_setting": "enabled"}},
    )
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    _parse(p, httpx.MockTransport(handler))
    assert sent[0]["custom_setting"] == "enabled" and "extra_body" not in sent[0]
    assert sent[0]["temperature"] == 0
    assert sent[0]["max_completion_tokens"] == 8192 and "max_tokens" not in sent[0]


def test_context_budget_rejection_does_not_call_provider() -> None:
    p = _profile(context_tokens=100, output_tokens=50)

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Prompt exceeds budget")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(CompatibleOutputError, match="context budget"):
            CompatibleStructuredOutputClient(p, http, NullDebugTrace()).parse(
                system_prompt="app", user_prompt="x" * 1000, schema_type=Result
            )


# --- Embeddings -------------------------------------------------------------


@pytest.mark.parametrize("protocol", ["compatible", "google"])
def test_embeddings_order_dimensions_and_normalization(protocol: str) -> None:
    p = EmbeddingProfile.model_validate(
        {
            "provider": "Test",
            "protocol": protocol,
            "endpoint": "https://example.test/v1",
            "model": "embedding",
            "batch_size": 2,
        }
    )
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        count = len(body["requests"] if protocol == "google" else body["input"])
        vectors = [[float(index + 1)] * 768 for index in range(count)]
        payload = (
            {"embeddings": [{"values": vector} for vector in vectors]}
            if protocol == "google"
            else {
                "data": [
                    {"index": index, "embedding": vector}
                    for index, vector in reversed(list(enumerate(vectors)))
                ]
            }
        )
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        embedding = ConfiguredKnowledgeEmbedding(p, http, NullDebugTrace())
        assert embedding.model == "embedding" and embedding.identity == p.identity
        vectors = embedding.embed(("first", "second", "third"))
    assert len(sent) == 2 and len(vectors) == 3
    assert all(
        len(vector) == 768 and math.isclose(sum(x * x for x in vector), 1) for vector in vectors
    )
    if protocol == "google":
        assert sent[0]["requests"][0]["outputDimensionality"] == 768
    else:
        assert sent[0]["dimensions"] == 768


def test_compatible_embedding_request_shape_and_raw_vectors() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [2] * 768}]})

    p = EmbeddingProfile(
        provider="Test",
        endpoint="http://localhost/v1/",
        model="test",
        send_dimensions=False,
        normalize=False,
        api_key="embed-key",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        (vector,) = ConfiguredKnowledgeEmbedding(p, http, NullDebugTrace()).embed(("source",))
    assert vector == (2.0,) * 768
    assert str(requests[0].url) == "http://localhost/v1/embeddings"
    assert requests[0].headers["authorization"] == "Bearer embed-key"
    assert json.loads(requests[0].content) == {"model": "test", "input": ["source"]}


def test_google_embedding_request_shape() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"embeddings": [{"values": [1.0] * 768}]})

    p = EmbeddingProfile(
        provider="Google",
        protocol="google",
        endpoint="https://provider.example/v1beta",
        model="models/gemini-embedding-001",
        api_key="google-key",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        ConfiguredKnowledgeEmbedding(p, http, NullDebugTrace()).embed(("source",))
    assert requests[0].url.path == "/v1beta/models/gemini-embedding-001:batchEmbedContents"
    assert requests[0].headers["x-goog-api-key"] == "google-key"
    assert json.loads(requests[0].content)["requests"][0]["model"] == (
        "models/gemini-embedding-001"
    )


@pytest.mark.parametrize("issue", ["count", "dimension", "order", "zero", "type"])
def test_invalid_embeddings_are_rejected(issue: str) -> None:
    vector: list[object] = [1.0] * 768
    if issue == "dimension":
        vector = [1.0] * 767
    if issue == "zero":
        vector = [0.0] * 768
    if issue == "type":
        vector[0] = "bad"
    payload = {
        "data": []
        if issue == "count"
        else [{"index": 1 if issue == "order" else 0, "embedding": vector}]
    }
    p = EmbeddingProfile(provider="Test", endpoint="http://localhost/v1", model="test")
    with httpx.Client(transport=_returning(payload)) as http:
        with pytest.raises(KnowledgeGenerationError):
            ConfiguredKnowledgeEmbedding(p, http, NullDebugTrace()).embed(("source",))


@pytest.mark.parametrize(
    "payload",
    [[], {"data": [None]}, {"data": [{"index": "0", "embedding": [1.0] * 768}]}],
)
def test_malformed_embedding_envelopes_are_rejected(payload: object) -> None:
    p = EmbeddingProfile(provider="Test", endpoint="http://localhost/v1", model="test")
    with httpx.Client(transport=_returning(payload)) as http:
        with pytest.raises(KnowledgeGenerationError, match="invalid vectors"):
            ConfiguredKnowledgeEmbedding(p, http, NullDebugTrace()).embed(("source",))


def test_nonfinite_embedding_values_are_rejected() -> None:
    vector = [float("nan"), *([1.0] * 767)]
    p = EmbeddingProfile(provider="Test", endpoint="http://localhost/v1", model="test")
    payload = json.dumps({"data": [{"index": 0, "embedding": vector}]}).encode()
    with httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=payload))
    ) as http:
        with pytest.raises(KnowledgeGenerationError):
            ConfiguredKnowledgeEmbedding(p, http, NullDebugTrace()).embed(("source",))


def test_no_text_means_no_embedding_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("No request expected.")

    p = EmbeddingProfile(provider="Test", endpoint="http://localhost/v1", model="test")
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert ConfiguredKnowledgeEmbedding(p, http, NullDebugTrace()).embed(()) == ()


# --- Embedding-model token counting ------------------------------------------


def counter_profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        provider="Google",
        protocol="google",
        endpoint="https://provider.example/v1beta",
        model="gemini-embedding-001",
        api_key="synthetic-key",
    )


def _counter(handler: Callable[[httpx.Request], httpx.Response]) -> GoogleEmbeddingTokenCounter:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return GoogleEmbeddingTokenCounter(counter_profile(), http, NullDebugTrace())


def test_counting_calls_the_embedding_model_with_exact_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1beta/models/gemini-embedding-001:countTokens"
        assert json.loads(request.content) == {
            "contents": [{"parts": [{"text": "التغطية Coverage"}]}]
        }
        assert request.headers["x-goog-api-key"] == "synthetic-key"
        return httpx.Response(200, json={"totalTokens": 7})

    counter = _counter(handler)
    assert counter.count("التغطية Coverage") == 7
    assert counter.identity.startswith("google-countTokens:gemini-embedding-001:")
    assert "synthetic-key" not in counter.identity


def test_blank_text_may_count_as_zero_tokens() -> None:
    assert _counter(lambda _: httpx.Response(200, json={"totalTokens": 0})).count("  ") == 0


@pytest.mark.parametrize(
    "payload",
    [{}, [], {"totalTokens": True}, {"totalTokens": -1}, {"totalTokens": 0}, {"totalTokens": "7"}],
)
def test_invalid_count_is_explicit_failure_never_utf8_fallback(payload: object) -> None:
    with pytest.raises(ModelTransportError, match="invalid"):
        _counter(lambda _: httpx.Response(200, json=payload)).count("hello")


def test_unsupported_endpoint_never_uses_a_chat_tokenizer() -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))) as http:
        with pytest.raises(ModelTransportError):
            GoogleEmbeddingTokenCounter(counter_profile(), http, NullDebugTrace()).count("hello")
        with pytest.raises(ModelTransportError):
            GoogleEmbeddingTokenCounter(
                counter_profile().model_copy(update={"protocol": "compatible"}),
                http,
                NullDebugTrace(),
            )
