"""Versioned, secret-free model profiles resolved by Settings at startup."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ProfileConfigurationError(ValueError):
    pass


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str
    endpoint: str
    model: str
    api_key_env: str | None = None
    images: bool = False
    structured_output: Literal["json_schema", "json_object"] = "json_schema"
    context_tokens: int = Field(default=32768, gt=0)
    output_tokens: int = Field(default=8192, gt=0)
    output_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None
    timeout_seconds: float = Field(default=120, gt=0, le=600)
    request_options: dict[str, Any] = Field(default_factory=dict)
    ollama_reasoning_fallback: bool = False
    api_key: str | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_profile(self) -> ModelProfile:
        parts = urlsplit(self.endpoint)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "Endpoint must be an HTTP(S) API root without credentials or query parameters."
            )
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("Provider and model must be nonblank.")
        if self.output_tokens >= self.context_tokens:
            raise ValueError("Output reserve must be smaller than the context budget.")
        if self.ollama_reasoning_fallback and self.reasoning_effort != "none":
            raise ValueError("Ollama reasoning fallback requires reasoning_effort=none.")
        allowed = {"provider", "temperature", "top_p", "seed", "extra_body", "service_tier"}
        if set(self.request_options) - allowed:
            raise ValueError(
                "Unsupported request option; application-owned request fields cannot be replaced."
            )
        protected = {
            "messages",
            "model",
            "response_format",
            "stream",
            "max_tokens",
            "max_completion_tokens",
            "authorization",
            "api_key",
            "headers",
            "endpoint",
            "reasoning_effort",
            "tools",
            "functions",
            "tool_choice",
            "instructions",
            "prompt",
            "input",
        }

        def check(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key).lower() in protected:
                        raise ValueError(
                            "Provider options cannot replace application-owned request fields."
                        )
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)

        check(self.request_options)
        extra = self.request_options.get("extra_body", {})
        if not isinstance(extra, dict):
            raise ValueError("extra_body must be an object.")
        if set(extra).intersection(set(self.request_options) - {"extra_body"}):
            raise ValueError("Provider extras cannot shadow another configured request option.")
        routing = self.request_options.get("provider", extra.get("provider", {}))
        if isinstance(routing, dict) and routing.get("allow_fallbacks") is True:
            raise ValueError("Automatic provider fallback is disabled.")
        if parts.hostname == "openrouter.ai" and (
            not isinstance(routing, dict)
            or routing.get("data_collection") != "deny"
            or routing.get("require_parameters") is not True
            or routing.get("allow_fallbacks") is not False
        ):
            raise ValueError(
                "OpenRouter profiles require restricted routing with fallback disabled."
            )
        if self.ollama_reasoning_fallback and self.provider.casefold() != "ollama":
            raise ValueError("Reasoning-response compatibility is restricted to Ollama profiles.")
        return self

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(exclude={"api_key_env"}), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()


class EmbeddingProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str
    protocol: Literal["compatible", "google"] = "compatible"
    endpoint: str
    model: str
    api_key_env: str | None = None
    dimensions: Literal[768] = 768
    send_dimensions: bool = True
    normalize: bool = True
    preprocessing_version: str = "text-v1"
    timeout_seconds: float = Field(default=120, gt=0, le=600)
    batch_size: int = Field(default=32, gt=0, le=100)
    request_options: dict[str, Any] = Field(default_factory=dict)
    api_key: str | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_profile(self) -> EmbeddingProfile:
        parts = urlsplit(self.endpoint)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Invalid embedding API root.")
        if (
            not self.model.strip()
            or not self.provider.strip()
            or not self.preprocessing_version.strip()
        ):
            raise ValueError("Embedding identity fields must be nonblank.")
        if self.protocol == "google" and not self.normalize:
            raise ValueError("Google embeddings require normalization.")
        if set(self.request_options) - {"provider"}:
            raise ValueError("Only provider routing options are supported for embeddings.")
        ModelProfile(
            provider=self.provider,
            endpoint=self.endpoint,
            model=self.model,
            request_options=self.request_options,
        )
        return self

    @property
    def identity(self) -> str:
        values = {
            "endpoint": self.endpoint.rstrip("/"),
            "protocol": self.protocol,
            "model": self.model,
            "dimensions": self.dimensions,
            "normalize": self.normalize,
            "preprocessing": self.preprocessing_version,
            "request_options": self.request_options,
        }
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


class LLMProfileConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1]
    profiles: dict[str, ModelProfile]
    embeddings: dict[str, EmbeddingProfile]
    default: str
    tasks: dict[str, str] = Field(default_factory=dict)
    embedding: str

    @model_validator(mode="after")
    def validate_assignments(self) -> LLMProfileConfiguration:
        if set(self.tasks) - {"analysis", "generation", "review", "knowledge", "catalogue"}:
            raise ValueError("Unknown task assignment.")
        for name in {self.default, *self.tasks.values()}:
            if name not in self.profiles:
                raise ValueError(f"Unknown model profile {name!r}.")
        if self.embedding not in self.embeddings:
            raise ValueError("Unknown embedding profile.")
        return self

    def for_task(self, task: str) -> ModelProfile:
        return self.profiles[self.tasks.get(task, self.default)]

    @property
    def selected_embedding(self) -> EmbeddingProfile:
        return self.embeddings[self.embedding]

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(
            key
            for key in (
                *[p.api_key for p in self.profiles.values()],
                *[p.api_key for p in self.embeddings.values()],
            )
            if key
        )

    def summary(self) -> dict[str, object]:
        return {
            **{
                task: {
                    "provider": self.for_task(task).provider,
                    "model": self.for_task(task).model,
                    "fingerprint": self.for_task(task).fingerprint,
                }
                for task in ("analysis", "generation", "review", "knowledge", "catalogue")
            },
            "embedding": {
                "provider": self.selected_embedding.provider,
                "model": self.selected_embedding.model,
                "identity": self.selected_embedding.identity,
            },
        }


def load_profiles(path: str, environment: dict[str, str]) -> LLMProfileConfiguration:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("Profile configuration must be a mapping.")
        # Credentials are forbidden in the file, including unused example profiles.
        for section in ("profiles", "embeddings"):
            for profile in data.get(section, {}).values():
                if "api_key" in profile:
                    raise ValueError(
                        "Put credentials in the named environment variable, never in YAML."
                    )
        config = LLMProfileConfiguration.model_validate(data)
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError, ValidationError):
        # Pydantic's errors may contain input values; never echo raw YAML.
        raise ProfileConfigurationError(
            "Invalid LLM profile configuration; check fields, task names and profile limits."
        ) from None
    selected = {config.default, *config.tasks.values()}
    profiles = dict(config.profiles)
    embeddings = dict(config.embeddings)
    for name in selected:
        profile = profiles[name]
        key = environment.get(profile.api_key_env or "", "").strip() or None
        if profile.api_key_env and not key:
            raise ProfileConfigurationError(f"Profile {name!r} requires {profile.api_key_env}.")
        profiles[name] = profile.model_copy(update={"api_key": key})
    profile_e = embeddings[config.embedding]
    key = environment.get(profile_e.api_key_env or "", "").strip() or None
    if profile_e.api_key_env and not key:
        raise ProfileConfigurationError(f"Embedding profile requires {profile_e.api_key_env}.")
    embeddings[config.embedding] = profile_e.model_copy(update={"api_key": key})
    return config.model_copy(update={"profiles": profiles, "embeddings": embeddings})
