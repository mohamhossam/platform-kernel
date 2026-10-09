"""Service-to-service authentication for internal routes.

Every route under the internal prefix requires `Authorization: Bearer <token>`
matching a configured caller's token. The edge proxy never routes the internal
prefix, so this is the second wall, not the only one.

Two kinds of token are recognised, alone or together:

- shared secrets (`ServiceTokenVerifier`), one per calling service, which both
  sides hold;
- access tokens an OIDC issuer such as Keycloak grants each calling service
  through the client-credentials grant (`ServiceJwtVerifier`), so a service holds
  only its own client secret and the receiver holds no secret at all.

`ServiceVerifierChain` accepts either, for a deployment moving from one to the
other or keeping shared secrets for offline runs.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from typing import Protocol

import jwt
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from smb_kernel.errors import (
    AuthenticationRequiredError,
    IdentityProviderUnavailableError,
    ServiceAuthenticationError,
)
from smb_kernel.identity.oidc import OidcSigningKeys

INTERNAL_PREFIX = "/internal"
CALLER_SCOPE_KEY = "smb_service_caller"


class ServiceCallerVerifier(Protocol):
    def caller(self, authorization: str | None) -> str:
        """The calling service's name for an `Authorization` header value.

        Raises `ServiceAuthenticationError` when the token is missing or not
        recognised, and `IdentityProviderUnavailableError` when it cannot be
        checked right now.
        """
        ...


def _bearer(authorization: str | None) -> str:
    scheme, _, presented = (authorization or "").partition(" ")
    presented = presented.strip()
    if scheme.lower() != "bearer" or not presented:
        raise ServiceAuthenticationError("An internal request needs a service token.")
    return presented


class ServiceTokenVerifier:
    """Maps a presented shared secret to the service that holds it."""

    def __init__(self, tokens: Mapping[str, str]) -> None:
        cleaned = {caller.strip(): token.strip() for caller, token in tokens.items()}
        if not cleaned or any(not caller or len(token) < 32 for caller, token in cleaned.items()):
            raise ValueError("Each service caller needs a name and a token of 32+ characters.")
        self._tokens = cleaned

    def caller(self, authorization: str | None) -> str:
        presented = _bearer(authorization)
        for caller, token in self._tokens.items():
            if hmac.compare_digest(presented.encode(), token.encode()):
                return caller
        raise ServiceAuthenticationError("The service token is not recognised.")


class ServiceJwtVerifier:
    """Maps an issuer-signed service access token to the service it was granted to.

    The token must be signed by one of the issuer's keys with an allowed
    algorithm, name the issuer and `audience`, and not have expired. The client
    it was granted to (`azp`, or `client_id` when `azp` is absent) must be one of
    `callers`, which maps client IDs to the caller names the routes see. A
    person's token never names a service client, so it is refused.
    """

    def __init__(
        self,
        keys: OidcSigningKeys,
        audience: str,
        callers: Mapping[str, str],
        *,
        allowed_algorithms: tuple[str, ...] = ("RS256",),
        leeway_seconds: float = 0.0,
    ) -> None:
        cleaned = {client.strip(): caller.strip() for client, caller in callers.items()}
        if not audience.strip():
            raise ValueError("Service access tokens need an audience.")
        if not cleaned or any(not client or not caller for client, caller in cleaned.items()):
            raise ValueError("Each service client needs a client ID and a caller name.")
        if not allowed_algorithms or any(
            name.upper().startswith("HS") for name in allowed_algorithms
        ):
            raise ValueError("Service access tokens need asymmetric signing algorithms.")
        self._keys = keys
        self._audience = audience.strip()
        self._callers = cleaned
        self._algorithms = allowed_algorithms
        self._leeway = leeway_seconds

    def caller(self, authorization: str | None) -> str:
        presented = _bearer(authorization)
        try:
            header = jwt.get_unverified_header(presented)
        except jwt.PyJWTError as exc:
            raise ServiceAuthenticationError("The service token is not recognised.") from exc
        if str(header.get("alg", "")) not in self._algorithms:
            raise ServiceAuthenticationError("The service token uses a disallowed algorithm.")
        try:
            key = self._keys.key(str(header.get("kid", "")))
            claims = jwt.decode(
                presented,
                key=key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._keys.issuer,
                leeway=self._leeway,
                options={"require": ["iss", "aud", "exp"]},
            )
        except AuthenticationRequiredError as exc:
            raise ServiceAuthenticationError(str(exc)) from exc
        except jwt.PyJWTError as exc:
            raise ServiceAuthenticationError("The service token is invalid.") from exc
        client = str(claims.get("azp") or claims.get("client_id") or "").strip()
        caller = self._callers.get(client)
        if caller is None:
            raise ServiceAuthenticationError("The service token was granted to an unknown client.")
        return caller


class ServiceVerifierChain:
    """Accepts a token any of its verifiers accepts, asking them in order.

    Put `ServiceTokenVerifier` first: it answers from memory, while a token it
    refuses goes on to the issuer-backed verifier.
    """

    def __init__(self, *verifiers: ServiceCallerVerifier) -> None:
        if not verifiers:
            raise ValueError("A verifier chain needs at least one verifier.")
        self._verifiers = verifiers

    def caller(self, authorization: str | None) -> str:
        refusal: ServiceAuthenticationError | None = None
        for verifier in self._verifiers:
            try:
                return verifier.caller(authorization)
            except ServiceAuthenticationError as exc:
                refusal = exc
        assert refusal is not None
        raise refusal


class InternalRouteGuard:
    """ASGI middleware refusing internal routes without a valid service token.

    The authenticated caller's name is placed in the scope under
    `CALLER_SCOPE_KEY`, for audit and per-caller authorization in the routes.
    A token that cannot be checked because the issuer is unreachable gets 503.
    The verifier runs in a worker thread, since checking an issuer-signed token
    may fetch the issuer's keys.
    """

    def __init__(
        self, app: ASGIApp, verifier: ServiceCallerVerifier, prefix: str = INTERNAL_PREFIX
    ) -> None:
        self._app = app
        self._verifier = verifier
        self._prefix = prefix.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path: str = scope.get("path", "")
        guarded = scope["type"] == "http" and (
            path == self._prefix or path.startswith(self._prefix + "/")
        )
        if not guarded:
            await self._app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", ())}
        authorization = headers.get(b"authorization", b"").decode("latin-1") or None
        try:
            scope[CALLER_SCOPE_KEY] = await run_in_threadpool(self._verifier.caller, authorization)
        except ServiceAuthenticationError as exc:
            await _refuse(send, 401, str(exc), [(b"www-authenticate", b"Bearer")])
            return
        except IdentityProviderUnavailableError:
            await _refuse(send, 503, "The service token cannot be checked right now.", [])
            return
        await self._app(scope, receive, send)


async def _refuse(send: Send, status: int, detail: str, headers: list[tuple[bytes, bytes]]) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *headers,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
