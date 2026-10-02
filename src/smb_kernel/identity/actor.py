"""Provider-neutral actor identity.

Who a person is and which roles their token grants. What a role permits is each
application's decision, never the kernel's.
"""

from __future__ import annotations

from dataclasses import dataclass


class InvalidIdentityError(ValueError):
    """Actor or assignment content is invalid."""


def _text(value: str, field: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise InvalidIdentityError(f"Identity {field} must not be blank.")
    return stripped


@dataclass(frozen=True, order=True)
class ActorId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _text(self.value, "actor id"))


@dataclass(frozen=True)
class ActorProfile:
    id: ActorId
    display_name: str
    email: str | None = None
    roles: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "display_name", _text(self.display_name, "display name"))
        if self.email is not None:
            email = self.email.strip()
            object.__setattr__(self, "email", email or None)
        roles = frozenset(role.strip() for role in self.roles if role.strip())
        object.__setattr__(self, "roles", roles)

    def snapshot(self) -> ActorSnapshot:
        return ActorSnapshot(self.id, self.display_name, self.email)


@dataclass(frozen=True)
class ActorSnapshot:
    id: ActorId
    display_name: str
    email: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "display_name", _text(self.display_name, "display name"))
        if self.email is not None:
            email = self.email.strip()
            object.__setattr__(self, "email", email or None)
