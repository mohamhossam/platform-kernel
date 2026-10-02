"""The internal HTTP client: token, correlation, bounded retries, and error mapping."""

from __future__ import annotations

import httpx
import pytest

from smb_kernel.errors import ServiceResponseError, ServiceUnavailableError
from smb_kernel.http.client import CORRELATION_HEADER, InternalHttpClient
from smb_kernel.observability.correlation import correlation_scope

TOKEN = "t" * 40


def _client(
    handler: httpx.MockTransport, retries: int = 2
) -> tuple[InternalHttpClient, list[float]]:
    waits: list[float] = []
    client = InternalHttpClient(
        "http://knowledge-api:8000/",
        TOKEN,
        service="knowledge",
        retries=retries,
        backoff_seconds=0.1,
        http=httpx.Client(transport=handler),
        sleep=waits.append,
    )
    return client, waits


def test_sends_the_token_and_the_current_correlation_id() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client, _ = _client(httpx.MockTransport(handle))
    with correlation_scope("req-42"):
        assert client.get_json("/internal/events", {"after": 3}) == {"ok": True}

    request = seen[0]
    assert str(request.url) == "http://knowledge-api:8000/internal/events?after=3"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers[CORRELATION_HEADER] == "req-42"


def test_no_correlation_header_outside_a_unit_of_work() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    client, _ = _client(httpx.MockTransport(handle))
    assert client.get_json("/internal/ping") is None
    assert CORRELATION_HEADER not in seen[0].headers


def test_a_get_retries_unavailability_with_backoff_then_succeeds() -> None:
    answers = iter([httpx.Response(503), httpx.Response(429), httpx.Response(200, json=[1])])
    client, waits = _client(httpx.MockTransport(lambda _request: next(answers)))

    assert client.get_json("/internal/x") == [1]
    assert waits == [0.1, 0.2]


def test_a_get_gives_up_after_its_retries() -> None:
    calls = 0

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused")

    client, _ = _client(httpx.MockTransport(handle), retries=1)
    with pytest.raises(ServiceUnavailableError, match="knowledge service could not be reached"):
        client.get_json("/internal/x")
    assert calls == 2


def test_a_post_is_not_retried_unless_idempotent() -> None:
    calls = 0

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client, _ = _client(httpx.MockTransport(handle))
    with pytest.raises(ServiceUnavailableError):
        client.post_json("/internal/match", {"text": "x"})
    assert calls == 1

    with pytest.raises(ServiceUnavailableError):
        client.post_json("/internal/search", {"text": "x"}, idempotent=True)
    assert calls == 4


def test_a_refusal_carries_status_and_detail() -> None:
    client, _ = _client(
        httpx.MockTransport(lambda _r: httpx.Response(404, json={"detail": "No such document."}))
    )
    with pytest.raises(ServiceResponseError) as raised:
        client.get_json("/internal/library/documents/x")
    assert raised.value.status_code == 404
    assert raised.value.detail == "No such document."


def test_a_body_that_is_not_json_is_unusable() -> None:
    client, _ = _client(httpx.MockTransport(lambda _r: httpx.Response(200, text="<html>")))
    with pytest.raises(ServiceUnavailableError, match="not JSON"):
        client.get_json("/internal/x")


@pytest.mark.parametrize("base_url,token", [("", TOKEN), ("http://x", " ")])
def test_configuration_is_required(base_url: str, token: str) -> None:
    with pytest.raises(ValueError):
        InternalHttpClient(base_url, token, service="knowledge")
