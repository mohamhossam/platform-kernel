"""Service-to-service authentication for internal routes.

Every route under the internal prefix requires `Authorization: Bearer <token>`
matching a configured caller's token. The edge proxy never routes the internal
prefix, so this is the second wall, not the only one.

Tokens are shared secrets. Keycloak client-credentials tokens are a later
release of this module; until then OIDC deployments configure secrets too.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping

from starlette.types import ASGIApp, Receive, Scope, Send

from smb_kernel.errors import ServiceAuthenticationError

INTERNAL_PREFIX = "/internal"
CALLER_SCOPE_KEY = "smb_service_caller"


class ServiceTokenVerifier:
    """Maps a presented token to the service that holds it."""

    def __init__(self, tokens: Mapping[str, str]) -> None:
        cleaned = {caller.strip(): token.strip() for caller, token in tokens.items()}
        if not cleaned or any(not caller or len(token) < 32 for caller, token in cleaned.items()):
            raise ValueError("Each service caller needs a name and a token of 32+ characters.")
        self._tokens = cleaned

    def caller(self, authorization: str | None) -> str:
        scheme, _, presented = (authorization or "").partition(" ")
        presented = presented.strip()
        if scheme.lower() != "bearer" or not presented:
            raise ServiceAuthenticationError("An internal request needs a service token.")
        for caller, token in self._tokens.items():
            if hmac.compare_digest(presented.encode(), token.encode()):
                return caller
        raise ServiceAuthenticationError("The service token is not recognised.")


class InternalRouteGuard:
    """ASGI middleware refusing internal routes without a valid service token.

    The authenticated caller's name is placed in the scope under
    `CALLER_SCOPE_KEY`, for audit and per-caller authorization in the routes.
    """

    def __init__(
        self, app: ASGIApp, verifier: ServiceTokenVerifier, prefix: str = INTERNAL_PREFIX
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
            scope[CALLER_SCOPE_KEY] = self._verifier.caller(authorization)
        except ServiceAuthenticationError as exc:
            body = json.dumps({"detail": str(exc)}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self._app(scope, receive, send)
