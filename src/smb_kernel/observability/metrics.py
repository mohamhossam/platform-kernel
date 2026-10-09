"""Prometheus metrics: HTTP requests, AI provider requests and tokens, AI jobs, the
database pool, readiness and the process itself.

Every container owns its registry, so tests and processes never share counters
through a module-level default. The exporter listens on its own port, never on
the public API.

Label values come from the application, which keeps each one to a small fixed
set: a raw path, a user's input or an ID must never become a label value.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from threading import Lock
from time import perf_counter
from typing import Protocol

import httpx
import httpx2
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.gc_collector import GCCollector
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector

_PROVIDER_BUCKETS = (0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)
_JOB_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800)
# Usage field spellings: chat completions and embeddings, and the Responses API.
_USAGE_DIRECTIONS = {
    "prompt_tokens": "input",
    "input_tokens": "input",
    "completion_tokens": "output",
    "output_tokens": "output",
}
# Bodies larger than this are not parsed for usage; a provider body that large
# is a batch of embeddings whose count the input side already bounds.
_MAX_USAGE_BODY_BYTES = 16 * 1024 * 1024


class PoolSample(Protocol):
    """A connection pool's use at one moment (`PooledPostgresConnector.stats()`)."""

    @property
    def in_use(self) -> int: ...

    @property
    def idle(self) -> int: ...

    @property
    def max_size(self) -> int: ...

    @property
    def waiting(self) -> int: ...


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        # The process's memory, CPU and open files, the Python version, and
        # garbage collection, as every Prometheus client exports them.
        ProcessCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)
        GCCollector(registry=self.registry)
        self._http_requests = Counter(
            "smb_http_requests_total",
            "HTTP requests by route template and status.",
            ("method", "route", "status"),
            registry=self.registry,
        )
        self._http_duration = Histogram(
            "smb_http_request_duration_seconds",
            "HTTP request duration by route template.",
            ("method", "route"),
            registry=self.registry,
        )
        self._provider_requests = Counter(
            "smb_provider_requests_total",
            "Requests to AI providers by provider, operation and outcome.",
            ("provider", "operation", "outcome"),
            registry=self.registry,
        )
        self._provider_duration = Histogram(
            "smb_provider_request_duration_seconds",
            "AI provider request duration, to the end of the response body.",
            ("provider", "operation"),
            buckets=_PROVIDER_BUCKETS,
            registry=self.registry,
        )
        self._provider_tokens = Counter(
            "smb_provider_tokens_total",
            "Tokens AI providers report consuming, by provider, model and direction.",
            ("provider", "model", "direction"),
            registry=self.registry,
        )
        self._jobs = Counter(
            "smb_ai_jobs_total",
            "Executed AI job attempts by operation and resulting status.",
            ("operation", "status"),
            registry=self.registry,
        )
        self._job_duration = Histogram(
            "smb_ai_job_duration_seconds",
            "AI job attempt duration by operation.",
            ("operation",),
            buckets=_JOB_BUCKETS,
            registry=self.registry,
        )

        self._build_info = Gauge(
            "smb_build_info",
            "The running service and its version; always 1.",
            ("service", "version"),
            registry=self.registry,
        )
        self._ready = Gauge(
            "smb_ready",
            "1 when this process's latest readiness check passed, 0 when it failed.",
            registry=self.registry,
        )
        self._jobs_queued = Gauge(
            "smb_ai_jobs_queued",
            "AI jobs waiting to be claimed, by operation, as last sampled.",
            ("operation",),
            registry=self.registry,
        )
        self._oldest_queued_age = Gauge(
            "smb_ai_job_oldest_queued_age_seconds",
            "How long the oldest waiting AI job has waited, as last sampled; 0 when none waits.",
            registry=self.registry,
        )
        self._queued_operations: set[str] = set()
        self._ingestion_failures = Counter(
            "smb_ingestion_failures_total",
            "Ingestion attempts that failed and will be retried.",
            registry=self.registry,
        )
        self._client_errors = Counter(
            "smb_client_errors_total",
            "Errors browsers reported, by kind.",
            ("kind",),
            registry=self.registry,
        )
        self._spend_blocked = Counter(
            "smb_provider_spend_blocked_total",
            "AI work held back because the provider spend budget is spent, by action.",
            ("action",),
            registry=self.registry,
        )
        self._pool_watched = False
        self._lock = Lock()

    def record_http(self, method: str, route: str, status: int, seconds: float) -> None:
        self._http_requests.labels(method, route, str(status)).inc()
        self._http_duration.labels(method, route).observe(seconds)

    def record_provider_request(
        self, provider: str, operation: str, outcome: str, seconds: float
    ) -> None:
        self._provider_requests.labels(provider, operation, outcome).inc()
        self._provider_duration.labels(provider, operation).observe(seconds)

    def record_provider_tokens(
        self, provider: str, model: str, direction: str, tokens: int
    ) -> None:
        self._provider_tokens.labels(provider, model, direction).inc(tokens)

    def record_job(self, operation: str, status: str, seconds: float) -> None:
        self._jobs.labels(operation, status).inc()
        self._job_duration.labels(operation).observe(seconds)

    def set_build_info(self, service: str, version: str) -> None:
        """Name the running service and version; call once at startup."""
        self._build_info.labels(service, version).set(1)

    def set_ready(self, ready: bool) -> None:
        self._ready.set(1 if ready else 0)

    def set_ai_job_queue(self, queued: Mapping[str, int], oldest_age_seconds: float) -> None:
        """Record a sample of the queue: waiting jobs by operation, and the oldest one's age.

        An operation sampled before and missing now is set to 0, so a drained
        queue does not keep reporting its last backlog.
        """
        with self._lock:
            for operation in self._queued_operations - queued.keys():
                self._jobs_queued.labels(operation).set(0)
            for operation, count in queued.items():
                self._jobs_queued.labels(operation).set(count)
            self._queued_operations.update(queued)
            self._oldest_queued_age.set(max(0.0, oldest_age_seconds))

    def record_ingestion_failure(self) -> None:
        self._ingestion_failures.inc()

    def record_client_error(self, kind: str) -> None:
        self._client_errors.labels(kind).inc()

    def record_provider_spend_blocked(self, action: str) -> None:
        self._spend_blocked.labels(action).inc()

    def watch_db_pool(self, sample: Callable[[], PoolSample]) -> None:
        """Report the pool's connections on every scrape, read from `sample`.

        Exports `smb_db_pool_connections` by state (`in_use`, `idle`),
        `smb_db_pool_max_connections` and `smb_db_pool_requests_waiting`.
        """
        with self._lock:
            if self._pool_watched:
                raise ValueError("A database pool is already watched.")
            self._pool_watched = True
        self.registry.register(_PoolCollector(sample))


