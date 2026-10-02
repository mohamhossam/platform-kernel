"""Shared structured-output helpers: truncation marks, provider envelopes, safe feedback."""

from __future__ import annotations

import json
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, Field, ValidationError

from smb_kernel.errors import ModelTransportError
from smb_kernel.llm.structured_output import (
    OutputTruncatedError,
    StructuredOutputError,
    StructuredResponseValidationError,
    response_provider_error,
    response_validation_error,
    truncated,
)


class Decision(BaseModel):
    output_number: int
    supported: bool
    block_numbers: list[Annotated[int, Field(ge=1, le=3)]]


class Decisions(BaseModel):
    decisions: list[Decision]


class Keyed(BaseModel):
    values: dict[str, int]


def _validation_error(schema: type[BaseModel], content: str) -> ValidationError:
    with pytest.raises(ValidationError) as raised:
        schema.model_validate_json(content)
    return raised.value


def test_truncated_follows_the_cause_chain() -> None:
    cut_off = ModelTransportError("invalid_output")
    cut_off.__cause__ = OutputTruncatedError()
    wrapper = StructuredOutputError("wrapped")
    wrapper.__cause__ = cut_off

    assert truncated(wrapper)
    assert truncated(OutputTruncatedError())
    assert not truncated(ModelTransportError("invalid_output"))


def test_truncated_stops_on_a_cyclic_cause_chain() -> None:
    first = StructuredOutputError("first")
    second = StructuredOutputError("second")
    first.__cause__ = second
    second.__cause__ = first

    assert not truncated(first)


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": [{"finish_reason": "error", "message": {"content": "{}"}}]},
        {"error": {"code": "429", "message": "provider message"}},
        {"choices": [{"error": {"code": 429}, "message": {"content": "{}"}}]},
    ],
)
def test_incomplete_error_envelopes_are_detected(payload: dict[str, Any]) -> None:
    assert response_provider_error(payload) is not None


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        (429, "rate_limit"),
        (401, "authentication"),
        ("403", "authentication"),
        (402, "payment"),
        (400, "configuration"),
        (404, "configuration"),
        (503, "unavailable"),
        (None, "unavailable"),
    ],
)
def test_embedded_provider_error_codes_become_safe_kinds(code: object, kind: str) -> None:
    error = response_provider_error(
        {"choices": [{"error": {"code": code, "message": "provider text private-test-key"}}]}
    )
    assert error is not None
    assert error.kind == kind
    assert "private-test-key" not in str(error)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "text",
        {},
        {"choices": []},
        {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]},
    ],
)
def test_successful_envelopes_are_not_provider_errors(payload: object) -> None:
    assert response_provider_error(payload) is None


def test_failed_choice_without_error_object_is_unavailable() -> None:
    error = response_provider_error({"choices": [{"finish_reason": "error"}]})
    assert error is not None and error.kind == "unavailable"


def test_validation_feedback_names_invalid_evidence_numbers_and_unsupported_decisions() -> None:
    content = json.dumps(
        {
            "decisions": [
                {"output_number": 1, "supported": True, "block_numbers": [1]},
                {"output_number": 2, "supported": False, "block_numbers": [7]},
            ]
        }
    )
    error = response_validation_error(_validation_error(Decisions, content), content)

    assert isinstance(error, StructuredResponseValidationError)
    assert error.unsupported is True
    assert str(error) == (
        "Decision at position 2 has invalid evidence block number 7; "
        "use only the supplied evidence range."
    )
    assert isinstance(error.__cause__, ModelTransportError)
    assert error.__cause__.kind == "invalid_output"


def test_validation_feedback_never_echoes_dictionary_keys_or_values() -> None:
    content = json.dumps({"values": {"private-source-key": "private-source-value"}})
    error = response_validation_error(_validation_error(Keyed, content), content)

    assert str(error) == "Response validation failed (int_parsing)."
    assert "private-source" not in str(error)
    assert error.unsupported is False


def test_validation_feedback_tolerates_unparseable_content_and_caps_its_length() -> None:
    content = json.dumps({"decisions": [{} for _ in range(5)]})
    error = response_validation_error(_validation_error(Decisions, content), "not json")

    assert error.unsupported is False
    assert str(error).count("Response validation failed (missing).") == 8


def test_validation_feedback_has_a_generic_fallback() -> None:
    empty = ValidationError.from_exception_data("Decisions", [])
    assert str(response_validation_error(empty, "{}")) == (
        "Response does not satisfy the output contract."
    )
