"""Versioned model profiles: task selection, credential handling and unsafe-option rejection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from smb_kernel.llm.profiles import (
    EmbeddingProfile,
    LLMProfileConfiguration,
    ModelProfile,
    ProfileConfigurationError,
    load_profiles,
)


def profile_data() -> dict[str, Any]:
    return {
        "version": 1,
        "default": "primary",
        "tasks": {"review": "other"},
        "embedding": "vectors",
        "profiles": {
            "primary": {
                "provider": "Gemini",
                "endpoint": "https://google.example/v1/",
                "model": "gemini-3.1-flash-lite",
                "api_key_env": "GEMINI_API_KEY",
                "images": True,
                "reasoning_effort": "minimal",
            },
            "other": {
                "provider": "Ollama",
                "endpoint": "http://localhost:11434/v1",
                "model": "local",
            },
            "unused": {
                "provider": "Other",
                "endpoint": "https://example.test/v1",
                "model": "other",
                "api_key_env": "UNUSED_KEY",
            },
        },
        "embeddings": {
            "vectors": {
                "provider": "Gemini",
                "protocol": "google",
                "endpoint": "https://google.example/v1beta",
                "model": "gemini-embedding-001",
                "api_key_env": "GEMINI_API_KEY",
            }
        },
    }


def write_config(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "llm.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_profiles_task_overrides_and_secret_exclusion(tmp_path: Path) -> None:
    config = load_profiles(
        str(write_config(tmp_path, profile_data())), {"GEMINI_API_KEY": "private-key"}
    )
    assert config.for_task("analysis").model == "gemini-3.1-flash-lite"
    assert config.for_task("review").model == "local"
    assert config.profiles["unused"].api_key is None
    assert config.selected_embedding.dimensions == 768
    assert "private-key" not in repr(config)
    assert "private-key" not in config.model_dump_json()
    assert "private-key" not in json.dumps(config.summary())
    assert (
        config.for_task("analysis").fingerprint
        == config.for_task("analysis").model_copy(update={"api_key": "replacement-key"}).fingerprint
    )


def test_selected_credentials_are_resolved_and_exposed_only_as_secrets(tmp_path: Path) -> None:
    config = load_profiles(
        str(write_config(tmp_path, profile_data())), {"GEMINI_API_KEY": "  private-key  "}
    )
    assert config.for_task("analysis").api_key == "private-key"
    assert config.for_task("review").api_key is None
    assert config.selected_embedding.api_key == "private-key"
    assert set(config.secrets) == {"private-key"}


def test_summary_names_every_task_and_the_embedding_identity(tmp_path: Path) -> None:
    config = load_profiles(
        str(write_config(tmp_path, profile_data())), {"GEMINI_API_KEY": "private-key"}
    )
    summary = config.summary()
    assert set(summary) == {
        "analysis",
        "generation",
        "review",
        "knowledge",
        "catalogue",
        "embedding",
    }
    assert summary["review"] == {
        "provider": "Ollama",
        "model": "local",
        "fingerprint": config.for_task("review").fingerprint,
    }
    assert summary["embedding"] == {
        "provider": "Gemini",
        "model": "gemini-embedding-001",
        "identity": config.selected_embedding.identity,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "task",
        "missing",
        "raw_key",
        "nested_auth",
        "endpoint",
        "budget",
        "raw_embedding_key",
        "unknown_embedding",
    ],
)
def test_configuration_rejects_invalid_or_unsafe_input(tmp_path: Path, mutation: str) -> None:
    data = profile_data()
    if mutation == "version":
        data["version"] = 2
    elif mutation == "task":
        data["tasks"] = {"unknown": "primary"}
    elif mutation == "missing":
        data["default"] = "absent"
    elif mutation == "raw_key":
        data["profiles"]["primary"]["api_key"] = "secret-value"
    elif mutation == "nested_auth":
        data["profiles"]["primary"]["request_options"] = {
            "extra_body": {"headers": {"Authorization": "secret-value"}}
        }
    elif mutation == "endpoint":
        data["profiles"]["primary"]["endpoint"] = "https://user:secret-value@example.test/v1"
    elif mutation == "raw_embedding_key":
        data["embeddings"]["vectors"]["api_key"] = "secret-value"
    elif mutation == "unknown_embedding":
        data["embedding"] = "secret-value"
    else:
        data["profiles"]["primary"]["context_tokens"] = 100
    with pytest.raises(ProfileConfigurationError) as failure:
        load_profiles(str(write_config(tmp_path, data)), {"GEMINI_API_KEY": "private-key"})
    assert "secret-value" not in str(failure.value)
    assert failure.value.__suppress_context__


@pytest.mark.parametrize("content", ["- not\n- a mapping\n", "profiles: [unclosed\n"])
def test_unreadable_configuration_is_rejected_without_echoing_it(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "llm.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ProfileConfigurationError, match="Invalid LLM profile configuration"):
        load_profiles(str(path), {})


def test_missing_configuration_file_is_a_profile_error(tmp_path: Path) -> None:
    with pytest.raises(ProfileConfigurationError):
        load_profiles(str(tmp_path / "absent.yaml"), {})


def test_missing_selected_credentials_fail_locally(tmp_path: Path) -> None:
    with pytest.raises(ProfileConfigurationError, match="GEMINI_API_KEY"):
        load_profiles(str(write_config(tmp_path, profile_data())), {})


def test_missing_embedding_credentials_fail_locally(tmp_path: Path) -> None:
    data = profile_data()
    data["profiles"]["primary"]["api_key_env"] = "CHAT_KEY"
    with pytest.raises(ProfileConfigurationError, match="Embedding profile requires GEMINI"):
        load_profiles(str(write_config(tmp_path, data)), {"CHAT_KEY": "chat-key"})


def test_embedding_identity_tracks_meaningful_changes() -> None:
    p = EmbeddingProfile(provider="Test", endpoint="http://localhost/v1", model="test")
    for update in (
        {"endpoint": "http://other/v1"},
        {"model": "other"},
        {"preprocessing_version": "v2"},
        {"normalize": False},
    ):
        assert p.model_copy(update=update).identity != p.identity
    assert p.model_copy(update={"api_key": "new-secret"}).identity == p.identity


def test_provider_options_cannot_shadow_routing_or_application_settings() -> None:
    safe = {"data_collection": "deny", "require_parameters": True, "allow_fallbacks": False}
    for options in (
        {"provider": safe, "extra_body": {"provider": {"allow_fallbacks": True}}},
        {"extra_body": {"reasoning_effort": "high"}},
        {"extra_body": {"tools": [{"type": "function"}]}},
        {"extra_body": "not an object"},
    ):
        with pytest.raises(ValidationError):
            ModelProfile(
                provider="OpenRouter",
                endpoint="https://openrouter.ai/api/v1",
                model="test",
                request_options=options,
            )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"endpoint": "ftp://example.test/v1"}, "HTTP\\(S\\) API root"),
        ({"endpoint": "https://example.test/v1?key=x"}, "HTTP\\(S\\) API root"),
        ({"provider": "  "}, "nonblank"),
        ({"output_tokens": 100, "context_tokens": 100}, "Output reserve"),
        ({"ollama_reasoning_fallback": True}, "requires reasoning_effort=none"),
        ({"request_options": {"stop": ["x"]}}, "Unsupported request option"),
        (
            {"request_options": {"extra_body": {"custom": [{"Tool_Choice": "auto"}]}}},
            "application-owned",
        ),
        (
            {"request_options": {"temperature": 0, "extra_body": {"temperature": 1}}},
            "cannot shadow",
        ),
        (
            {"request_options": {"provider": {"allow_fallbacks": True}}},
            "fallback is disabled",
        ),
        ({"endpoint": "https://openrouter.ai/api/v1"}, "restricted routing"),
        (
            {
                "provider": "Google",
                "reasoning_effort": "none",
                "ollama_reasoning_fallback": True,
            },
            "restricted to Ollama",
        ),
    ],
)
def test_model_profile_rejects_unsafe_settings(overrides: dict[str, Any], message: str) -> None:
    values: dict[str, Any] = {
        "provider": "Test",
        "endpoint": "https://example.test/v1",
        "model": "test",
        **overrides,
    }
    with pytest.raises(ValidationError, match=message):
        ModelProfile(**values)


def test_openrouter_profile_accepts_restricted_routing() -> None:
    profile = ModelProfile(
        provider="OpenRouter",
        endpoint="https://openrouter.ai/api/v1",
        model="test",
        request_options={
            "provider": {
                "data_collection": "deny",
                "require_parameters": True,
                "allow_fallbacks": False,
            }
        },
    )
    assert profile.request_options["provider"]["allow_fallbacks"] is False


def test_fingerprint_ignores_the_credential_variable_name_and_value() -> None:
    p = ModelProfile(provider="Test", endpoint="http://localhost/v1", model="test")
    assert p.model_copy(update={"api_key_env": "OTHER", "api_key": "k"}).fingerprint == (
        p.fingerprint
    )
    assert p.model_copy(update={"model": "other"}).fingerprint != p.fingerprint


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"endpoint": "https://user:pw@example.test/v1"}, "Invalid embedding API root"),
        ({"model": " "}, "nonblank"),
        ({"preprocessing_version": ""}, "nonblank"),
        ({"request_options": {"temperature": 0}}, "Only provider routing"),
        ({"endpoint": "https://openrouter.ai/api/v1"}, "restricted routing"),
    ],
)
def test_embedding_profile_rejects_unsafe_settings(overrides: dict[str, Any], message: str) -> None:
    values: dict[str, Any] = {
        "provider": "Test",
        "endpoint": "https://example.test/v1",
        "model": "test",
        **overrides,
    }
    with pytest.raises(ValidationError, match=message):
        EmbeddingProfile(**values)


def test_google_embeddings_require_normalization() -> None:
    with pytest.raises(ValidationError):
        EmbeddingProfile(
            provider="Google",
            protocol="google",
            endpoint="https://example.test/v1",
            model="test",
            normalize=False,
        )


def test_configuration_validates_assignments_directly() -> None:
    data = profile_data()
    with pytest.raises(ValidationError, match="Unknown model profile"):
        LLMProfileConfiguration.model_validate({**data, "tasks": {"review": "absent"}})
    with pytest.raises(ValidationError, match="Unknown embedding profile"):
        LLMProfileConfiguration.model_validate({**data, "embedding": "absent"})
