"""The internal HTTP client: token, correlation, bounded retries, the circuit breaker, and
error mapping."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from smb_kernel.errors import ServiceResponseError, ServiceUnavailableError
from smb_kernel.http.client import CORRELATION_HEADER, CircuitBreaker, InternalHttpClient
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


class _Source:
    """A token source that hands out a new token each time it is invalidated."""

    def __init__(self) -> None:
        self.generation = 1
        self.invalidations = 0

    def __call__(self) -> str:
        return f"granted-{self.generation}"

    def invalidate(self) -> None:
        self.invalidations += 1
        self.generation += 1


def _sourced_client(source: Callable[[], str], handle: httpx.MockTransport) -> InternalHttpClient:
    return InternalHttpClient(
        "http://knowledge-api:8000",
        source,
        service="knowledge",
        http=httpx.Client(transport=handle),
        sleep=lambda _seconds: None,
    )


def test_a_token_source_is_asked_for_the_current_token_on_each_request() -> None:
    seen: list[str] = []
    tokens = iter(["first", "second"])

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={})

    client = _sourced_client(lambda: next(tokens), httpx.MockTransport(handle))
    client.get_json("/internal/a")
    client.get_json("/internal/b")
    assert seen == ["Bearer first", "Bearer second"]


def test_a_refused_granted_token_is_renewed_once_even_for_a_post() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer granted-1":
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, json={"ok": True})

    source = _Source()
    client = _sourced_client(source, httpx.MockTransport(handle))
    assert client.post_json("/internal/write", {}) == {"ok": True}
    assert seen == ["Bearer granted-1", "Bearer granted-2"]
    assert source.invalidations == 1


def test_a_token_refused_again_after_renewal_is_reported() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "The service token is not recognised."})

    source = _Source()
    client = _sourced_client(source, httpx.MockTransport(handle))
    with pytest.raises(ServiceResponseError) as raised:
        client.get_json("/internal/a")
    assert raised.value.status_code == 401
    assert source.invalidations == 1


def test_a_shared_token_is_not_renewed_on_401() -> None:
    calls: list[int] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"detail": "no"})

    client, _ = _client(httpx.MockTransport(handle))
    with pytest.raises(ServiceResponseError):
        client.get_json("/internal/a")
    assert len(calls) == 1


def test_an_empty_token_from_a_source_is_unavailable_not_sent() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("nothing is sent without a token")

    client = _sourced_client(lambda: " ", httpx.MockTransport(handle))
    with pytest.raises(ServiceUnavailableError):
        client.get_json("/internal/a")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _guarded(
    handle: httpx.MockTransport, clock: _Clock, token: str | Callable[[], str] = TOKEN
) -> InternalHttpClient:
    return InternalHttpClient(
        "http://knowledge-api:8000",
        token,
        service="knowledge",
        retries=0,
        http=httpx.Client(transport=handle),
        sleep=lambda _seconds: None,
        breaker=CircuitBreaker(failure_threshold=2, open_seconds=30, monotonic_seconds=clock),
    )


def test_repeated_unavailability_opens_the_circuit_until_a_test_call_succeeds() -> None:
    clock = _Clock()
    answers = [503, 503, 200]
    calls: list[int] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(answers.pop(0), json={})

    client = _guarded(httpx.MockTransport(handle), clock)
    for _ in range(2):
        with pytest.raises(ServiceUnavailableError):
            client.get_json("/internal/a")
    with pytest.raises(ServiceUnavailableError, match="paused after repeated failures"):
        client.get_json("/internal/a")
    assert len(calls) == 2  # Refused without a request.

    clock.now = 30
    assert client.get_json("/internal/a") == {}
    assert len(calls) == 3


def test_a_failed_test_call_opens_the_circuit_for_another_pause() -> None:
    clock = _Clock()
    breaker = CircuitBreaker(failure_threshold=1, open_seconds=30, monotonic_seconds=clock)
    states: list[str] = []
    breaker.failed()
    states.append(breaker.state)

    clock.now = 30
    states.append(breaker.state)
    assert breaker.allow()
    assert not breaker.allow()  # One test call at a time.
    breaker.failed()
    states.append(breaker.state)
    clock.now = 59
    assert not breaker.allow()
    clock.now = 60
    assert breaker.allow()
    breaker.succeeded()
    states.append(breaker.state)

    assert states == ["open", "half_open", "open", "closed"]


def test_a_refusal_shows_the_peer_is_up_and_resets_the_count() -> None:
    clock = _Clock()
    answers = [503, 404, 503]

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(answers.pop(0), json={"detail": "x"})

    client = _guarded(httpx.MockTransport(handle), clock)
    with pytest.raises(ServiceUnavailableError):
        client.get_json("/internal/a")
    with pytest.raises(ServiceResponseError):
        client.get_json("/internal/a")
    with pytest.raises(ServiceUnavailableError, match="unavailable \\(503\\)"):
        client.get_json("/internal/a")


def test_failed_token_grants_count_and_an_open_circuit_stops_asking_for_tokens() -> None:
    clock = _Clock()
    grants: list[int] = []

    def refused_grant() -> str:
        grants.append(1)
        raise ServiceUnavailableError("The identity provider could not be reached.")

    def handle(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("nothing is sent without a token")

    client = _guarded(httpx.MockTransport(handle), clock, refused_grant)
    for _ in range(3):
        with pytest.raises(ServiceUnavailableError):
            client.get_json("/internal/a")
    assert len(grants) == 2


def test_a_call_that_ends_unexpectedly_frees_the_test_slot() -> None:
    clock = _Clock()
    breaker = CircuitBreaker(failure_threshold=1, open_seconds=30, monotonic_seconds=clock)
    breaker.failed()
    clock.now = 30

    def handle(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("bug")

    client = InternalHttpClient(
        "http://knowledge-api:8000",
        TOKEN,
        service="knowledge",
        http=httpx.Client(transport=httpx.MockTransport(handle)),
        breaker=breaker,
    )
    with pytest.raises(RuntimeError):
        client.get_json("/internal/a")
    assert breaker.allow()


@pytest.mark.parametrize("threshold,pause", [(0, 30.0), (1, 0.0)])
def test_a_breaker_needs_a_threshold_and_a_pause(threshold: int, pause: float) -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=threshold, open_seconds=pause)