class _PoolCollector:
    def __init__(self, sample: Callable[[], PoolSample]) -> None:
        self._sample = sample

    def collect(self) -> Iterator[Metric]:
        try:
            stats = self._sample()
        except Exception:  # A failing sample drops these series; it never fails the scrape.
            return
        connections = GaugeMetricFamily(
            "smb_db_pool_connections",
            "Open database pool connections, by state.",
            labels=("state",),
        )
        connections.add_metric(("in_use",), stats.in_use)
        connections.add_metric(("idle",), stats.idle)
        yield connections
        yield GaugeMetricFamily(
            "smb_db_pool_max_connections",
            "The most connections the database pool may open.",
            value=stats.max_size,
        )
        yield GaugeMetricFamily(
            "smb_db_pool_requests_waiting",
            "Requests waiting for a database pool connection.",
            value=stats.waiting,
        )


def serve_metrics(metrics: Metrics, host: str, port: int) -> Callable[[], None]:
    """Expose `metrics` over HTTP and return the function that stops the exporter."""
    server, thread = start_http_server(port, addr=host, registry=metrics.registry)

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    return stop


def provider_operation(path: str) -> str:
    """A bounded label for a provider endpoint; raw paths must never become label values."""
    trimmed = path.rstrip("/")
    if trimmed.endswith("embeddings"):
        return "embeddings"
    if trimmed.endswith(("chat/completions", "responses")):
        return "generation"
    return "other"


def _outcome(status_code: int) -> str:
    return f"{status_code // 100}xx"


def provider_usage(body: bytes) -> tuple[str, dict[str, int]] | None:
    """The model and token counts a provider reported in a JSON body, if any.

    Providers report usage in the response they already return, so this reads
    what they bill rather than estimating it. A body that is not JSON, or that
    carries no usable counts, yields None: metrics never fail a provider call.
    """
    if len(body) > _MAX_USAGE_BODY_BYTES:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    counts: dict[str, int] = {}
    for field, direction in _USAGE_DIRECTIONS.items():
        value = usage.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            counts[direction] = counts.get(direction, 0) + value
    if not counts:
        return None
    model = payload.get("model")
    label = model.strip()[:100] if isinstance(model, str) and model.strip() else "unknown"
    return label, counts


def _record_usage(metrics: Metrics, provider: str, status_code: int, body: bytes) -> None:
    if not 200 <= status_code < 300:
        return
    usage = provider_usage(body)
    if usage is None:
        return
    model, counts = usage
    for direction, tokens in counts.items():
        metrics.record_provider_tokens(provider, model, direction, tokens)


class MeteredTransport(httpx.BaseTransport):
    """Records every provider request sent through an `httpx` client."""

    def __init__(self, metrics: Metrics, provider: str, inner: httpx.BaseTransport) -> None:
        self._metrics = metrics
        self._provider = provider
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        operation = provider_operation(request.url.path)
        started = perf_counter()
        try:
            response = self._inner.handle_request(request)
        except Exception:
            self._metrics.record_provider_request(
                self._provider, operation, "error", perf_counter() - started
            )
            raise
        # Every provider call here is non-streaming, so reading the body is safe;
        # the client then uses the content already read.
        body = response.read()
        self._metrics.record_provider_request(
            self._provider, operation, _outcome(response.status_code), perf_counter() - started
        )
        _record_usage(self._metrics, self._provider, response.status_code, body)
        return response

    def close(self) -> None:
        self._inner.close()


class MeteredTransport2(httpx2.BaseTransport):
    """The same, for the `httpx2` client inside the OpenAI SDK."""

    def __init__(self, metrics: Metrics, provider: str, inner: httpx2.BaseTransport) -> None:
        self._metrics = metrics
        self._provider = provider
        self._inner = inner

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        operation = provider_operation(request.url.path)
        started = perf_counter()
        try:
            response = self._inner.handle_request(request)
        except Exception:
            self._metrics.record_provider_request(
                self._provider, operation, "error", perf_counter() - started
            )
            raise
        # Every provider call here is non-streaming, so reading the body is safe;
        # the client then uses the content already read.
        body = response.read()
        self._metrics.record_provider_request(
            self._provider, operation, _outcome(response.status_code), perf_counter() - started
        )
        _record_usage(self._metrics, self._provider, response.status_code, body)
        return response

    def close(self) -> None:
        self._inner.close()
