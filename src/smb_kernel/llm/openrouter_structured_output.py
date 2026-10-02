"""Authenticated JSON-mode client for OpenRouter chat completions."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Sequence
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from smb_kernel.diagnostics import DebugTrace, NullDebugTrace
from smb_kernel.llm.structured_output import (
    OutputTruncatedError,
    StructuredOutputError,
    response_provider_error,
    response_validation_error,
)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

_MAX_ATTEMPTS = 3
_MAX_RETRY_AFTER_SECONDS = 10.0
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504, 529})


class OpenRouterError(StructuredOutputError):
    """Raised when OpenRouter fails or returns unusable JSON content."""


class OpenRouterStructuredOutputClient:
    """Request JSON output and validate it locally against a Pydantic schema."""

    def __init__(
        self,
        *,
        base_url: str,
        http_client: httpx.Client,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
        data_collection: str,
        debug_trace: DebugTrace | None = None,
    ) -> None:
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._data_collection = data_collection
        self._http = http_client
        self._debug_trace = debug_trace if debug_trace is not None else NullDebugTrace()

    @property
    def model(self) -> str:
        return self._model

    def parse(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_type: type[SchemaT],
        images: Sequence[tuple[str, bytes]] = (),
    ) -> SchemaT:
        schema = schema_type.model_json_schema()
        schema_text = json.dumps(schema, separators=(",", ":"), ensure_ascii=False)
        constrained_system_prompt = (
            f"{system_prompt}\n\n"
            "OUTPUT CONTRACT: Return exactly one JSON object and no prose or markdown. "
            "The object must satisfy this JSON Schema exactly:\n"
            f"{schema_text}"
        )
        user_content: object = user_prompt
        if images:
            user_content = [
                {"type": "text", "text": user_prompt},
                *(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": (
                                f"data:{mime_type};base64,"
                                f"{base64.b64encode(content).decode('ascii')}"
                            )
                        },
                    }
                    for mime_type, content in images
                ),
            ]
        request_body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": constrained_system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "provider": {
                "require_parameters": True,
                "data_collection": self._data_collection,
            },
            "stream": False,
            "max_tokens": self._max_output_tokens,
        }
        self._debug_trace.record(
            "openrouter.request",
            endpoint=self._endpoint,
            schema=schema_type.__name__,
            image_count=len(images),
            request_body=request_body,
        )
        try:
            response = self._post_with_retries(request_body, schema_type.__name__)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self._debug_trace.record(
                "openrouter.transport_failed",
                endpoint=self._endpoint,
                schema=schema_type.__name__,
                error_type=type(exc).__name__,
                error=str(exc),
                status_code=exc.response.status_code,
            )
            if exc.response.status_code == 429:
                raise OpenRouterError(_rate_limit_message(exc.response)) from exc
            raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc
        except httpx.HTTPError as exc:
            self._debug_trace.record(
                "openrouter.transport_failed",
                endpoint=self._endpoint,
                schema=schema_type.__name__,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            self._debug_trace.record(
                "openrouter.response_invalid_json",
                schema=schema_type.__name__,
                status_code=_status_code(response),
                response_text=response.text,
            )
            raise OpenRouterError("OpenRouter returned a non-JSON response.") from exc

        self._debug_trace.record(
            "openrouter.response",
            schema=schema_type.__name__,
            status_code=_status_code(response),
            payload=payload,
        )
        if not isinstance(payload, dict):
            raise OpenRouterError("OpenRouter returned an invalid response object.")
        provider_error = response_provider_error(payload)
        if provider_error is not None:
            raise OpenRouterError(
                "OpenRouter reported an upstream provider failure."
            ) from provider_error
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OpenRouterError("OpenRouter returned no choices.")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise OpenRouterError("OpenRouter returned an invalid choice.")
        if first_choice.get("finish_reason") == "length":
            raise OpenRouterError(
                "OpenRouter output was truncated after exhausting "
                f"OPENROUTER_MAX_OUTPUT_TOKENS={self._max_output_tokens}."
            ) from OutputTruncatedError()
        if first_choice.get("finish_reason") in {"content_filter", "tool_calls", "function_call"}:
            raise OpenRouterError("OpenRouter did not complete the structured-output request.")
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise OpenRouterError("OpenRouter returned no assistant message.")
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            raise OpenRouterError("OpenRouter refused the structured-output request.")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise OpenRouterError("OpenRouter returned empty completion content.")
        try:
            parsed = schema_type.model_validate_json(content, strict=True)
        except ValidationError as exc:
            self._debug_trace.record(
                "openrouter.schema_validation_failed",
                schema=schema_type.__name__,
                validation_error=str(exc),
            )
            raise OpenRouterError(
                f"OpenRouter response did not match {schema_type.__name__}."
            ) from response_validation_error(exc, content)
        self._debug_trace.record(
            "openrouter.parsed",
            schema=schema_type.__name__,
            parsed=parsed,
        )
        return parsed

    def _post_with_retries(
        self,
        request_body: dict[str, object],
        schema_name: str,
    ) -> httpx.Response:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = self._http.post(
                    self._endpoint,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_body,
                    timeout=self._timeout_seconds,
                )
            except httpx.TimeoutException:
                # A timed-out generation may already have completed and been billed.
                raise
            except httpx.TransportError:
                if attempt == _MAX_ATTEMPTS:
                    raise
                delay_seconds = float(2 ** (attempt - 1))
                self._record_retry(schema_name, attempt, None, delay_seconds)
                time.sleep(delay_seconds)
                continue

            if response.status_code not in _RETRYABLE_STATUS_CODES:
                return response
            retry_after = _retry_after_seconds(response)
            if response.status_code == 429 and retry_after is None:
                return response
            if attempt == _MAX_ATTEMPTS or (
                retry_after is not None and retry_after > _MAX_RETRY_AFTER_SECONDS
            ):
                return response
            delay_seconds = retry_after if retry_after is not None else float(2 ** (attempt - 1))
            self._record_retry(schema_name, attempt, response.status_code, delay_seconds)
            time.sleep(delay_seconds)

        raise AssertionError("OpenRouter retry loop exhausted without returning.")

    def _record_retry(
        self,
        schema_name: str,
        attempt: int,
        status_code: int | None,
        delay_seconds: float,
    ) -> None:
        self._debug_trace.record(
            "openrouter.retry_scheduled",
            endpoint=self._endpoint,
            schema=schema_name,
            failed_attempt=attempt,
            status_code=status_code,
            delay_seconds=delay_seconds,
        )


def _status_code(response: httpx.Response) -> int | None:
    status_code = getattr(response, "status_code", None)
    return status_code if isinstance(status_code, int) else None


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.0, seconds)


def _rate_limit_message(response: httpx.Response) -> str:
    retry_after = _retry_after_seconds(response)
    retry_guidance = (
        f" Retry after {retry_after:g} seconds." if retry_after is not None else " Retry later."
    )
    return (
        "OpenRouter rate limit reached."
        f"{retry_guidance} The configured free model may be temporarily throttled "
        "or its daily allowance may be exhausted; review the OpenRouter account limits."
    )
