"""OpenAI SDK transport for the shared structured-generation adapters.

Every focused operation (analysis, Epic, Feature, Story, quality, knowledge
screening, answer suggestions, reference proposals) runs through the same
`Structured…Adapter` classes the local and OpenRouter providers use, so a fix
to prompt assembly or response cleaning reaches every provider at once. This
module only knows how to ask the OpenAI SDK for one schema-constrained reply.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any, cast

import openai
from openai import OpenAI
from pydantic import ValidationError

from smb_kernel.errors import ModelTransportError
from smb_kernel.llm.structured_output import (
    OutputTruncatedError,
    SchemaT,
    StructuredOutputError,
    response_validation_error,
)


class OpenAIStructuredOutputClient:
    """One structured chat completion per call against the OpenAI API."""

    def __init__(self, client: OpenAI, *, model: str, timeout_seconds: float) -> None:
        self._client = client
        self._model = model
        self._timeout_seconds = timeout_seconds

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
        user_content: object = user_prompt
        if images:
            user_content = [
                {"type": "text", "text": user_prompt},
                *(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,"
                            f"{base64.b64encode(content).decode('ascii')}"
                        },
                    }
                    for mime_type, content in images
                ),
            ]
        try:
            response = self._client.chat.completions.parse(
                model=self._model,
                messages=cast(
                    Any,
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                ),
                response_format=schema_type,
                timeout=self._timeout_seconds,
            )
        except openai.LengthFinishReasonError as exc:
            cut_off = OutputTruncatedError()
            cut_off.__cause__ = exc
            raise StructuredOutputError("OpenAI returned truncated output.") from (
                _classified("invalid_output", cut_off)
            )
        except openai.ContentFilterFinishReasonError as exc:
            raise StructuredOutputError("OpenAI returned filtered output.") from (
                _classified("invalid_output", exc)
            )
        except ValidationError as exc:
            raise response_validation_error(exc, "") from _classified("invalid_output", exc)
        except openai.OpenAIError as exc:
            raise StructuredOutputError("OpenAI request failed.") from _transport_error(exc)
        if not response.choices:
            raise StructuredOutputError("OpenAI returned no choices.") from _classified(
                "invalid_output"
            )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise StructuredOutputError("OpenAI returned an empty or refused response.") from (
                _classified("invalid_output")
            )
        return parsed


def _classified(kind: str, cause: BaseException | None = None) -> ModelTransportError:
    """A public-safe failure kind that still carries the SDK error for logs."""
    error = ModelTransportError(kind)
    error.__cause__ = cause
    return error


def _transport_error(exc: openai.OpenAIError) -> ModelTransportError:
    """Classify an SDK failure without echoing provider text to clients."""
    if isinstance(exc, openai.APITimeoutError):
        return _classified("timeout", exc)
    if isinstance(exc, openai.RateLimitError):
        return _classified("rate_limit", exc)
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return _classified("authentication", exc)
    if isinstance(exc, openai.BadRequestError | openai.NotFoundError):
        return _classified("configuration", exc)
    return _classified("unavailable", exc)
