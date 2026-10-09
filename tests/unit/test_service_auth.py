"""Internal routes refuse requests without a recognised service token or granted token."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from smb_kernel.errors import IdentityProviderUnavailableError, ServiceAuthenticationError
from smb_kernel.http.service_auth import (
    CALLER_SCOPE_KEY,
    InternalRouteGuard,
    ServiceJwtVerifier,
    ServiceTokenVerifier,
    ServiceVerifierChain,
)
from smb_kernel.identity.oidc import OidcSigningKeys

REQUIREMENTS_TOKEN = "r" * 40
KNOWLEDGE_TOKEN = "k" * 40


def _app() -> TestClient:
    async def whoami(request: Request) -> JSONResponse:
        return JSONResponse({"caller": request.scope.get(CALLER_SCOPE_KEY)})

    app = Starlette(routes=[Route("/internal/whoami", whoami), Route("/public/whoami", whoami)])
    verifier = ServiceTokenVerifier(
        {"requirements": REQUIREMENTS_TOKEN, "knowledge": KNOWLEDGE_TOKEN}
    )
    app.add_middleware(InternalRouteGuard, verifier=verifier)
    return TestClient(app)


def test_a_valid_token_names_its_caller() -> None:
    response = _app().get(
        "/internal/whoami", headers={"Authorization": f"Bearer {KNOWLEDGE_TOKEN}"}
    )
    assert response.status_code == 200
    assert response.json() == {"caller": "knowledge"}


@pytest.mark.parametrize(
    "authorization", [None, "Bearer ", f"Basic {REQUIREMENTS_TOKEN}", "Bearer " + "x" * 40]
)
def test_internal_routes_refuse_missing_or_unknown_tokens(authorization: str | None) -> None:
    headers = {"Authorization": authorization} if authorization else {}
    response = _app().get("/internal/whoami", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "token" in response.json()["detail"]


def test_routes_outside_the_prefix_are_not_guarded() -> None:
    response = _app().get("/public/whoami")
    assert response.status_code == 200
    assert response.json() == {"caller": None}


def test_a_lookalike_prefix_is_not_guarded() -> None:
    verifier = ServiceTokenVerifier({"requirements": REQUIREMENTS_TOKEN})
    with pytest.raises(ServiceAuthenticationError):
        verifier.caller(None)
    app = Starlette(routes=[Route("/internalish", lambda _r: JSONResponse({}))])
    app.add_middleware(InternalRouteGuard, verifier=verifier)
    assert TestClient(app).get("/internalish").status_code == 200


@pytest.mark.parametrize("tokens", [{}, {"requirements": "short"}, {" ": "x" * 40}])
def test_weak_or_empty_configuration_is_refused(tokens: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        ServiceTokenVerifier(tokens)


# Service access tokens an issuer grants through the client-credentials grant.

ISSUER = "https://identity.example.test/realms/platform"
INTERNAL_AUDIENCE = "knowledge-internal"
CLIENTS = {"requirement-service": "requirements"}
_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(private: RSAPrivateKey = _PRIVATE, key_id: str = "k1") -> dict[str, object]:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    jwk.update({"kid": key_id, "use": "sig"})
    return {"keys": [jwk]}


def _keys(available: bool = True) -> OidcSigningKeys:
    def handle(request: httpx.Request) -> httpx.Response:
        if not available:
            return httpx.Response(503)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/certs"})
        return httpx.Response(200, json=_jwks())

    return OidcSigningKeys(ISSUER, httpx.Client(transport=httpx.MockTransport(handle)), lambda: 0.0)


def _service_token(private: RSAPrivateKey = _PRIVATE, **overrides: object) -> str:
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": ["account", INTERNAL_AUDIENCE],
        "azp": "requirement-service",
        "sub": "service-account-requirement-service",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    claims.update(overrides)
    claims = {name: value for name, value in claims.items() if value is not None}
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "k1"})


def _jwt_verifier(available: bool = True) -> ServiceJwtVerifier:
    return ServiceJwtVerifier(_keys(available), INTERNAL_AUDIENCE, CLIENTS)


def test_a_granted_service_token_names_its_caller() -> None:
    assert _jwt_verifier().caller(f"Bearer {_service_token()}") == "requirements"


def test_client_id_names_the_caller_when_azp_is_absent() -> None:
    token = _service_token(azp=None, client_id="requirement-service")
    assert _jwt_verifier().caller(f"Bearer {token}") == "requirements"


@pytest.mark.parametrize(
    "token",
    [
        _service_token(azp="requirement-spa"),
        _service_token(aud="requirement-api"),
        _service_token(iss="https://elsewhere.example.test"),
        _service_token(exp=datetime.now(UTC) - timedelta(minutes=5)),
        _service_token(exp=None),
        _service_token(rsa.generate_private_key(public_exponent=65537, key_size=2048)),
        "not-a-jwt",
        jwt.encode(
            {"iss": ISSUER, "aud": INTERNAL_AUDIENCE, "azp": "requirement-service"}, "s" * 40
        ),
    ],
    ids=[
        "a person's client",
        "another audience",
        "another issuer",
        "expired",
        "no expiry",
        "another key",
        "not a token",
        "symmetric",
    ],
)
def test_other_tokens_are_refused(token: str) -> None:
    with pytest.raises(ServiceAuthenticationError):
        _jwt_verifier().caller(f"Bearer {token}")


def test_a_token_just_past_its_expiry_is_accepted_within_the_clock_leeway() -> None:
    token = _service_token(exp=datetime.now(UTC) - timedelta(seconds=30))

    assert _jwt_verifier().caller(f"Bearer {token}") == "requirements"
    strict = ServiceJwtVerifier(_keys(), INTERNAL_AUDIENCE, CLIENTS, leeway_seconds=0)
    with pytest.raises(ServiceAuthenticationError):
        strict.caller(f"Bearer {token}")


def test_an_unreachable_issuer_is_reported_as_unavailable() -> None:
    with pytest.raises(IdentityProviderUnavailableError):
        _jwt_verifier(available=False).caller(f"Bearer {_service_token()}")


@pytest.mark.parametrize(
    ("audience", "clients", "algorithms"),
    [
        (" ", CLIENTS, ("RS256",)),
        (INTERNAL_AUDIENCE, {}, ("RS256",)),
        (INTERNAL_AUDIENCE, {"requirement-service": " "}, ("RS256",)),
        (INTERNAL_AUDIENCE, CLIENTS, ("HS256",)),
        (INTERNAL_AUDIENCE, CLIENTS, ()),
    ],
)
def test_weak_jwt_configuration_is_refused(
    audience: str, clients: dict[str, str], algorithms: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        ServiceJwtVerifier(_keys(), audience, clients, allowed_algorithms=algorithms)


def _chained_app(available: bool = True) -> TestClient:
    async def whoami(request: Request) -> JSONResponse:
        return JSONResponse({"caller": request.scope.get(CALLER_SCOPE_KEY)})

    app = Starlette(routes=[Route("/internal/whoami", whoami)])
    verifier = ServiceVerifierChain(
        ServiceTokenVerifier({"requirements": REQUIREMENTS_TOKEN}), _jwt_verifier(available)
    )
    app.add_middleware(InternalRouteGuard, verifier=verifier)
    return TestClient(app)


@pytest.mark.parametrize("token", [REQUIREMENTS_TOKEN, _service_token()])
def test_a_chain_accepts_a_shared_secret_or_a_granted_token(token: str) -> None:
    response = _chained_app().get("/internal/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"caller": "requirements"}


def test_a_chain_refuses_what_no_verifier_accepts() -> None:
    response = _chained_app().get("/internal/whoami", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_shared_secret_still_works_while_the_issuer_is_unreachable() -> None:
    client = _chained_app(available=False)
    shared = client.get(
        "/internal/whoami", headers={"Authorization": f"Bearer {REQUIREMENTS_TOKEN}"}
    )
    granted = client.get(
        "/internal/whoami", headers={"Authorization": f"Bearer {_service_token()}"}
    )
    assert shared.status_code == 200
    assert granted.status_code == 503
    assert "www-authenticate" not in granted.headers


def test_an_empty_chain_is_refused() -> None:
    with pytest.raises(ValueError):
        ServiceVerifierChain()
