"""The base every service-to-service adapter builds on.

It owns the mechanics only: the service token (a shared secret, or a callable
such as `ClientCredentialsTokenSource` that returns the current one), timeouts,
bounded retries with backoff, a circuit breaker, forwarding the correlation ID,
and turning transport failures into kernel errors. Request and response models
belong to the calling adapter, which validates what comes back before handing it
to its domain.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any, Literal

import httpx

from smb_kernel.errors import ServiceResponseError, ServiceUnavailableError
from smb_kernel.observability.correlation import current_correlation_id

CORRELATION_HEADER = "X-Request-ID"
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


class CircuitBreaker:
    """Stops calling a peer that keeps failing, and tries it again after a pause.

    After `failure_threshold` calls in a row fail with `ServiceUnavailableError`
    (unreachable, 5xx or throttled after retries, or no token granted), the
    circuit opens: calls fail at once, without touching the network or the
    issuer, for `open_seconds`. Then one call at a time is let through to test
    the peer: success closes the circuit, failure opens it for another pause.

    A peer that answers, even with a 4xx refusal, counts as up. Clients that call
    the same peer can share one breaker.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        open_seconds: float = 30.0,
        monotonic_seconds: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1 or open_seconds <= 0:
            raise ValueError("A circuit breaker needs a positive threshold and pause.")
        self._threshold = failure_threshold
        self._open_seconds = open_seconds
        self._monotonic = monotonic_seconds
        self._failures = 0
        self._opened_until = float("-inf")
        self._probing = False
        self._lock = Lock()

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        with self._lock:
            if self._failures < self._threshold:
                return "closed"
            return "open" if self._monotonic() < self._opened_until else "half_open"

    def allow(self) -> bool:
        """Whether a call may go ahead now; in half-open state, only one at a time."""
        with self._lock:
            if self._failures < self._threshold:
                return True
            if self._probing or self._monotonic() < self._opened_until:
                return False
            self._probing = True
            return True

    def succeeded(self) -> None:
        with self._lock:
            self._failures = 0
            self._probing = False

    def failed(self) -> None:
        with self._lock:
            self._failures += 1
            self._probing = False
            if self._failures >= self._threshold:
                self._opened_until = self._monotonic() + self._open_seconds

    def abandoned(self) -> None:
        """A call ended without saying anything about the peer; free its test slot."""
        with self._lock:
            self._probing = False


class InternalHttpClient:
    """Calls one peer service.

    Every client has a circuit breaker; without one passed in, it gets its own
    with the defaults.
    """

    def __init__(
        self,
        base_url: str,
        token: str | Callable[[], str],
        *,
        service: str,
        timeout_seconds: float = 10.0,
        retries: int = 2,
        backoff_seconds: float = 0.25,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        if not base_url.strip() or (isinstance(token, str) and not token.strip()):
            raise ValueError("An internal client needs a base URL and a service token.")
        if retries < 0:
            raise ValueError("Retries must not be negative.")
        self.service = service
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._retries = retries
        self._backoff = backoff_seconds
        self._http = http or httpx.Client()
        self._sleep = sleep
        self._breaker = breaker or CircuitBreaker()

    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params, retry=True)

    def post_json(self, path: str, body: Any, *, idempotent: bool = False) -> Any:
        """POST `body`. Only an idempotent POST is retried, because a retry after
        a lost response would otherwise repeat the side effect."""
        return self._request("POST", path, json=body, retry=idempotent)

    def _request(self, method: str, path: str, *, retry: bool, **kwargs: Any) -> Any:
        if not self._breaker.allow():
            raise ServiceUnavailableError(
                f"The {self.service} service is unavailable; calls are paused after "
                "repeated failures."
            )
        try:
            result = self._attempts(method, path, retry=retry, **kwargs)
        except ServiceUnavailableError:
            self._breaker.failed()
            raise
        except ServiceResponseError:
            # The peer answered: it is up, whatever it thought of the request.
            self._breaker.succeeded()
            raise
        except BaseException:
            self._breaker.abandoned()
            raise
        self._breaker.succeeded()
        return result

    def _attempts(self, method: str, path: str, *, retry: bool, **kwargs: Any) -> Any:
        url = f"{self._base_url}/{path.lstrip('/')}"
        headers = {"Accept": "application/json"}
        correlation_id = current_correlation_id()
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        attempts = 1 + (self._retries if retry else 0)
        renewed = False
        attempt = 0
        while attempt < attempts:
            attempt += 1
            headers["Authorization"] = f"Bearer {self._current_token()}"
            try:
                response = self._http.request(
                    method, url, headers=headers, timeout=self._timeout, **kwargs
                )
            except httpx.HTTPError as exc:
                if attempt < attempts:
                    self._sleep(self._backoff * 2 ** (attempt - 1))
                    continue
                raise ServiceUnavailableError(
                    f"The {self.service} service could not be reached."
                ) from exc
            if response.status_code in _RETRYABLE_STATUS and attempt < attempts:
                self._sleep(self._backoff * 2 ** (attempt - 1))
                continue
            invalidate = getattr(self._token, "invalidate", None)
            if response.status_code == 401 and callable(invalidate) and not renewed:
                # The guard refused the token before the route ran, so sending
                # the request again with a freshly granted one repeats nothing.
                invalidate()
                renewed = True
                attempt -= 1
                continue
            return self._decode(response)
        raise AssertionError("unreachable")  # pragma: no cover

    def _current_token(self) -> str:
        token = self._token if isinstance(self._token, str) else self._token()
        if not token.strip():
            raise ServiceUnavailableError(f"No service token is available for {self.service}.")
        return token

    def _decode(self, response: httpx.Response) -> Any:
        if response.status_code >= 500 or response.status_code in _RETRYABLE_STATUS:
            raise ServiceUnavailableError(
                f"The {self.service} service is unavailable ({response.status_code})."
            )
        if response.status_code >= 400:
            raise ServiceResponseError(response.status_code, _detail(response))
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ServiceUnavailableError(
                f"The {self.service} service returned a body that is not JSON."
            ) from exc


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return detail if isinstance(detail, str) else response.text[:500]
