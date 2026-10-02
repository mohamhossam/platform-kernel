"""The base every service-to-service adapter builds on.

It owns the mechanics only: the service token, timeouts, bounded retries with
backoff, forwarding the correlation ID, and turning transport failures into
kernel errors. Request and response models belong to the calling adapter,
which validates what comes back before handing it to its domain.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from smb_kernel.errors import ServiceResponseError, ServiceUnavailableError
from smb_kernel.observability.correlation import current_correlation_id

CORRELATION_HEADER = "X-Request-ID"
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


class InternalHttpClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        service: str,
        timeout_seconds: float = 10.0,
        retries: int = 2,
        backoff_seconds: float = 0.25,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url.strip() or not token.strip():
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

    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params, retry=True)

    def post_json(self, path: str, body: Any, *, idempotent: bool = False) -> Any:
        """POST `body`. Only an idempotent POST is retried, because a retry after
        a lost response would otherwise repeat the side effect."""
        return self._request("POST", path, json=body, retry=idempotent)

    def _request(self, method: str, path: str, *, retry: bool, **kwargs: Any) -> Any:
        url = f"{self._base_url}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        correlation_id = current_correlation_id()
        if correlation_id:
            headers[CORRELATION_HEADER] = correlation_id
        attempts = 1 + (self._retries if retry else 0)
        for attempt in range(1, attempts + 1):
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
            return self._decode(response)
        raise AssertionError("unreachable")  # pragma: no cover

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
