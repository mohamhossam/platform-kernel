"""Optional tracing: outbound spans, trace context only to peers, SQL spans and log IDs."""

from __future__ import annotations

import io
import json
import logging
import os
from collections.abc import Iterator

import httpx
import httpx2
import psycopg
import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from smb_kernel.observability.logging import JsonLogFormatter
from smb_kernel.observability.tracing import (
    TracedTransport,
    TracedTransport2,
    current_trace_ids,
)
from smb_kernel.observability.tracing_setup import NO_TRACING, Tracing, configure_tracing

DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture
def exported() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracing(exported: InMemorySpanExporter) -> Iterator[Tracing]:
    configured = configure_tracing(
        "http://collector:4318", service="kernel-test", version="9.9.9", exporter=exported
    )
    yield configured
    configured.shutdown()


def _finished(tracing: Tracing, exported: InMemorySpanExporter) -> list[ReadableSpan]:
    provider = tracing.tracer_provider
    assert isinstance(provider, SdkTracerProvider)
    provider.force_flush()
    return list(exported.get_finished_spans())


def _client_spans(tracing: Tracing, exported: InMemorySpanExporter) -> list[ReadableSpan]:
    return [span for span in _finished(tracing, exported) if span.kind is SpanKind.CLIENT]


def _echo_headers(seen: list[httpx.Headers]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler)


def test_an_outbound_call_is_a_client_span_without_path_or_query(
    tracing: Tracing, exported: InMemorySpanExporter
) -> None:
    seen: list[httpx.Headers] = []
    transport = TracedTransport(
        _echo_headers(seen), tracer_provider=tracing.tracer_provider, peer="model"
    )
    with tracing.span("job"), httpx.Client(transport=transport) as client:
        client.get("https://models.example:8443/v1/secret-path?key=hidden")

    (span,) = _client_spans(tracing, exported)
    assert span.name == "HTTP GET"
    assert span.kind is SpanKind.CLIENT
    assert dict(span.attributes or {}) == {
        "http.request.method": "GET",
        "server.address": "models.example",
        "server.port": 8443,
        "peer.service": "model",
        "http.response.status_code": 200,
    }
    assert "secret-path" not in str(span.to_json())
    # Not propagating: the provider never sees the trace.
    assert "traceparent" not in seen[0]
    assert span.resource.attributes["service.name"] == "kernel-test"
    assert span.resource.attributes["service.version"] == "9.9.9"


def test_only_a_propagating_transport_sends_traceparent(
    tracing: Tracing, exported: InMemorySpanExporter
) -> None:
    seen: list[httpx.Headers] = []
    transport = TracedTransport(
        _echo_headers(seen),
        tracer_provider=tracing.tracer_provider,
        peer="knowledge",
        propagate=True,
    )
    with tracing.span("request"), httpx.Client(transport=transport) as client:
        client.post("https://knowledge.internal/internal/search", json={})

    request_span = next(s for s in _finished(tracing, exported) if s.name == "request")
    client_span = next(s for s in _finished(tracing, exported) if s.name == "HTTP POST")
    trace_id = format(request_span.context.trace_id, "032x")
    span_id = format(client_span.context.span_id, "016x")
    version, sent_trace, sent_parent, flags = seen[0]["traceparent"].split("-")
    assert (version, sent_trace, sent_parent) == ("00", trace_id, span_id)
    assert int(flags, 16) & 1  # sampled
    assert "baggage" not in seen[0]
    assert client_span.parent is not None
    assert client_span.parent.span_id == request_span.context.span_id


def test_server_errors_and_transport_failures_end_in_error_without_messages(
    tracing: Tracing, exported: InMemorySpanExporter
) -> None:
    failing = TracedTransport(
        httpx.MockTransport(lambda _r: httpx.Response(503)),
        tracer_provider=tracing.tracer_provider,
        peer="model",
    )

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("could not reach https://idp/secret")

    broken = TracedTransport(
        httpx.MockTransport(refuse), tracer_provider=tracing.tracer_provider, peer="identity"
    )
    with tracing.span("job"):
        with httpx.Client(transport=failing) as client:
            client.get("https://models.example/")
        with httpx.Client(transport=broken) as client, pytest.raises(httpx.ConnectError):
            client.get("https://idp.example/")

    unavailable, refused = _client_spans(tracing, exported)
    assert unavailable.status.status_code is StatusCode.ERROR
    assert unavailable.attributes is not None
    assert unavailable.attributes["error.type"] == "503"
    assert refused.status.status_code is StatusCode.ERROR
    assert refused.attributes is not None
    assert refused.attributes["error.type"] == "ConnectError"
    assert not refused.events
    assert "secret" not in str(refused.to_json())


def test_the_httpx2_transport_traces_the_openai_client(
    tracing: Tracing, exported: InMemorySpanExporter
) -> None:
    transport = TracedTransport2(
        httpx2.MockTransport(lambda _r: httpx2.Response(200, json={})),
        tracer_provider=tracing.tracer_provider,
        peer="openai",
    )
    with tracing.span("job"), httpx2.Client(transport=transport) as client:
        client.post("https://api.openai.com/v1/responses", json={})

    (span,) = _client_spans(tracing, exported)
    assert span.attributes is not None
    assert span.attributes["peer.service"] == "openai"
    assert span.attributes["http.response.status_code"] == 200


