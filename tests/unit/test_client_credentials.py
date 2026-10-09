"""Service access tokens granted through the client-credentials grant."""

from __future__ import annotations

import base64
from urllib.parse import parse_qs

import httpx
import pytest

from smb_kernel.errors import ServiceUnavailableError
from smb_kernel.http.client_credentials import ClientCredentialsTokenSource

ISSUER = "https://identity.example.test/realms/platform"
TOKEN_ENDPOINT = f"{ISSUER}/protocol/openid-connect/token"


class _Issuer:
    def __init__(self, *, expires_in: object = 300, token_endpoint: str = TOKEN_ENDPOINT) -> None:
        self.expires_in = expires_in
        self.token_endpoint = token_endpoint
        self.grants: list[httpx.Request] = []
        self.discoveries = 0
        self.status = 200
        self.body: object | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            self.discoveries += 1
            return httpx.Response(
                200, json={"issuer": ISSUER, "token_endpoint": self.token_endpoint}
            )
        self.grants.append(request)
        if self.body is not None:
            return httpx.Response(self.status, json=self.body)
        return httpx.Response(
            self.status,
            json={
                "access_token": f"granted-{len(self.grants)}",
                "token_type": "Bearer",
                "expires_in": self.expires_in,
            },
        )


def _source(issuer: _Issuer, clock: list[float], **options: object) -> ClientCredentialsTokenSource:
    return ClientCredentialsTokenSource(
        ISSUER + "/",
        "knowledge-service",
        "s3cret",
        client=httpx.Client(transport=httpx.MockTransport(issuer)),
        monotonic_seconds=lambda: clock[0],
        **options,  # type: ignore[arg-type]
    )


def test_grants_with_the_client_credentials_and_reuses_the_token() -> None:
    issuer = _Issuer()
    clock = [0.0]
    source = _source(issuer, clock)

    assert source() == "granted-1"
    clock[0] = 269.0
    assert source() == "granted-1"

    request = issuer.grants[0]
    assert str(request.url) == TOKEN_ENDPOINT
    assert parse_qs(request.content.decode()) == {"grant_type": ["client_credentials"]}
    expected = base64.b64encode(b"knowledge-service:s3cret").decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert issuer.discoveries == 1


def test_renews_the_token_before_it_expires() -> None:
    issuer = _Issuer()
    clock = [0.0]
    source = _source(issuer, clock)
    source()
    clock[0] = 270.0
    assert source() == "granted-2"
    assert issuer.discoveries == 1


def test_a_short_lived_token_is_kept_for_half_its_life() -> None:
    issuer = _Issuer(expires_in=20)
    clock = [0.0]
    source = _source(issuer, clock)
    source()
    clock[0] = 9.0
    assert source() == "granted-1"
    clock[0] = 10.0
    assert source() == "granted-2"


def test_invalidate_forces_a_new_grant() -> None:
    issuer = _Issuer()
    source = _source(issuer, [0.0])
    source()
    source.invalidate()
    assert source() == "granted-2"


def test_sends_the_scope_when_one_is_configured() -> None:
    issuer = _Issuer()
    _source(issuer, [0.0], scope="internal")()
    assert parse_qs(issuer.grants[0].content.decode())["scope"] == ["internal"]


def test_refuses_a_token_endpoint_that_is_not_https() -> None:
    issuer = _Issuer(token_endpoint="http://identity.example.test/token")
    with pytest.raises(ServiceUnavailableError, match="HTTPS"):
        _source(issuer, [0.0])()
    assert issuer.grants == []


@pytest.mark.parametrize(
    "body",
    [
        {"token_type": "Bearer", "expires_in": 300},
        {"access_token": "x", "token_type": "mac", "expires_in": 300},
        {"access_token": "x", "token_type": "Bearer"},
        {"access_token": "x", "token_type": "Bearer", "expires_in": 0},
        {"access_token": "x", "token_type": "Bearer", "expires_in": True},
        ["not", "an", "object"],
    ],
)
def test_an_unusable_token_response_is_unavailable(body: object) -> None:
    issuer = _Issuer()
    issuer.body = body
    with pytest.raises(ServiceUnavailableError, match="usable bearer token"):
        _source(issuer, [0.0])()


def test_a_refused_grant_is_unavailable_and_not_cached() -> None:
    issuer = _Issuer()
    issuer.status = 401
    issuer.body = {"error": "invalid_client"}
    source = _source(issuer, [0.0])
    with pytest.raises(ServiceUnavailableError, match="401"):
        source()
    issuer.status = 200
    issuer.body = None
    assert source() == "granted-2"


def test_an_unreachable_issuer_is_unavailable() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    source = ClientCredentialsTokenSource(
        ISSUER,
        "knowledge-service",
        "s3cret",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    with pytest.raises(ServiceUnavailableError):
        source()


@pytest.mark.parametrize(
    ("issuer", "client_id", "secret"), [("", "a", "b"), (ISSUER, " ", "b"), (ISSUER, "a", "")]
)
def test_incomplete_configuration_is_refused(issuer: str, client_id: str, secret: str) -> None:
    with pytest.raises(ValueError):
        ClientCredentialsTokenSource(issuer, client_id, secret)
