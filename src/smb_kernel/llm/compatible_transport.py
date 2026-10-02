"""Shared chat-completions and text-embedding transports for configured profiles."""

from __future__ import annotations

import base64
import json
import math
import time
from collections.abc import Sequence
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from smb_kernel.diagnostics import DebugTrace
from smb_kernel.embeddings import Embedding
from smb_kernel.errors import KnowledgeGenerationError, ModelTransportError
from smb_kernel.llm.profiles import EmbeddingProfile, ModelProfile
from smb_kernel.llm.structured_output import (
    OutputTruncatedError,
    StructuredOutputError,
    response_provider_error,
    response_validation_error,
)

SchemaT = TypeVar("SchemaT", bound=BaseModel)
_RETRYABLE = {429, 500, 502, 503, 504, 529}


class CompatibleOutputError(StructuredOutputError):
    pass


def _post(
    http: httpx.Client,
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
    trace: DebugTrace,
) -> httpx.Response:
    start = time.monotonic()
    for attempt in range(1, 4):
        try:
            response = http.post(endpoint, json=body, headers=headers, timeout=timeout)
        except httpx.TimeoutException as exc:
            trace.record(
                "model.transport_failed",
                kind="timeout",
                duration_ms=(time.monotonic() - start) * 1000,
                attempt=attempt,
            )
            raise ModelTransportError("timeout") from exc
        except httpx.HTTPError as exc:
            raise ModelTransportError("unavailable") from exc
        trace.record(
            "model.http_response",
            status_code=response.status_code,
            duration_ms=(time.monotonic() - start) * 1000,
            attempt=attempt,
        )
        if response.status_code in _RETRYABLE and attempt < 3:
            try:
                delay = float(response.headers.get("retry-after", str(2 ** (attempt - 1))))
            except ValueError:
                delay = float(2 ** (attempt - 1))
            time.sleep(max(0, min(delay, 10)))
            continue
        if response.status_code >= 400:
            kind = (
                "rate_limit"
                if response.status_code == 429
                else "authentication"
                if response.status_code in {401, 403}
                else "unavailable"
                if response.status_code >= 500
                else "configuration"
            )
            raise ModelTransportError(kind)
        return response
    raise AssertionError("Unreachable transport attempt.")


class CompatibleStructuredOutputClient:
    def __init__(self, profile: ModelProfile, http: httpx.Client, trace: DebugTrace) -> None:
        self.profile = profile
        self._http = http
        self._trace = trace

    @property
    def model(self) -> str:
        return self.profile.model

    @property
    def configuration_fingerprint(self) -> str:
        return self.profile.fingerprint

    def parse(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_type: type[SchemaT],
        images: Sequence[tuple[str, bytes]] = (),
    ) -> SchemaT:
        p = self.profile
        if images and not p.images:
            raise CompatibleOutputError("Configured model does not support image evidence.")
        schema = schema_type.model_json_schema()
        estimate = (
            len(system_prompt) + len(user_prompt) + len(json.dumps(schema)) + 3
        ) // 4 + 1024 * len(images)
        if estimate > p.context_tokens - p.output_tokens:
            raise CompatibleOutputError(
                "Focused prompt exceeds the configured context budget; "
                "source evidence was not discarded."
            )
        system = system_prompt
        if p.structured_output == "json_object":
            system += (
                "\nOUTPUT CONTRACT: Return one JSON object satisfying this schema exactly:\n"
                + json.dumps(schema, separators=(",", ":"))
            )
        content: object = user_prompt
        if images:
            content = [
                {"type": "text", "text": user_prompt},
                *(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
                        },
                    }
                    for mime, data in images
                ),
            ]
        response_format: dict[str, Any] = (
            {"type": "json_object"}
            if p.structured_output == "json_object"
            else {
                "type": "json_schema",
                "json_schema": {"name": schema_type.__name__, "strict": True, "schema": schema},
            }
        )
        options = dict(p.request_options)
        extra = options.pop("extra_body", {})
        body = {
            "model": p.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "response_format": response_format,
            "stream": False,
            p.output_parameter: p.output_tokens,
            **options,
            **extra,
        }
        if p.reasoning_effort is not None:
            body["reasoning_effort"] = p.reasoning_effort
        headers = {"Authorization": f"Bearer {p.api_key}"} if p.api_key else {}
        start = time.monotonic()
        self._trace.record(
            "model.request",
            provider=p.provider,
            model=p.model,
            fingerprint=p.fingerprint,
            schema=schema_type.__name__,
            estimated_input_tokens=estimate,
            image_count=len(images),
        )
        response = _post(
            self._http,
            p.endpoint.rstrip("/") + "/chat/completions",
            body,
            headers,
            p.timeout_seconds,
            self._trace,
        )
        try:
            payload = response.json()
            provider_error = response_provider_error(payload)
            if provider_error is not None:
                self._trace.record(
                    "model.provider_failed",
                    kind=provider_error.kind,
                    provider=p.provider,
                    model=p.model,
                    duration_ms=(time.monotonic() - start) * 1000,
                )
                raise provider_error
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ValueError("Missing choice.")
            choice = choices[0]
            if choice.get("finish_reason") == "length":
                raise ModelTransportError("invalid_output") from OutputTruncatedError()
            if choice.get("finish_reason") in {
                "content_filter",
                "tool_calls",
                "function_call",
            }:
                raise ValueError("Output truncated.")
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("refusal"):
                raise ValueError("Missing or refused completion.")
            completion = message.get("content")
            if not completion and p.ollama_reasoning_fallback and p.reasoning_effort == "none":
                completion = message.get("reasoning")
            if not isinstance(completion, str) or not completion.strip():
                raise ValueError("Empty completion.")
        except (ValueError, ValidationError, TypeError) as exc:
            raise ModelTransportError("invalid_output") from exc
        usage = payload.get("usage", {})
        safe_usage = (
            {
                k: v
                for k, v in usage.items()
                if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and isinstance(v, int)
            }
            if isinstance(usage, dict)
            else {}
        )
        try:
            result = schema_type.model_validate_json(completion, strict=True)
        except ValidationError as exc:
            self._trace.record(
                "model.output_invalid",
                provider=p.provider,
                model=p.model,
                fingerprint=p.fingerprint,
                duration_ms=(time.monotonic() - start) * 1000,
                usage=safe_usage,
            )
            raise ModelTransportError("invalid_output") from response_validation_error(
                exc, completion
            )
        self._trace.record(
            "model.completed",
            provider=p.provider,
            model=p.model,
            fingerprint=p.fingerprint,
            duration_ms=(time.monotonic() - start) * 1000,
            usage=safe_usage,
        )
        return result


