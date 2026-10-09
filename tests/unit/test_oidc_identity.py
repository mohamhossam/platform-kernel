"""Generic OIDC discovery, JWKS, and bearer-token validation tests."""

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from smb_kernel.errors import (
    AuthenticationRequiredError,
    IdentityProviderUnavailableError,
)
from smb_kernel.identity.oidc import OidcIdentityProvider, OidcSigningKeys
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


def _provider(jwks: list[dict[str, object]], **options: Any) -> OidcIdentityProvider:
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
        **options,
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
        {"exp": datetime.now(UTC) - timedelta(minutes=5)},
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


def test_a_minute_of_clock_difference_with_the_issuer_is_tolerated() -> None:
    private, public = _key("primary")
    just_expired = _token(private, "primary", exp=datetime.now(UTC) - timedelta(seconds=30))
    issued_ahead = _token(private, "primary", iat=datetime.now(UTC) + timedelta(seconds=30))

    provider = _provider([public])
    provider.authenticate(IdentityCredential(just_expired))
    provider.authenticate(IdentityCredential(issued_ahead))
    with pytest.raises(AuthenticationRequiredError):
        _provider([public], leeway_seconds=0).authenticate(IdentityCredential(just_expired))
    with pytest.raises(ValueError):
        _provider([public], leeway_seconds=-1)


@pytest.mark.parametrize("token_type", ["ID", "Refresh", "Logout"])
def test_a_token_that_is_not_an_access_token_is_refused(token_type: str) -> None:
    private, public = _key("primary")

    with pytest.raises(AuthenticationRequiredError, match="not an access token"):
        _provider([public]).authenticate(
            IdentityCredential(_token(private, "primary", typ=token_type))
        )


@pytest.mark.parametrize("token_type", ["Bearer", "bearer", None])
def test_an_access_token_or_an_untyped_token_is_accepted(token_type: str | None) -> None:
    private, public = _key("primary")
    claims = {} if token_type is None else {"typ": token_type}

    _provider([public]).authenticate(IdentityCredential(_token(private, "primary", **claims)))


def test_authorized_parties_refuse_tokens_issued_to_another_client() -> None:
    private, public = _key("primary")
    provider = _provider([public], authorized_parties=("requirement-spa",))

    provider.authenticate(IdentityCredential(_token(private, "primary", azp="requirement-spa")))
    for other in ({"azp": "requirement-service"}, {}):
        with pytest.raises(AuthenticationRequiredError, match="another client"):
            provider.authenticate(IdentityCredential(_token(private, "primary", **other)))
    # Without authorized parties, any client's token for the audience is accepted.
    _provider([public]).authenticate(
        IdentityCredential(_token(private, "primary", azp="requirement-service"))
    )


class _Issuer:
    """A mock issuer whose key endpoint can fail, or hold a request until released."""

    def __init__(self, jwks: list[dict[str, object]]) -> None:
        self.jwks = jwks
        self.loads = 0
        self.failing = False
        self.hold = threading.Event()
        self.hold.set()
        self.holding = threading.Event()

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/keys"})
        self.loads += 1
        # The answer is fixed when the request arrives, however long it is held.
        jwks, failing = self.jwks, self.failing
        self.holding.set()
        assert self.hold.wait(5)
        if failing:
            return httpx.Response(503)
        return httpx.Response(200, json={"keys": jwks})


def test_a_failed_scheduled_reload_serves_the_last_good_keys_and_retries_later() -> None:
    _, public = _key("primary")
    issuer = _Issuer([public])
    now = [0.0]
    keys = OidcSigningKeys(
        ISSUER, issuer.client(), lambda: now[0], jwks_ttl_seconds=900, retry_seconds=30
    )
    assert keys.key("primary") is not None

    issuer.failing = True
    now[0] = 900
    assert keys.key("primary") is not None
    now[0] = 910
    assert keys.key("primary") is not None
    assert issuer.loads == 2  # No new attempt before the retry interval.
    now[0] = 930
    assert keys.key("primary") is not None
    assert issuer.loads == 3

    issuer.failing = False
    now[0] = 960
    keys.key("primary")
    now[0] = 1000
    keys.key("primary")
    assert issuer.loads == 4  # Back on the normal reload interval.


def test_callers_with_cached_keys_are_not_held_up_by_a_reload() -> None:
    _, public = _key("primary")
    issuer = _Issuer([public])
    now = [0.0]
    keys = OidcSigningKeys(ISSUER, issuer.client(), lambda: now[0], jwks_ttl_seconds=900)
    keys.key("primary")
    now[0] = 900
    issuer.hold.clear()
    issuer.holding.clear()
    reloading = threading.Thread(target=keys.key, args=("primary",))
    reloading.start()
    try:
        assert issuer.holding.wait(5)
        # The reload is under way and held: another caller is answered from the cache.
        assert keys.key("primary") is not None
    finally:
        issuer.hold.set()
        reloading.join(5)
    assert issuer.loads == 2


def test_callers_on_a_cold_cache_share_one_fetch() -> None:
    _, public = _key("primary")
    issuer = _Issuer([public])
    keys = OidcSigningKeys(ISSUER, issuer.client(), lambda: 0.0)
    issuer.hold.clear()
    results: list[object] = []
    callers = [
        threading.Thread(target=lambda: results.append(keys.key("primary"))) for _ in range(4)
    ]
    for caller in callers:
        caller.start()
    assert issuer.holding.wait(5)
    issuer.hold.set()
    for caller in callers:
        caller.join(5)

    assert len(results) == 4
    assert issuer.loads == 1


def test_an_unknown_key_that_cannot_be_looked_up_is_unavailable_not_invalid() -> None:
    _, public = _key("primary")
    issuer = _Issuer([public])
    keys = OidcSigningKeys(ISSUER, issuer.client(), lambda: 0.0)
    keys.key("primary")
    issuer.failing = True

    with pytest.raises(IdentityProviderUnavailableError):
        keys.key("rotated")
    # Inside the unknown-key window, the same answer, without another fetch.
    with pytest.raises(IdentityProviderUnavailableError):
        keys.key("rotated")
    assert issuer.loads == 2
    # A key the cache holds is still served.
    assert keys.key("primary") is not None


def test_a_key_lookup_does_not_settle_for_a_reload_that_began_before_it_asked() -> None:
    _, old_public = _key("old")
    _, new_public = _key("new")
    issuer = _Issuer([old_public])
    now = [0.0]
    keys = OidcSigningKeys(ISSUER, issuer.client(), lambda: now[0], jwks_ttl_seconds=900)
    keys.key("old")
    # A scheduled reload starts and is held, still answering with the old keys.
    now[0] = 900
    issuer.hold.clear()
    issuer.holding.clear()
    reloading = threading.Thread(target=keys.key, args=("old",))
    reloading.start()
    assert issuer.holding.wait(5)
    # Meanwhile the issuer rotates, and a token signed with the new key arrives.
    found: list[object] = []
    looking = threading.Thread(target=lambda: found.append(keys.key("new")))
    looking.start()
    issuer.jwks = [new_public]
    deadline = time.monotonic() + 5
    while keys._last_unknown_refresh != 900 and time.monotonic() < deadline:
        time.sleep(0.01)  # Until the lookup is waiting on the held reload.
    issuer.hold.set()
    reloading.join(5)
    looking.join(5)

    assert found and found[0] is not None
    assert issuer.loads == 3
