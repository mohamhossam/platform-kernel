"""Optional OpenTelemetry tracing for a process: OTLP export, spans and SQL.

Needs the `tracing` extra. An application builds one `Tracing` at start, from
its settings, and hands it to whatever records spans; nothing is installed as a
global provider, so tests and processes never share one. With no endpoint the
application uses `NO_TRACING`, whose spans are never recorded or exported.

SQL spans carry the statement as written, with its `%s` placeholders, never the
parameter values, and nothing is appended to the SQL sent.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
)
from opentelemetry.trace import (
    Link,
    NoOpTracerProvider,
    Span,
    SpanKind,
    Status,
    StatusCode,
    TracerProvider,
    TraceState,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import Attributes

from smb_kernel.persistence.connector import DbConnection

_INSTRUMENTATION = "smb_kernel.tracing"
_TRACES_PATH = "/v1/traces"
_PROPAGATOR = TraceContextTextMapPropagator()
type SpanValue = str | int | float | bool


class RequestSpan:
    """The server span of one request, named once its route is known."""

    def __init__(self, span: Span, method: str) -> None:
        self._span = span
        self._method = method

    def finish(self, route: str, status_code: int) -> None:
        """Name the span by the route template (never the path) and record the status."""
        self._span.update_name(f"{self._method} {route}")
        self._span.set_attribute("http.route", route)
        self._span.set_attribute("http.response.status_code", status_code)
        if status_code >= 500:
            self._span.set_attribute("error.type", str(status_code))
            self._span.set_status(Status(StatusCode.ERROR))


class _RootSampler(Sampler):
    """Starts traces at the given ratio, but never from a lone client span.

    A background loop's poll queries and calls have no request or job around
    them; as roots they would become a trace every second, so they are dropped.
    """

    def __init__(self, ratio: float) -> None:
        self._ratio = TraceIdRatioBased(ratio)

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        if kind is SpanKind.CLIENT:
            return SamplingResult(Decision.DROP)
        return self._ratio.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        return f"RootSampler{{{self._ratio.get_description()}}}"


@dataclass(frozen=True)
class Tracing:
    """One process's tracer provider, and the spans and SQL instrumentation it backs."""

    tracer_provider: TracerProvider
    enabled: bool

    @contextmanager
    def span(self, name: str, attributes: Mapping[str, SpanValue] | None = None) -> Iterator[None]:
        """Record `name` around the block, as a child of any span already open.

        An exception ends the span in error, recording its type only: a message
        may quote requirement text.
        """
        tracer = self.tracer_provider.get_tracer(_INSTRUMENTATION)
        with tracer.start_as_current_span(
            name,
            attributes=dict(attributes or {}),
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield
            except Exception as exc:
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR))
                raise

    @contextmanager
    def request_span(
        self,
        method: str,
        headers: Mapping[str, str],
        attributes: Mapping[str, SpanValue] | None = None,
    ) -> Iterator[RequestSpan]:
        """The server span around one request, continuing a `traceparent` it arrived with.

        Only the trace context is read from `headers`. Call `finish` with the route
        template; the path and query are never recorded, since they carry IDs and
        search text.
        """
        tracer = self.tracer_provider.get_tracer(_INSTRUMENTATION)
        with tracer.start_as_current_span(
            method,
            context=_PROPAGATOR.extract(headers),
            kind=SpanKind.SERVER,
            attributes={"http.request.method": method, **(attributes or {})},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield RequestSpan(span, method)
            except Exception as exc:
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR))
                raise

    def instrument_connection(self, connection: DbConnection) -> None:
        """Trace every statement this connection runs; nothing when tracing is off.

        Suits `PooledPostgresConnector(configure=…)`, which runs on each new connection.
        """
        if self.enabled:
            PsycopgInstrumentor.instrument_connection(
                connection, tracer_provider=self.tracer_provider
            )

    def shutdown(self) -> None:
        """Export what is still buffered, then stop. Safe to call when tracing is off."""
        if isinstance(self.tracer_provider, SdkTracerProvider):
            self.tracer_provider.shutdown()


NO_TRACING = Tracing(NoOpTracerProvider(), enabled=False)


def configure_tracing(
    endpoint: str,
    *,
    service: str,
    version: str,
    sample_ratio: float = 1.0,
    exporter: SpanExporter | None = None,
) -> Tracing:
    """Export spans over OTLP/HTTP to `endpoint` (a collector's base URL).

    `sample_ratio` keeps that share of new traces; a request that arrives with a
    sampled trace context is always kept. A client span (SQL or an outbound call)
    with no span around it starts no trace. `exporter` replaces the OTLP exporter,
    for tests.
    """
    if not 0.0 <= sample_ratio <= 1.0:
        raise ValueError("sample_ratio must be between 0 and 1.")
    provider = SdkTracerProvider(
        resource=Resource.create({"service.name": service, "service.version": version}),
        sampler=ParentBased(_RootSampler(sample_ratio)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            exporter or OTLPSpanExporter(endpoint=endpoint.rstrip("/") + _TRACES_PATH)
        )
    )
    return Tracing(provider, enabled=True)
