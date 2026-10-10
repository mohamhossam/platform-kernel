"""Client spans for outbound HTTP, and the IDs a log line carries while a span is open.

Only the OpenTelemetry API is used here, so nothing is recorded until an
application configures a provider (`smb_kernel.observability.tracing_setup`).

Like the metrics, spans carry no requirement text or provider payloads: an
outbound span records the method, the peer's host and the status, never the
path, query, headers or body. W3C `traceparent` is sent only by a transport
built with `propagate=True`, which an application keeps for its own peers, so a
trace ID never reaches a model provider or an identity provider.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping
from functools import partial
from typing import TypeVar

import httpx
import httpx2
from opentelemetry.trace import SpanKind, Status, StatusCode, TracerProvider, get_current_span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_INSTRUMENTATION = "smb_kernel.http"
# Only the trace context, never baggage: nothing else of the caller's crosses.
_PROPAGATOR = TraceContextTextMapPropagator()
_Response = TypeVar("_Response", httpx.Response, httpx2.Response)


def current_trace_ids() -> tuple[str, str] | None:
    """The open span's trace and span IDs in hex, or None when none is recording."""
    context = get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")


class _Carrier(MutableMapping[str, str]):
    """Writes injected headers straight onto the outgoing request."""

    def __init__(self, headers: httpx.Headers | httpx2.Headers) -> None:
        self._headers = headers

    def __getitem__(self, key: str) -> str:
        return str(self._headers[key])

    def __setitem__(self, key: str, value: str) -> None:
        self._headers[key] = value

    def __delitem__(self, key: str) -> None:
        del self._headers[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._headers.keys())

    def __len__(self) -> int:
        return len(self._headers)


class _Outbound:
    def __init__(self, tracer_provider: TracerProvider, peer: str, propagate: bool) -> None:
        self._tracer = tracer_provider.get_tracer(_INSTRUMENTATION)
        self._peer = peer
        self._propagate = propagate

    def send(
        self,
        method: str,
        host: str,
        port: int | None,
        headers: httpx.Headers | httpx2.Headers,
        call: Callable[[], _Response],
    ) -> _Response:
        attributes: dict[str, str | int] = {
            "http.request.method": method,
            "server.address": host,
            "peer.service": self._peer,
        }
        if port is not None:
            attributes["server.port"] = port
        with self._tracer.start_as_current_span(
            f"HTTP {method}",
            kind=SpanKind.CLIENT,
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            if self._propagate:
                _PROPAGATOR.inject(_Carrier(headers))
            try:
                response = call()
            except Exception as exc:
                # The exception's type only: its message may quote a URL or a payload.
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR))
                raise
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 500:
                span.set_attribute("error.type", str(response.status_code))
                span.set_status(Status(StatusCode.ERROR))
            return response


class TracedTransport(httpx.BaseTransport):
    """A client span around every request sent through an `httpx` client.

    `peer` names the service called (a small fixed set, like a metrics label).
    With `propagate=True` the request also carries `traceparent`.
    """

    def __init__(
        self,
        inner: httpx.BaseTransport,
        *,
        tracer_provider: TracerProvider,
        peer: str,
        propagate: bool = False,
    ) -> None:
        self._inner = inner
        self._outbound = _Outbound(tracer_provider, peer, propagate)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._outbound.send(
            request.method,
            request.url.host,
            request.url.port,
            request.headers,
            partial(self._inner.handle_request, request),
        )

    def close(self) -> None:
        self._inner.close()


class TracedTransport2(httpx2.BaseTransport):
    """The same, for the `httpx2` client inside the OpenAI SDK."""

    def __init__(
        self,
        inner: httpx2.BaseTransport,
        *,
        tracer_provider: TracerProvider,
        peer: str,
        propagate: bool = False,
    ) -> None:
        self._inner = inner
        self._outbound = _Outbound(tracer_provider, peer, propagate)

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        return self._outbound.send(
            request.method,
            request.url.host,
            request.url.port,
            request.headers,
            partial(self._inner.handle_request, request),
        )

    def close(self) -> None:
        self._inner.close()
