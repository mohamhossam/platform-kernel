"""Deterministic offline identity provider.

Each application supplies its own personas; the kernel ships none, because which
roles a persona carries is business meaning.
"""

from smb_kernel.errors import AuthenticationRequiredError
from smb_kernel.identity.actor import ActorProfile
from smb_kernel.identity.ports import (
    IdentityCredential,
    IdentityProviderPort,
)


class FakeIdentityProvider(IdentityProviderPort):
    def __init__(self, actors: tuple[ActorProfile, ...]) -> None:
        if not actors:
            raise ValueError("A fake identity provider needs at least one persona.")
        self.actors = actors

    def authenticate(self, credential: IdentityCredential) -> ActorProfile:
        actor_id = (credential.actor_hint or self.actors[0].id.value).strip()
        actor = next((item for item in self.actors if item.id.value == actor_id), None)
        if actor is None:
            raise AuthenticationRequiredError("Unknown fake actor identity.")
        return actor
