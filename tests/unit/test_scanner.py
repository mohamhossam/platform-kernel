"""Malware protocol replies fail closed unless the complete clean verdict arrives."""

from types import TracebackType

import pytest

from smb_kernel.documents.scanner import ClamAvDocumentScanner
from smb_kernel.errors import DocumentExtractionError


class ScannerSocket:
    def __init__(self, replies: tuple[bytes, ...]) -> None:
        self.replies = list(replies)
        self.sent: list[bytes] = []

    def __enter__(self) -> "ScannerSocket":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass

    def sendall(self, value: bytes) -> None:
        self.sent.append(value)

    def recv(self, limit: int) -> bytes:
        return self.replies.pop(0) if self.replies else b""


@pytest.mark.parametrize(
    "replies,clean", [((b"stream: ", b"OK\x00"), True), ((b"stream: Malware FOUND\x00",), False)]
)
def test_scanner_complete_verdict(
    monkeypatch: pytest.MonkeyPatch, replies: tuple[bytes, ...], clean: bool
) -> None:
    connection = ScannerSocket(replies)
    monkeypatch.setattr("socket.create_connection", lambda *_args, **_kwargs: connection)
    assert ClamAvDocumentScanner("127.0.0.1", 3310).scan(b"document") is clean
    assert connection.sent[0] == b"zINSTREAM\x00"
    assert connection.sent[-1] == b"\x00\x00\x00\x00"


@pytest.mark.parametrize(
    "reply", [b"", b"stream: OK", b"stream: Size limit exceeded ERROR\x00", b"garbage\x00"]
)
def test_scanner_incomplete_or_malformed_reply_fails_closed(
    monkeypatch: pytest.MonkeyPatch, reply: bytes
) -> None:
    monkeypatch.setattr(
        "socket.create_connection", lambda *_args, **_kwargs: ScannerSocket((reply,))
    )
    with pytest.raises(DocumentExtractionError, match="complete clean verdict"):
        ClamAvDocumentScanner("127.0.0.1", 3310).scan(b"document")
