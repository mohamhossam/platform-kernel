"""Shared boundary for schema-validated chat-completion transports."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from smb_kernel.errors import ModelTransportError

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class StructuredOutputError(Exception):
    """Raised when a chat provider cannot return a usable typed response."""


class OutputTruncatedError(Exception):
    """The provider stopped its answer at the output-token limit.

    Raised as the cause of a transport's own error, so a caller that can ask
    for less (a smaller part of a document) can tell this failure apart.
    """


def truncated(error: BaseException) -> bool:
    """Whether a transport failure, or anything it was raised from, was a cut-off answer."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, OutputTruncatedError):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False


class StructuredResponseValidationError(StructuredOutputError):
    """A completed response has malformed JSON or an invalid output shape."""

    def __init__(self, feedback: str, *, unsupported: bool = False) -> None:
        super().__init__(feedback)
        self.unsupported = unsupported
        # Keep sanitized correction feedback here, while public error translation
        # follows the cause chain to a provider-neutral, safe description.
        self.__cause__ = ModelTransportError("invalid_output")


def response_provider_error(payload: object) -> ModelTransportError | None:
    """Detect provider failures inside successful HTTP envelopes before parsing content."""
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    choices = payload.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    failed = False
    if isinstance(first, dict):
        error = first.get("error") or error
        failed = first.get("finish_reason") == "error"
    if not error and not failed:
        return None
    code = error.get("code") if isinstance(error, dict) else None
    kind = {
        "429": "rate_limit",
        "401": "authentication",
        "403": "authentication",
        "402": "payment",
        "400": "configuration",
        "404": "configuration",
    }.get(str(code), "unavailable")
    return ModelTransportError(kind)


def response_validation_error(
    error: ValidationError, content: str
) -> StructuredResponseValidationError:
    """Keep correction feedback free of provider text, source contents and secrets."""
    unsupported = False
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("decisions"), list):
        unsupported = any(
            isinstance(item, dict) and item.get("supported") is False
            for item in payload["decisions"]
        )
    feedback: list[str] = []
    for item in error.errors(include_url=False, include_context=False)[:8]:
        location = item["loc"]
        if (
            len(location) == 4
            and location[0] == "decisions"
            and isinstance(location[1], int)
            and location[2] == "block_numbers"
            and isinstance(item.get("input"), int)
            and not isinstance(item["input"], bool)
        ):
            feedback.append(
                f"Decision at position {location[1] + 1} has invalid evidence block "
                f"number {item['input']}; use only the supplied evidence range."
            )
        else:
            # Locations can contain arbitrary dictionary keys; never echo them.
            feedback.append(f"Response validation failed ({item['type']}).")
    return StructuredResponseValidationError(
        " ".join(feedback) or "Response does not satisfy the output contract.",
        unsupported=unsupported,
    )


class StructuredOutputClient(Protocol):
    """Minimal transport required by focused structured-generation adapters."""

    @property
    def model(self) -> str: ...

    def parse(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_type: type[SchemaT],
        images: Sequence[tuple[str, bytes]] = (),
    ) -> SchemaT: ...
