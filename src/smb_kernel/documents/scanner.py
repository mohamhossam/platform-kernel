"""Fail-closed local malware scanning."""

from __future__ import annotations

import socket
import struct

from smb_kernel.errors import DocumentExtractionError


class ClamAvDocumentScanner:
    def __init__(self, host: str, port: int) -> None:
        self._host, self._port = host, port

    def scan(self, content: bytes) -> bool:
        try:
            with socket.create_connection((self._host, self._port), timeout=30) as connection:
                connection.sendall(b"zINSTREAM\x00")
                for start in range(0, len(content), 65536):
                    chunk = content[start : start + 65536]
                    connection.sendall(struct.pack("!I", len(chunk)) + chunk)
                connection.sendall(struct.pack("!I", 0))
                reply = b""
                while b"\x00" not in reply and len(reply) < 4096:
                    part = connection.recv(4096 - len(reply))
                    if not part:
                        break
                    reply += part
        except OSError as exc:
            raise DocumentExtractionError(
                "Malware scanner unavailable. Restore the local scanner and retry."
            ) from exc
        result = reply.split(b"\x00", 1)[0]
        if result == b"stream: OK" and b"\x00" in reply:
            return True
        if result.startswith(b"stream: ") and result.endswith(b" FOUND"):
            return False
        raise DocumentExtractionError(
            "Malware scanner did not return a complete clean verdict. "
            "Retry after checking its limits."
        )


class OfflineDocumentScanner:
    """Explicit deterministic development adapter, never selected for durable/OIDC deployments."""

    def scan(self, content: bytes) -> bool:
        return b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" not in content
