"""HTTP client for local OpenAI-compatible structured chat completions."""

from __future__ import annotations

import base64
import json
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


class LocalLLMError(StructuredOutputError):
    """Raised when a local model server fails or returns unusable output."""


class LocalStructuredOutputClient:
    """Call an unauthenticated local `/v1/chat/completions` endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        http_client: httpx.Client,
        model: str,
        timeout_seconds: float,
        reasoning_effort: str | None,
        context_window_tokens: int = 8192,
        max_output_tokens: int = 4096,
        debug_trace: DebugTrace | None = None,
    ) -> None:
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._reasoning_effort = reasoning_effort
        self._context_window_tokens = context_window_tokens
        self._max_output_tokens = max_output_tokens
        self._http = http_client
        self._debug_trace = debug_trace if debug_trace is not None else NullDebugTrace()

    @property
    def model(self) -> str:
        """The configured local model identifier used for provenance."""
        return self._model

    @property
    def input_budget_tokens(self) -> int:
        return self._context_window_tokens - self._max_output_tokens

    def parse(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_type: type[SchemaT],
        images: Sequence[tuple[str, bytes]] = (),
    ) -> SchemaT:
        """Request and validate one schema-constrained local completion."""
        # Ollama's OpenAI-compatible endpoint does not retain server-side
        # conversation state. Use a conservative, deterministic estimate and
        # fail before the provider silently truncates human decisions.
        schema = schema_type.model_json_schema()
        schema_text = json.dumps(schema, separators=(",", ":"))
        estimated_input_tokens = (
            len(system_prompt) + len(user_prompt) + len(schema_text) + 3
        ) // 4 + len(images) * 1024
        available_input = self._context_window_tokens - self._max_output_tokens
        if estimated_input_tokens > available_input:
            raise LocalLLMError(
                "Focused prompt exceeds the configured local context budget "
                f"({estimated_input_tokens} estimated input tokens; {available_input} allowed). "
                "Shorten the requirement or raise LOCAL_LLM_CONTEXT_WINDOW_TOKENS in both "
                "Ollama and this application. Human clarifications were not discarded."
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
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_type.__name__,
                    "strict": True,
                    "schema": schema,
                },
            },
            "stream": False,
            "max_tokens": self._max_output_tokens,
        }
        if self._reasoning_effort is not None:
            request_body["reasoning_effort"] = self._reasoning_effort

        self._debug_trace.record(
            "local_llm.request",
            endpoint=self._endpoint,
            schema=schema_type.__name__,
            estimated_input_tokens=estimated_input_tokens,
            available_input_tokens=available_input,
            image_count=len(images),
            request_body=request_body,
        )

        try:
            response = self._http.post(
                self._endpoint,
                json=request_body,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            self._debug_trace.record(
                "local_llm.transport_failed",
                endpoint=self._endpoint,
                schema=schema_type.__name__,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise LocalLLMError(f"Local LLM request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            self._debug_trace.record(
                "local_llm.response_invalid_json",
                schema=schema_type.__name__,
                status_code=_status_code(response),
                response_text=response.text,
            )
            raise LocalLLMError("Local LLM returned a non-JSON response.") from exc

        self._debug_trace.record(
            "local_llm.response",
            schema=schema_type.__name__,
            status_code=_status_code(response),
            payload=payload,
        )

        if not isinstance(payload, dict):
            raise LocalLLMError("Local LLM returned an invalid response object.")
        provider_error = response_provider_error(payload)
        if provider_error is not None:
            raise LocalLLMError("Local LLM reported a provider failure.") from provider_error

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LocalLLMError("Local LLM returned no choices.")

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise LocalLLMError("Local LLM returned an invalid choice.")
        if first_choice.get("finish_reason") == "length":
            raise LocalLLMError(
                "Local LLM output was truncated after exhausting "
                f"LOCAL_LLM_MAX_OUTPUT_TOKENS={self._max_output_tokens}. "
                "Increase LOCAL_LLM_CONTEXT_WINDOW_TOKENS and "
                "LOCAL_LLM_MAX_OUTPUT_TOKENS, then restart the application."
            ) from OutputTruncatedError()
        if first_choice.get("finish_reason") in {"content_filter", "tool_calls", "function_call"}:
            raise LocalLLMError("Local LLM did not complete the structured-output request.")
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise LocalLLMError("Local LLM returned no assistant message.")
        if message.get("refusal"):
            raise LocalLLMError("Local LLM refused the structured-output request.")
        content = message.get("content")
        if (
            not isinstance(content, str) or not content.strip()
        ) and self._reasoning_effort == "none":
            # Ollama 0.33 may place schema-constrained output from a thinking
            # model in `reasoning` even when reasoning was disabled. Accept it
            # only when it independently validates against the requested
            # schema; it is never exposed as a reasoning trace.
            content = message.get("reasoning")
        if not isinstance(content, str) or not content.strip():
            raise LocalLLMError("Local LLM returned empty completion content.")

        try:
            parsed = schema_type.model_validate_json(content)
        except ValidationError as exc:
            self._debug_trace.record(
                "local_llm.schema_validation_failed",
                schema=schema_type.__name__,
                completion_source=("content" if message.get("content") else "reasoning"),
                validation_error=str(exc),
            )
            raise LocalLLMError(
                f"Local LLM response did not match {schema_type.__name__}."
            ) from response_validation_error(exc, content)
        self._debug_trace.record(
            "local_llm.parsed",
            schema=schema_type.__name__,
            completion_source=("content" if message.get("content") else "reasoning"),
            parsed=parsed,
        )
        return parsed


def _status_code(response: httpx.Response) -> int | None:
    status_code = getattr(response, "status_code", None)
    return status_code if isinstance(status_code, int) else None
