"""A ceiling on every request body, enforced while it streams in.

nginx caps bodies at the edge, but an API must not depend on its proxy: a
request that reaches it directly would otherwise be buffered whole before any
handler or schema could refuse it. Ordinary requests are capped at
`request_max_body_bytes`. Multipart uploads may additionally carry one file of
up to `document_max_file_bytes`, whose own reader enforces the exact per-file
limit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send


@dataclass(frozen=True)
class BodyLimits:
    request_max_body_bytes: int
    document_max_file_bytes: int


class RequestBodyTooLargeError(HTTPException):
    """A request body exceeded its ceiling. An HTTPException, because FastAPI
    turns any other exception raised while reading a body into a 400."""

    def __init__(self, limit_bytes: int) -> None:
        super().__init__(
            status_code=413,
            detail=f"The request body is larger than the {limit_bytes:,}-byte limit.",
        )


def body_limit_for(limits: BodyLimits, content_type: str) -> int:
    if content_type.lower().startswith("multipart/form-data"):
        return limits.document_max_file_bytes + limits.request_max_body_bytes
    return limits.request_max_body_bytes


class RequestBodyLimit:
    """ASGI middleware. `limits` is read per request, so an application can take
    it from configuration resolved after the middleware was installed."""

    def __init__(self, app: ASGIApp, limits: Callable[[Scope], BodyLimits]) -> None:
        self._app = app
        self._limits = limits

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", ())}
        limit = body_limit_for(
            self._limits(scope), headers.get(b"content-type", b"").decode("latin-1")
        )
        declared = headers.get(b"content-length", b"").decode("latin-1")
        if declared.isdigit() and int(declared) > limit:
            # Refused before a byte is read; the request's reader never starts.
            limited_receive = _refuse_immediately(limit)
        else:
            limited_receive = _counting(receive, limit)
        await self._app(scope, limited_receive, send)


def _refuse_immediately(limit: int) -> Receive:
    async def receive() -> Message:
        raise RequestBodyTooLargeError(limit)

    return receive


def _counting(receive: Receive, limit: int) -> Receive:
    received = 0

    async def limited() -> Message:
        nonlocal received
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > limit:
                raise RequestBodyTooLargeError(limit)
        return message

    return limited
