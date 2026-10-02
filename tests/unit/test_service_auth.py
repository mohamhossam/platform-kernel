"""Internal routes refuse requests without a recognised service token."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from smb_kernel.errors import ServiceAuthenticationError
from smb_kernel.http.service_auth import CALLER_SCOPE_KEY, InternalRouteGuard, ServiceTokenVerifier

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