class ConfiguredKnowledgeEmbedding:
    def __init__(self, profile: EmbeddingProfile, http: httpx.Client, trace: DebugTrace) -> None:
        self.profile = profile
        self.model = profile.model
        self.identity = profile.identity
        self._http = http
        self._trace = trace

    def embed(self, texts: tuple[str, ...]) -> tuple[Embedding, ...]:
        values: list[Embedding] = []
        for start in range(0, len(texts), self.profile.batch_size):
            values.extend(self._batch(texts[start : start + self.profile.batch_size]))
        return tuple(values)

    def _batch(self, texts: tuple[str, ...]) -> tuple[Embedding, ...]:
        p = self.profile
        body: dict[str, Any]
        if p.protocol == "google":
            model = "models/" + p.model.removeprefix("models/")
            endpoint = p.endpoint.rstrip("/") + "/" + model + ":batchEmbedContents"
            body = {
                "requests": [
                    {
                        "model": model,
                        "content": {"parts": [{"text": text}]},
                        "outputDimensionality": p.dimensions,
                    }
                    for text in texts
                ]
            }
            headers = {"x-goog-api-key": p.api_key} if p.api_key else {}
        else:
            endpoint = p.endpoint.rstrip("/") + "/embeddings"
            body = {"model": p.model, "input": list(texts), **p.request_options}
            if p.send_dimensions:
                body["dimensions"] = p.dimensions
            headers = {"Authorization": f"Bearer {p.api_key}"} if p.api_key else {}
        response = _post(self._http, endpoint, body, headers, p.timeout_seconds, self._trace)
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid embedding object.")
            raw = data.get("embeddings" if p.protocol == "google" else "data")
            if (
                not isinstance(raw, list)
                or len(raw) != len(texts)
                or not all(isinstance(item, dict) for item in raw)
            ):
                raise ValueError("Invalid vector count.")
            if p.protocol == "compatible":
                indices = [item.get("index") for item in raw]
                if any(type(i) is not int for i in indices) or sorted(indices) != list(
                    range(len(texts))
                ):
                    raise ValueError("Invalid vector ordering.")
                raw.sort(key=lambda item: item["index"])
            vectors: list[Embedding] = []
            for item in raw:
                array = item.get("values" if p.protocol == "google" else "embedding")
                if (
                    not isinstance(array, list)
                    or len(array) != p.dimensions
                    or any(type(v) not in {int, float} for v in array)
                ):
                    raise ValueError("Invalid vector dimensions/values.")
                vector = tuple(float(v) for v in array)
                if not all(math.isfinite(v) for v in vector):
                    raise ValueError("Nonfinite vector.")
                norm = math.sqrt(sum(v * v for v in vector))
                if not norm or not math.isfinite(norm):
                    raise ValueError("Unusable vector.")
                vectors.append(tuple(v / norm for v in vector) if p.normalize else vector)
            return tuple(vectors)
        except (ValueError, TypeError) as exc:
            raise KnowledgeGenerationError("Embedding provider returned invalid vectors.") from exc


class GoogleEmbeddingTokenCounter:
    """Measure the selected embedding model through its own countTokens endpoint.

    Used by explicit qualification, never substituted with a chat model or invoked
    by library reads. Runtime splitting retains its independently labelled budget.
    """

    def __init__(self, profile: EmbeddingProfile, http: httpx.Client, trace: DebugTrace) -> None:
        if profile.protocol != "google":
            raise ModelTransportError("configuration")
        self._profile, self._http, self._trace = profile, http, trace

    @property
    def identity(self) -> str:
        return f"google-countTokens:{self._profile.model}:{self._profile.identity}"

    def count(self, text: str) -> int:
        p = self._profile
        model = "models/" + p.model.removeprefix("models/")
        response = _post(
            self._http,
            f"{p.endpoint.rstrip('/')}/{model}:countTokens",
            {"contents": [{"parts": [{"text": text}]}]},
            {"x-goog-api-key": p.api_key} if p.api_key else {},
            p.timeout_seconds,
            self._trace,
        )
        try:
            data = response.json()
            total = data.get("totalTokens") if isinstance(data, dict) else None
            if type(total) is not int or total < 0 or (text.strip() and total == 0):
                raise ValueError("Invalid token count.")
            return total
        except (ValueError, TypeError) as exc:
            raise ModelTransportError("invalid_output") from exc