def test_a_named_span_records_its_attributes_and_the_type_of_a_failure(
    tracing: Tracing, exported: InMemorySpanExporter
) -> None:
    with tracing.span("ai_job analyse", {"job.id": "j-1"}):
        pass
    with pytest.raises(ValueError), tracing.span("ai_job generate"):
        raise ValueError("requirement text that must not be exported")

    succeeded, failed = _finished(tracing, exported)
    assert succeeded.attributes == {"job.id": "j-1"}
    assert succeeded.status.status_code is StatusCode.UNSET
    assert failed.status.status_code is StatusCode.ERROR
    assert failed.attributes == {"error.type": "ValueError"}
    assert "requirement text" not in str(failed.to_json())


def test_log_lines_carry_the_open_span_ids(tracing: Tracing) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("kernel.tracing.test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("outside")
        with tracing.span("work"):
            ids = current_trace_ids()
            logger.info("inside")
    finally:
        logger.removeHandler(handler)

    outside, inside = (json.loads(line) for line in stream.getvalue().splitlines())
    assert "trace_id" not in outside and "span_id" not in outside
    assert ids is not None
    assert (inside["trace_id"], inside["span_id"]) == ids
    assert len(inside["trace_id"]) == 32 and len(inside["span_id"]) == 16


def test_no_tracing_records_nothing_and_sends_no_trace_context() -> None:
    seen: list[httpx.Headers] = []
    transport = TracedTransport(
        _echo_headers(seen),
        tracer_provider=NO_TRACING.tracer_provider,
        peer="knowledge",
        propagate=True,
    )
    with NO_TRACING.span("request"), httpx.Client(transport=transport) as client:
        assert current_trace_ids() is None
        client.get("https://knowledge.internal/")

    assert "traceparent" not in seen[0]
    assert NO_TRACING.enabled is False
    NO_TRACING.shutdown()


def test_sampling_keeps_the_configured_share_and_follows_a_sampled_parent() -> None:
    exported = InMemorySpanExporter()
    never = configure_tracing(
        "http://collector:4318", service="s", version="1", sample_ratio=0.0, exporter=exported
    )
    with never.span("dropped"):
        pass
    assert _finished(never, exported) == []
    never.shutdown()

    with pytest.raises(ValueError, match="between 0 and 1"):
        configure_tracing("http://collector:4318", service="s", version="1", sample_ratio=1.5)


@pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")
def test_an_instrumented_connection_traces_statements_without_values(
    tracing: Tracing, exported: InMemorySpanExporter
) -> None:
    assert DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL) as connection:
        tracing.instrument_connection(connection)
        with tracing.span("request"):
            connection.execute("SELECT %s::text", ("private value",)).fetchone()

    statement = next(s for s in _finished(tracing, exported) if s.name != "request")
    assert statement.kind is SpanKind.CLIENT
    assert statement.attributes is not None
    assert "SELECT %s::text" in str(statement.attributes.get("db.statement"))
    assert "private value" not in str(statement.to_json())


@pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not configured")
def test_no_tracing_leaves_a_connection_untouched() -> None:
    assert DATABASE_URL is not None
    with psycopg.connect(DATABASE_URL) as connection:
        factory = connection.cursor_factory
        NO_TRACING.instrument_connection(connection)
        assert connection.cursor_factory is factory


def test_a_request_span_continues_the_callers_trace_and_is_named_by_route(
    tracing: Tracing, exported: InMemorySpanExporter
) -> None:
    caller_trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    headers = {"traceparent": f"00-{caller_trace}-00f067aa0ba902b7-01", "baggage": "user=x"}
    with tracing.request_span("GET", headers, {"request.id": "req-1"}) as request:
        request.finish("/requirements/{requirement_id}", 200)
    with tracing.request_span("POST", {}) as request:
        request.finish("/requirements", 503)
    with pytest.raises(RuntimeError), tracing.request_span("GET", {}):
        raise RuntimeError("payload text")

    continued, unavailable, crashed = _finished(tracing, exported)
    assert continued.name == "GET /requirements/{requirement_id}"
    assert continued.kind is SpanKind.SERVER
    assert format(continued.context.trace_id, "032x") == caller_trace
    assert dict(continued.attributes or {}) == {
        "http.request.method": "GET",
        "request.id": "req-1",
        "http.route": "/requirements/{requirement_id}",
        "http.response.status_code": 200,
    }
    assert unavailable.status.status_code is StatusCode.ERROR
    assert format(unavailable.context.trace_id, "032x") != caller_trace
    assert crashed.status.status_code is StatusCode.ERROR
    assert crashed.attributes is not None
    assert crashed.attributes["error.type"] == "RuntimeError"
    assert "payload text" not in str(crashed.to_json())


def test_a_client_span_with_nothing_around_it_starts_no_trace(
    tracing: Tracing, exported: InMemorySpanExporter
) -> None:
    transport = TracedTransport(
        httpx.MockTransport(lambda _r: httpx.Response(200)),
        tracer_provider=tracing.tracer_provider,
        peer="knowledge",
    )
    with httpx.Client(transport=transport) as client:
        client.get("https://knowledge.internal/poll")
        with tracing.span("ai_job analyse"):
            client.get("https://knowledge.internal/search")

    assert sorted(span.name for span in _finished(tracing, exported)) == [
        "HTTP GET",
        "ai_job analyse",
    ]
