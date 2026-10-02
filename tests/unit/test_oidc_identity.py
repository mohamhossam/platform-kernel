"""Generic OIDC discovery, JWKS, and bearer-token validation tests."""

from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from smb_kernel.errors import (
    AuthenticationRequiredError,
    IdentityProviderUnavailableError,
)
from smb_kernel.identity.oidc import OidcIdentityProvider
from smb_kernel.identity.ports import IdentityCredential

ISSUER = "https://identity.example.test"
AUDIENCE = "requirement-api"


def _key(key_id: str) -> tuple[RSAPrivateKey, dict[str, object]]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    jwk["kid"] = key_id
    jwk["use"] = "sig"
    return private, jwk


def _token(
    private: RSAPrivateKey,
    key_id: str,
    **overrides: object,
) -> str:
    claims: dict[str, object] = {
        "iss": ISSUER,
        "sub": "provider-user-42",
        "aud": AUDIENCE,
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "name": "Noura Reviewer",
        "email": "noura@example.test",
    }
    claims.update(overrides)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": key_id})


def _provider(jwks: list[dict[str, object]]) -> OidcIdentityProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/keys"})
        return httpx.Response(200, json={"keys": jwks})

    return OidcIdentityProvider(
        ISSUER,
        AUDIENCE,
        ("RS256",),
        httpx.Client(transport=httpx.MockTransport(handler)),
        lambda: 0.0,
    )


def test_valid_token_maps_to_stable_opaque_actor_without_claim_leakage() -> None:
    private, public = _key("primary")
    provider = _provider([public])

    actor = provider.authenticate(IdentityCredential(_token(private, "primary")))
    repeated = provider.authenticate(IdentityCredential(_token(private, "primary")))

    assert actor == repeated
    assert actor.id.value != "provider-user-42"
    assert actor.display_name == "Noura Reviewer"
    assert actor.email == "noura@example.test"


@pytest.mark.parametrize(
    "changes",
    [
        {"exp": datetime.now(UTC) - timedelta(minutes=1)},
        {"iss": "https://attacker.example.test"},
        {"aud": "different-api"},
        {"sub": ""},
    ],
)
def test_expired_or_invalid_claims_are_rejected(changes: dict[str, object]) -> None:
    private, public = _key("primary")
    provider = _provider([public])

    with pytest.raises(AuthenticationRequiredError):
        provider.authenticate(IdentityCredential(_token(private, "primary", **changes)))


def test_missing_token_signature_and_algorithm_failures_are_rejected() -> None:
    private, public = _key("primary")
    attacker, _ = _key("attacker")
    provider = _provider([public])

    with pytest.raises(AuthenticationRequiredError):
        provider.authenticate(IdentityCredential())
    with pytest.raises(AuthenticationRequiredError):
        provider.authenticate(IdentityCredential(_token(attacker, "primary")))
    unsafe = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "x",
            "aud": AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        "test-secret-that-is-at-least-32-bytes",
        algorithm="HS256",
        headers={"kid": "primary"},
    )
    with pytest.raises(AuthenticationRequiredError, match="disallowed"):
        provider.authenticate(IdentityCredential(unsafe))


def test_unknown_key_triggers_one_jwks_refresh_for_rotation() -> None:
    _, old_public = _key("old")
    new_private, new_public = _key("new")
    loads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal loads
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/keys"})
        loads += 1
        return httpx.Response(200, json={"keys": [old_public] if loads == 1 else [new_public]})

    provider = OidcIdentityProvider(
        ISSUER,
        AUDIENCE,
        ("RS256",),
        httpx.Client(transport=httpx.MockTransport(handler)),
        lambda: 0.0,
    )
    assert provider.authenticate(IdentityCredential(_token(new_private, "new"))).display_name
    assert loads == 2


