"""Correlation, operational logging, metrics, provider metering and the clocks."""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
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


def _exposed(metrics: Metrics) -> str:
    return generate_latest(metrics.registry).decode()


def test_the_process_platform_and_garbage_collector_are_exported() -> None:
    exposed = _exposed(Metrics())

    assert "python_info{" in exposed
    assert "python_gc_collections_total{" in exposed
    # The process collector reads /proc, which only Linux has.
    assert "process_resident_memory_bytes" in exposed or "process_" not in exposed


def test_build_readiness_and_counted_events_are_exported() -> None:
    metrics = Metrics()
    metrics.set_build_info("requirement-portal", "1.4.0")
    metrics.set_ready(False)
    metrics.record_ingestion_failure()
    metrics.record_client_error("render")
    metrics.record_provider_spend_blocked("start")
    metrics.record_provider_spend_blocked("claim")

    exposed = _exposed(metrics)
    assert 'smb_build_info{service="requirement-portal",version="1.4.0"} 1.0' in exposed
    assert "smb_ready 0.0" in exposed
    assert "smb_ingestion_failures_total 1.0" in exposed
    assert 'smb_client_errors_total{kind="render"} 1.0' in exposed
    assert 'smb_provider_spend_blocked_total{action="claim"} 1.0' in exposed
    metrics.set_ready(True)
    assert "smb_ready 1.0" in _exposed(metrics)


def test_a_drained_operation_reports_zero_waiting_jobs() -> None:
    metrics = Metrics()
    metrics.set_ai_job_queue({"analysis": 3, "epic": 1}, oldest_age_seconds=42.5)
    metrics.set_ai_job_queue({"epic": 2}, oldest_age_seconds=-1)

    exposed = _exposed(metrics)
    assert 'smb_ai_jobs_queued{operation="analysis"} 0.0' in exposed
    assert 'smb_ai_jobs_queued{operation="epic"} 2.0' in exposed
    assert "smb_ai_job_oldest_queued_age_seconds 0.0" in exposed


@dataclass(frozen=True)
class _Pool:
    in_use: int
    idle: int
    max_size: int
    waiting: int


def test_the_database_pool_is_read_on_every_scrape() -> None:
    metrics = Metrics()
    samples = iter([_Pool(3, 1, 10, 0), _Pool(10, 0, 10, 3)])
    metrics.watch_db_pool(lambda: next(samples))

    first = _exposed(metrics)
    assert 'smb_db_pool_connections{state="in_use"} 3.0' in first
    assert 'smb_db_pool_connections{state="idle"} 1.0' in first
    assert "smb_db_pool_max_connections 10.0" in first
    second = _exposed(metrics)
    assert 'smb_db_pool_connections{state="in_use"} 10.0' in second
    assert "smb_db_pool_requests_waiting 3.0" in second
    # The sample is exhausted now: the pool's series drop out, the rest still scrape.
    assert "smb_db_pool_connections" not in _exposed(metrics)
    with pytest.raises(ValueError):
        metrics.watch_db_pool(lambda: _Pool(0, 0, 1, 0))


def test_clocks() -> None:
    instant = datetime(2026, 10, 2, tzinfo=UTC)
    clock = FixedClock(instant)
    assert clock.now() == instant
    later = datetime(2026, 10, 3, tzinfo=UTC)
    clock.set(later)
    assert clock.now() == later
    assert SystemClock().now().tzinfo is UTC
