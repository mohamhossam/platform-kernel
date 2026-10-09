"""Generic OIDC JWT validation adapter."""

from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock
from typing import Any, cast

import httpx
import jwt

from smb_kernel.errors import (
    AuthenticationRequiredError,
    IdentityProviderUnavailableError,
)
from smb_kernel.identity.actor import ActorId, ActorProfile
from smb_kernel.identity.ports import (
    IdentityCredential,
    IdentityProviderPort,
)


def discover_oidc(issuer: str, client: httpx.Client) -> dict[str, Any]:
    """Load the issuer's discovery document and check that it names the issuer."""
    issuer = issuer.rstrip("/")
    url = f"{issuer}/.well-known/openid-configuration"
    try:
        response = client.get(url)
        response.raise_for_status()
        raw_payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise IdentityProviderUnavailableError(
            "OIDC discovery could not be loaded from the configured issuer."
        ) from exc
    if not isinstance(raw_payload, dict):
        raise IdentityProviderUnavailableError("OIDC discovery returned an invalid object.")
    payload = cast(dict[str, Any], raw_payload)
    if str(payload.get("issuer", "")).rstrip("/") != issuer:
        raise IdentityProviderUnavailableError("OIDC discovery returned a different issuer.")
    return payload


class OidcSigningKeys:
    """An issuer's signing keys, discovered over HTTPS and cached.

    The key set is reloaded after `jwks_ttl_seconds`, and at most once per
    `unknown_key_ttl_seconds` when a token names a key the cache lacks, so a
    rotation is picked up without letting unknown key IDs force a fetch on
    every request.
    """

    def __init__(
        self,
        issuer: str,
        client: httpx.Client,
        monotonic_seconds: Callable[[], float],
        *,
        jwks_ttl_seconds: float = 900.0,
        unknown_key_ttl_seconds: float = 30.0,
        unknown_key_cache_size: int = 256,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._client = client
        self._monotonic = monotonic_seconds
        self._jwks_ttl = jwks_ttl_seconds
        self._unknown_key_ttl = unknown_key_ttl_seconds
        self._unknown_key_cache_size = unknown_key_cache_size
        self._jwks_uri: str | None = None
        self._keys: jwt.PyJWKSet | None = None
        self._keys_loaded_at = 0.0
        self._last_unknown_refresh = float("-inf")
        self._unknown_keys: OrderedDict[str, float] = OrderedDict()
        self._refresh_lock = Lock()

    @property
    def issuer(self) -> str:
        return self._issuer

    def close(self) -> None:
        self._client.close()

    def key(self, key_id: str) -> Any:
        """The verification key for `key_id`.

        Raises `AuthenticationRequiredError` when the issuer has no such key, and
        `IdentityProviderUnavailableError` when the keys cannot be loaded.
        """
        if not key_id:
            raise AuthenticationRequiredError("The bearer token has no signing-key ID.")
        with self._refresh_lock:
            now = self._monotonic()
            keys = self._keys
            expired = keys is None or now - self._keys_loaded_at >= self._jwks_ttl
            if expired:
                keys = self._load_keys()
                self._keys = keys
                self._keys_loaded_at = now
            if keys is None:
                raise IdentityProviderUnavailableError("OIDC signing keys are unavailable.")
            key = next((item for item in keys.keys if item.key_id == key_id), None)
            if key is not None:
                self._unknown_keys.pop(key_id, None)
                return key.key
            if self._unknown_keys.get(key_id, 0.0) > now:
                raise AuthenticationRequiredError("The bearer token signing key is unknown.")
            if now - self._last_unknown_refresh >= self._unknown_key_ttl:
                keys = self._load_keys()
                self._keys = keys
                self._keys_loaded_at = now
                self._last_unknown_refresh = now
                key = next((item for item in keys.keys if item.key_id == key_id), None)
                if key is not None:
                    return key.key
            self._unknown_keys[key_id] = now + self._unknown_key_ttl
            self._unknown_keys.move_to_end(key_id)
            while len(self._unknown_keys) > self._unknown_key_cache_size:
                self._unknown_keys.popitem(last=False)
            raise AuthenticationRequiredError("The bearer token signing key is unknown.")

    def _discover(self) -> str:
        payload = discover_oidc(self._issuer, self._client)
        uri = str(payload.get("jwks_uri", "")).strip()
        if not uri.startswith("https://"):
            raise IdentityProviderUnavailableError(
                "OIDC discovery did not provide an HTTPS JWKS URI."
            )
        return uri

    def _load_keys(self) -> jwt.PyJWKSet:
        if self._jwks_uri is None:
            self._jwks_uri = self._discover()
        try:
            response = self._client.get(self._jwks_uri)
            response.raise_for_status()
            raw_payload = response.json()
            if not isinstance(raw_payload, dict) or not isinstance(raw_payload.get("keys"), list):
                raise TypeError("JWKS response is not an object containing a keys array.")
            return jwt.PyJWKSet.from_dict(cast(dict[str, Any], raw_payload))
        except (httpx.HTTPError, ValueError, TypeError, jwt.PyJWTError) as exc:
            raise IdentityProviderUnavailableError("OIDC signing keys are unavailable.") from exc


class OidcIdentityProvider(IdentityProviderPort):
    def __init__(
        self,
        issuer: str,
        audience: str,
        allowed_algorithms: tuple[str, ...],
        client: httpx.Client,
        monotonic_seconds: Callable[[], float],
        *,
        jwks_ttl_seconds: float = 900.0,
        unknown_key_ttl_seconds: float = 30.0,
        unknown_key_cache_size: int = 256,
        roles_claim: str = "roles",
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._algorithms = allowed_algorithms
        self._roles_claim = roles_claim
        self._keys = OidcSigningKeys(
            issuer,
            client,
            monotonic_seconds,
            jwks_ttl_seconds=jwks_ttl_seconds,
            unknown_key_ttl_seconds=unknown_key_ttl_seconds,
            unknown_key_cache_size=unknown_key_cache_size,
        )

    def close(self) -> None:
        self._keys.close()

    def authenticate(self, credential: IdentityCredential) -> ActorProfile:
        token = (credential.bearer_token or "").strip()
        if not token:
            raise AuthenticationRequiredError("A bearer access token is required.")
        try:
            header = jwt.get_unverified_header(token)
            algorithm = str(header.get("alg", ""))
            if algorithm not in self._algorithms:
                raise AuthenticationRequiredError("The access token uses a disallowed algorithm.")
            key = self._keys.key(str(header.get("kid", "")))
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["iss", "sub", "aud", "exp"]},
            )
        except AuthenticationRequiredError:
            raise
        except jwt.PyJWTError as exc:
            raise AuthenticationRequiredError("The bearer access token is invalid.") from exc
        subject = str(claims.get("sub", "")).strip()
        if not subject:
            raise AuthenticationRequiredError("The bearer access token has no subject.")
        display_name = next(
            (
                str(claims.get(name, "")).strip()
                for name in ("name", "preferred_username", "email", "sub")
                if str(claims.get(name, "")).strip()
            ),
            subject,
        )
        email = str(claims.get("email", "")).strip() or None
        raw_roles = claims.get(self._roles_claim, ())
        if isinstance(raw_roles, str):
            roles = frozenset(item for item in raw_roles.split() if item)
        elif isinstance(raw_roles, list):
            roles = frozenset(str(item).strip() for item in raw_roles if str(item).strip())
        else:
            roles = frozenset()
        opaque_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self._issuer}\x1f{subject}"))
        return ActorProfile(ActorId(opaque_id), display_name, email, roles)
