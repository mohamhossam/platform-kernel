"""Correlation, operational logging, provider metering and the clocks."""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime

import httpx
from prometheus_client import generate_latest

from smb_kernel.observability.correlation import correlation_scope, current_correlation_id
from smb_kernel.observability.logging import JsonLogFormatter, LogFormat, configure_logging
from smb_kernel.observability.metrics import MeteredTransport, Metrics
from smb_kernel.time.fixed import FixedClock
from smb_kernel.time.system import SystemClock


def test_correlation_is_scoped_and_restored() -> None:
    assert current_correlation_id() is None
    with correlation_scope("outer"):
        with correlation_scope("inner"):
            assert current_correlation_id() == "inner"
        assert current_correlation_id() == "outer"
    assert current_correlation_id() is None


def test_json_lines_carry_the_correlation_id() -> None:
    configure_logging("INFO", LogFormat.JSON)
    root = logging.getLogger()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    for existing in root.handlers:
        for log_filter in existing.filters:
            handler.addFilter(log_filter)
    root.addHandler(handler)
    try:
        with correlation_scope("req-7"):
            logging.getLogger("kernel.test").info("served", extra={"route": "/x"})
    finally:
        root.removeHandler(handler)
    line = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert line["message"] == "served"
    assert line["correlation_id"] == "req-7"
    assert line["route"] == "/x"


def test_provider_requests_and_tokens_are_metered() -> None:
    metrics = Metrics()
    inner = httpx.MockTransport(
        lambda _r: httpx.Response(
            200, json={"model": "m1", "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
        )
    )
    client = httpx.Client(transport=MeteredTransport(metrics, "openrouter", inner))
    client.post("https://openrouter.ai/api/v1/chat/completions", json={})

    exposed = generate_latest(metrics.registry).decode()
    labels = 'operation="generation",outcome="2xx",provider="openrouter"'
    assert f"smb_provider_requests_total{{{labels}}} 1.0" in exposed
    assert 'direction="input",model="m1",provider="openrouter"} 7.0' in exposed
    assert 'direction="output",model="m1",provider="openrouter"} 3.0' in exposed


def test_clocks() -> None:
    instant = datetime(2026, 10, 2, tzinfo=UTC)
    clock = FixedClock(instant)
    assert clock.now() == instant
    later = datetime(2026, 10, 3, tzinfo=UTC)
    clock.set(later)
    assert clock.now() == later
    assert SystemClock().now().tzinfo is UTC
