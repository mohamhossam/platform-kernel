"""Generic OIDC JWT validation adapter."""

from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
from collections.abc import Callable, Collection
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

_LOG = logging.getLogger(__name__)
# Clock difference allowed between this host and the issuer on `exp`, `nbf` and `iat`.
DEFAULT_LEEWAY_SECONDS = 60.0


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

    Fetching never holds up a caller that can be answered from the cache:
    - one caller at a time fetches, outside the cache's lock;
    - while it does, the others check tokens against the keys already held;
    - when a scheduled reload fails, the last good keys keep being served, and
      the reload is tried again after `retry_seconds`.

    Only a cold cache, or a token naming a key the cache lacks, waits for a
    fetch; callers waiting together share its outcome rather than each
    fetching in turn.
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
        retry_seconds: float = 30.0,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._client = client
        self._monotonic = monotonic_seconds
        self._jwks_ttl = jwks_ttl_seconds
        self._unknown_key_ttl = unknown_key_ttl_seconds
        self._unknown_key_cache_size = unknown_key_cache_size
        self._retry = retry_seconds
        self._jwks_uri: str | None = None
        self._keys: jwt.PyJWKSet | None = None
        self._reload_at = float("-inf")
        # Fetches begun and ended, so a waiting caller can tell whether one ran
        # while it waited.
        self._fetches_started = 0
        self._fetches = 0
        self._last_fetch_failed = False
        self._last_unknown_refresh = float("-inf")
        self._unknown_keys: OrderedDict[str, float] = OrderedDict()
        # Guards the cached state above; never held while fetching.
        self._state_lock = Lock()
        # Held by the one caller fetching.
        self._fetch_lock = Lock()

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
        with self._state_lock:
            keys = self._keys
            due = self._monotonic() >= self._reload_at
        if keys is None:
            keys = self._fetch(wait=True)
        elif due:
            refreshed = self._fetch(wait=False)
            keys = keys if refreshed is None else refreshed
        found = _find(keys, key_id)
        if found is not None:
            with self._state_lock:
                self._unknown_keys.pop(key_id, None)
            return found
        return self._unknown(key_id)

    def _unknown(self, key_id: str) -> Any:
        """A key the cache lacks: reload at most once per `unknown_key_ttl_seconds`."""
        with self._state_lock:
            now = self._monotonic()
            known_unknown = self._unknown_keys.get(key_id, 0.0) > now
            may_reload = not known_unknown and (
                now - self._last_unknown_refresh >= self._unknown_key_ttl
            )
            if may_reload:
                self._last_unknown_refresh = now
            last_failed = self._last_fetch_failed
        if known_unknown:
            raise AuthenticationRequiredError("The bearer token signing key is unknown.")
        if not may_reload:
            if last_failed:
                # The reload that would have found it failed: this token cannot
                # be checked yet, which is not the same as being invalid.
                raise IdentityProviderUnavailableError("OIDC signing keys are unavailable.")
            raise AuthenticationRequiredError("The bearer token signing key is unknown.")
        found = _find(self._fetch(wait=True, forced=True), key_id)
        if found is not None:
            return found
        with self._state_lock:
            self._unknown_keys[key_id] = self._monotonic() + self._unknown_key_ttl
            self._unknown_keys.move_to_end(key_id)
            while len(self._unknown_keys) > self._unknown_key_cache_size:
                self._unknown_keys.popitem(last=False)
        raise AuthenticationRequiredError("The bearer token signing key is unknown.")

    def _fetch(self, *, wait: bool, forced: bool = False) -> jwt.PyJWKSet | None:
        """Reload the key set, or share the outcome of a reload that ran meanwhile.

        Without `wait`, returns None at once when another caller is fetching,
        and the cached keys when the reload fails. With it, raises
        `IdentityProviderUnavailableError` when no keys can be had.

        A `forced` reload (for a key the cache lacks) shares only a fetch that
        began after it asked: one already under way may predate the new key.
        """
        with self._state_lock:
            seen_started = self._fetches_started
            seen_ended = self._fetches
        if not self._fetch_lock.acquire(blocking=wait):
            return None
        try:
            with self._state_lock:
                if forced:
                    shared = self._fetches_started != seen_started
                else:
                    shared = self._fetches != seen_ended
                keys = self._keys
                failed = self._last_fetch_failed
                due = self._monotonic() >= self._reload_at
            if shared:
                if keys is None or (wait and failed):
                    raise IdentityProviderUnavailableError("OIDC signing keys are unavailable.")
                return keys
            if not forced and not due and keys is not None:
                return keys
            with self._state_lock:
                self._fetches_started += 1
            try:
                loaded = self._load_keys()
            except IdentityProviderUnavailableError:
                with self._state_lock:
                    self._fetches += 1
                    self._last_fetch_failed = True
                    self._reload_at = self._monotonic() + self._retry
                    keys = self._keys
                if keys is None or wait:
                    raise
                _LOG.warning("Serving cached OIDC signing keys; reloading them failed.")
                return keys
            with self._state_lock:
                self._fetches += 1
                self._last_fetch_failed = False
                self._keys = loaded
                self._reload_at = self._monotonic() + self._jwks_ttl
            return loaded
        finally:
            self._fetch_lock.release()

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


def _find(keys: jwt.PyJWKSet | None, key_id: str) -> Any:
    if keys is None:
        return None
    return next((item.key for item in keys.keys if item.key_id == key_id), None)


class OidcIdentityProvider(IdentityProviderPort):
    """Authenticates a person by the access token their sign-in client sent.

    Beyond the signature, issuer, audience and expiry, the token must:
    - carry a `typ` claim, when it has one, naming an access token
      (`access_token_types`, `Bearer` by default), so a Keycloak ID or refresh
      token sent as a bearer token is refused;
    - name one of `authorized_parties` as `azp`, when any are configured, so a
      token issued to another client (a service, or another application sharing
      the audience) is refused.

    Time claims allow `leeway_seconds` of clock difference with the issuer.
    """

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
        jwks_retry_seconds: float = 30.0,
        roles_claim: str = "roles",
        leeway_seconds: float = DEFAULT_LEEWAY_SECONDS,
        access_token_types: Collection[str] = ("Bearer",),
        authorized_parties: Collection[str] = (),
    ) -> None:
        if leeway_seconds < 0:
            raise ValueError("The token leeway must not be negative.")
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._algorithms = allowed_algorithms
        self._roles_claim = roles_claim
        self._leeway = leeway_seconds
        self._token_types = frozenset(
            item.strip().lower() for item in access_token_types if item.strip()
        )
        self._parties = frozenset(item.strip() for item in authorized_parties if item.strip())
        self._keys = OidcSigningKeys(
            issuer,
            client,
            monotonic_seconds,
            jwks_ttl_seconds=jwks_ttl_seconds,
            unknown_key_ttl_seconds=unknown_key_ttl_seconds,
            unknown_key_cache_size=unknown_key_cache_size,
            retry_seconds=jwks_retry_seconds,
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
                leeway=self._leeway,
                options={"require": ["iss", "sub", "aud", "exp"]},
            )
        except AuthenticationRequiredError:
            raise
        except jwt.PyJWTError as exc:
            raise AuthenticationRequiredError("The bearer access token is invalid.") from exc
        token_type = claims.get("typ")
        if token_type is not None and str(token_type).strip().lower() not in self._token_types:
            raise AuthenticationRequiredError("The bearer token is not an access token.")
        if self._parties and str(claims.get("azp", "")).strip() not in self._parties:
            raise AuthenticationRequiredError("The access token was issued to another client.")
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
