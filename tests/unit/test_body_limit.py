"""Request bodies are capped while they stream, with room for one file on multipart."""

from __future__ import annotations

from collections.abc import Iterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import Scope

from smb_kernel.http.body_limit import BodyLimits, RequestBodyLimit, body_limit_for

LIMITS = BodyLimits(request_max_body_bytes=100, document_max_file_bytes=1000)


def _client() -> TestClient:
    async def echo(request: Request) -> JSONResponse:
        return JSONResponse({"size": len(await request.body())})

    def limits(_scope: Scope) -> BodyLimits:
        return LIMITS

    app = Starlette(routes=[Route("/echo", echo, methods=["POST"])])
    app.add_middleware(RequestBodyLimit, limits=limits)
    return TestClient(app)


def test_multipart_gets_room_for_one_file() -> None:
    assert body_limit_for(LIMITS, "application/json") == 100
    assert body_limit_for(LIMITS, "multipart/form-data; boundary=x") == 1100


def test_a_body_within_the_ceiling_passes() -> None:
    response = _client().post("/echo", content=b"x" * 100)
    assert response.json() == {"size": 100}


def test_a_declared_oversize_body_is_refused_before_reading() -> None:
    response = _client().post("/echo", content=b"x" * 101)
    assert response.status_code == 413
    assert "100-byte limit" in response.text


def test_an_undeclared_oversize_stream_is_refused_while_reading() -> None:
    def chunks() -> Iterator[bytes]:
        yield b"x" * 60
        yield b"x" * 60

    response = _client().post("/echo", content=chunks())
    assert response.status_code == 413
