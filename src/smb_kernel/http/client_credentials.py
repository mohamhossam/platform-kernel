"""Service access tokens from an OIDC issuer's client-credentials grant.

A calling service holds only its own client ID and secret. The issuer (for
example Keycloak) grants it short-lived access tokens, which the receiving
service checks with `ServiceJwtVerifier` without holding any secret.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from threading import Lock
from typing import Any, cast

import httpx

from smb_kernel.errors import IdentityProviderUnavailableError, ServiceUnavailableError
from smb_kernel.identity.oidc import discover_oidc


class ClientCredentialsTokenSource:
    """Grants and caches this service's access token; call it for the current one.

    The token endpoint comes from the issuer's discovery document and must use
    HTTPS. A token is reused until `refresh_margin_seconds` before it expires (or
    half its lifetime, when that is shorter), so a request never carries one that
    lapses on the way. Any failure to get a token raises
    `ServiceUnavailableError`, which callers already treat as the peer being
    unavailable.
    """

    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: str,
        *,
        client: httpx.Client | None = None,
        scope: str | None = None,
        refresh_margin_seconds: float = 30.0,
        timeout_seconds: float = 10.0,
        monotonic_seconds: Callable[[], float] = time.monotonic,
    ) -> None:
        if not issuer.strip() or not client_id.strip() or not client_secret.strip():
            raise ValueError("A client-credentials grant needs an issuer, client ID and secret.")
        self._issuer = issuer.strip().rstrip("/")
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._client = client or httpx.Client()
        self._scope = scope
        self._margin = refresh_margin_seconds
        self._timeout = timeout_seconds
        self._monotonic = monotonic_seconds
        self._token_endpoint: str | None = None
        self._token: str | None = None
        self._refresh_at = float("-inf")
        self._lock = Lock()

    def close(self) -> None:
        self._client.close()

    def __call__(self) -> str:
        with self._lock:
            now = self._monotonic()
            if self._token is None or now >= self._refresh_at:
                token, lifetime = self._grant()
                self._token = token
                self._refresh_at = now + lifetime - min(self._margin, lifetime / 2)
            return self._token

    def invalidate(self) -> None:
        """Drop the cached token, so the next call is granted a fresh one."""
        with self._lock:
            self._token = None

    def _endpoint(self) -> str:
        if self._token_endpoint is None:
            try:
                payload = discover_oidc(self._issuer, self._client)
            except IdentityProviderUnavailableError as exc:
                raise ServiceUnavailableError(str(exc)) from exc
            endpoint = str(payload.get("token_endpoint", "")).strip()
            if not endpoint.startswith("https://"):
                raise ServiceUnavailableError(
                    "OIDC discovery did not provide an HTTPS token endpoint."
                )
            self._token_endpoint = endpoint
        return self._token_endpoint

    def _grant(self) -> tuple[str, float]:
        form = {"grant_type": "client_credentials"}
        if self._scope:
            form["scope"] = self._scope
        try:
            response = self._client.post(
                self._endpoint(),
                data=form,
                auth=(self._client_id, self._client_secret),
                headers={"Accept": "application/json"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("The identity provider could not be reached.") from exc
        if response.status_code != 200:
            raise ServiceUnavailableError(
                f"The identity provider refused this service a token ({response.status_code})."
            )
        try:
            raw_payload = response.json()
        except ValueError as exc:
            raise ServiceUnavailableError(
                "The identity provider's token response is not JSON."
            ) from exc
        payload = cast(dict[str, Any], raw_payload) if isinstance(raw_payload, dict) else {}
        token = payload.get("access_token")
        token_type = str(payload.get("token_type", "bearer")).lower()
        lifetime = payload.get("expires_in")
        if (
            not isinstance(token, str)
            or not token.strip()
            or token_type != "bearer"
            or isinstance(lifetime, bool)
            or not isinstance(lifetime, int | float)
            or lifetime <= 0
        ):
            raise ServiceUnavailableError(
                "The identity provider's token response has no usable bearer token."
            )
        return token.strip(), float(lifetime)
