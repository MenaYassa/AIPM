"""Observation-only MC-6.12B AF_UNIX client."""
from __future__ import annotations

import socket
from typing import Any

from aipm.provenance.protocol import MAX_REQUEST_FRAME, ObservationRequest, decode_frame, encode_frame


class ObservationClient:
    """Client for the ordinary observation channel only."""

    __slots__ = ("_socket_path", "_timeout")

    def __init__(self, *, socket_path: str = "/run/aipm/provenance.sock", timeout: float = 2.0):
        if not socket_path.startswith("/") or timeout <= 0 or timeout > 10:
            raise ValueError("invalid observation client configuration")
        self._socket_path = socket_path
        self._timeout = timeout

    def observe(self, request: ObservationRequest) -> dict[str, Any]:
        payload = encode_frame(request.as_dict(), limit=MAX_REQUEST_FRAME)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(self._timeout)
            conn.connect(self._socket_path)
            conn.sendall(payload)
            header = conn.recv(4)
            if len(header) != 4:
                raise ValueError("truncated observation response")
            size = int.from_bytes(header, "big")
            if size > 256 * 1024:
                raise ValueError("observation response exceeds bound")
            body = b""
            while len(body) < size:
                chunk = conn.recv(min(65536, size - len(body)))
                if not chunk:
                    raise ValueError("truncated observation response")
                body += chunk
            return decode_frame(header + body)