def test_discovery_and_jwks_unavailability_are_explicit_503_errors() -> None:
    failing = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(503)))
    provider = OidcIdentityProvider(ISSUER, AUDIENCE, ("RS256",), failing, lambda: 0.0)
    private, _ = _key("primary")
    with pytest.raises(IdentityProviderUnavailableError):
        provider.authenticate(IdentityCredential(_token(private, "primary")))

    discovery_only = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(
                    200,
                    json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/keys"},
                )
                if request.url.path.endswith("openid-configuration")
                else httpx.Response(503)
            )
        )
    )
    provider = OidcIdentityProvider(ISSUER, AUDIENCE, ("RS256",), discovery_only, lambda: 0.0)
    private, _ = _key("primary")
    with pytest.raises(IdentityProviderUnavailableError):
        provider.authenticate(IdentityCredential(_token(private, "primary")))


@pytest.mark.parametrize(
    "discovery,jwks",
    [
        ([], {"keys": []}),
        ({"issuer": "https://other.test", "jwks_uri": f"{ISSUER}/keys"}, {"keys": []}),
        ({"issuer": ISSUER, "jwks_uri": "http://identity.test/keys"}, {"keys": []}),
        ({"issuer": ISSUER, "jwks_uri": f"{ISSUER}/keys"}, {"keys": "invalid"}),
    ],
)
def test_malformed_discovery_and_jwks_shapes_are_explicit(discovery: object, jwks: object) -> None:
    private, _ = _key("primary")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=discovery if request.url.path.endswith("openid-configuration") else jwks,
        )

    provider = OidcIdentityProvider(
        ISSUER,
        AUDIENCE,
        ("RS256",),
        httpx.Client(transport=httpx.MockTransport(handler)),
        lambda: 0.0,
    )
    with pytest.raises(IdentityProviderUnavailableError):
        provider.authenticate(IdentityCredential(_token(private, "primary")))


def test_unknown_key_negative_cache_prevents_refresh_storms() -> None:
    private, _ = _key("missing")
    _, available = _key("available")
    jwks_loads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal jwks_loads
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/keys"})
        jwks_loads += 1
        return httpx.Response(200, json={"keys": [available]})

    provider = OidcIdentityProvider(
        ISSUER,
        AUDIENCE,
        ("RS256",),
        httpx.Client(transport=httpx.MockTransport(handler)),
        lambda: 10.0,
    )
    token = IdentityCredential(_token(private, "missing"))
    with pytest.raises(AuthenticationRequiredError):
        provider.authenticate(token)
    with pytest.raises(AuthenticationRequiredError):
        provider.authenticate(token)

    assert jwks_loads == 2


def test_jwks_cache_refreshes_after_configured_ttl() -> None:
    private, public = _key("primary")
    now = [0.0]
    jwks_loads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal jwks_loads
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/keys"})
        jwks_loads += 1
        return httpx.Response(200, json={"keys": [public]})

    provider = OidcIdentityProvider(
        ISSUER,
        AUDIENCE,
        ("RS256",),
        httpx.Client(transport=httpx.MockTransport(handler)),
        lambda: now[0],
        jwks_ttl_seconds=900,
    )
    credential = IdentityCredential(_token(private, "primary"))
    provider.authenticate(credential)
    now[0] = 899
    provider.authenticate(credential)
    now[0] = 900
    provider.authenticate(credential)

    assert jwks_loads == 2


def test_different_unknown_keys_share_one_refresh_window_per_issuer() -> None:
    first_private, _ = _key("missing-one")
    second_private, _ = _key("missing-two")
    _, available = _key("available")
    jwks_loads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal jwks_loads
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/keys"})
        jwks_loads += 1
        return httpx.Response(200, json={"keys": [available]})

    provider = OidcIdentityProvider(
        ISSUER,
        AUDIENCE,
        ("RS256",),
        httpx.Client(transport=httpx.MockTransport(handler)),
        lambda: 10.0,
    )
    with pytest.raises(AuthenticationRequiredError):
        provider.authenticate(IdentityCredential(_token(first_private, "missing-one")))
    with pytest.raises(AuthenticationRequiredError):
        provider.authenticate(IdentityCredential(_token(second_private, "missing-two")))

    assert jwks_loads == 2
