"""Prometheus metrics: HTTP requests, AI provider requests and tokens, and AI jobs.

Every container owns its registry, so tests and processes never share counters
through a module-level default. The exporter listens on its own port, never on
the public API.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from time import perf_counter

import httpx
import httpx2
from prometheus_client import CollectorRegistry, Counter, Histogram, start_http_server

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


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
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
