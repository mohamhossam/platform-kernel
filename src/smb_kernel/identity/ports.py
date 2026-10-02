"""Inbound-credential to provider-neutral actor boundary."""

from dataclasses import dataclass
from typing import Protocol

from smb_kernel.identity.actor import ActorProfile


@dataclass(frozen=True)
class IdentityCredential:
    bearer_token: str | None = None
    actor_hint: str | None = None


class IdentityProviderPort(Protocol):
    def authenticate(self, credential: IdentityCredential) -> ActorProfile: ...
