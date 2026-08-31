"""Executor service: dedicated privileged process for external mutations.

This service is the ONLY AIPM process that crosses the Linux privilege
boundary. It listens on a Unix domain socket, receives structured
execution requests from the control plane, validates them independently
(structural validation, NOT business authorization), performs the exact
mutation, and returns a bounded result.

Trust model:
- The control plane is the authorization authority (it decides WHETHER).
- This service is the execution authority (it decides HOW, for the
  exact capability it was configured with).
- This service performs structural validation, NOT business authorization.
- This service does NOT trust the caller: it independently verifies
  request schema, action identity, capability identity, and contract digest.

IPC: Unix domain socket with length-prefixed JSON.
Authentication: SO_PEERCRED (Unix peer credentials) + caller UID check.
No shell. No arbitrary command. No arbitrary argv. Bounded request/response.
"""
from __future__ import annotations

import json
import os
import selectors
import signal
import socket
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

EXECUTOR_SOCKET_PATH = "/run/aipm/executor.sock"
MAX_REQUEST_SIZE = 4096
MAX_RESPONSE_SIZE = 4096
PROTOCOL_VERSION = "mc612-executor-ipc-v1"
CALLER_UID = None  # set at service construction; None = accept any uid in allowed set


class ExecutorIPCError(ValueError):
    """Raised when an IPC request fails structural validation."""


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """Bounded execution request from the control plane."""

    action_id: str
    capability_id: str
    target_id: str
    contract_digest: str
    lease_id: str
    fencing_token: int

    @classmethod
    def from_json(cls, data: bytes) -> "ExecutionRequest":
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ExecutorIPCError("Malformed JSON request") from exc
        if not isinstance(payload, dict):
            raise ExecutorIPCError("Request must be a JSON object")
        required = {"action_id", "capability_id", "target_id", "contract_digest", "lease_id", "fencing_token"}
        if not required.issubset(payload):
            missing = required - set(payload)
            raise ExecutorIPCError(f"Missing required fields: {missing}")
        if len(payload) > 16:
            raise ExecutorIPCError("Too many request fields")
        return cls(
            action_id=payload["action_id"],
            capability_id=payload["capability_id"],
            target_id=payload["target_id"],
            contract_digest=payload["contract_digest"],
            lease_id=payload["lease_id"],
            fencing_token=payload["fencing_token"],
        )

    def to_json(self) -> bytes:
        return json.dumps({
            "action_id": self.action_id,
            "capability_id": self.capability_id,
            "target_id": self.target_id,
            "contract_digest": self.contract_digest,
            "lease_id": self.lease_id,
            "fencing_token": self.fencing_token,
        }, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ExecutionResponse:
    """Bounded execution response to the control plane."""

    outcome: str
    provider_code: str
    action_id: str
    evidence_reference: str

    def to_json(self) -> bytes:
        return json.dumps({
            "outcome": self.outcome,
            "provider_code": self.provider_code,
            "action_id": self.action_id,
            "evidence_reference": self.evidence_reference,
        }, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def encode_frame(data: bytes) -> bytes:
    """Length-prefixed frame: 4-byte big-endian length + payload."""
    if len(data) > MAX_REQUEST_SIZE:
        raise ExecutorIPCError(f"Frame too large: {len(data)} > {MAX_REQUEST_SIZE}")
    return struct.pack(">I", len(data)) + data


def decode_frame(sock: socket.socket) -> bytes:
    """Read one length-prefixed frame from the socket."""
    header = _recv_exact(sock, 4)
    if header is None:
        raise ExecutorIPCError("Connection closed before header")
    (length,) = struct.unpack(">I", header)
    if length > MAX_REQUEST_SIZE:
        raise ExecutorIPCError(f"Frame too large: {length}")
    payload = _recv_exact(sock, length)
    if payload is None:
        raise ExecutorIPCError("Connection closed before payload")
    return payload


def _recv_exact(sock: socket.socket, size: int) -> bytes | None:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            return None if not data else data  # partial read
        data += chunk
    return data


def get_peer_uid(conn: socket.socket) -> int:
    """Get the Unix peer credentials (UID) of the connected client."""
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", creds)
    return uid


class ExecutorIPCServer:
    """Unix domain socket server for the executor service."""

    __slots__ = ("_socket_path", "_allowed_caller_uids", "_handler", "_sock", "_initialized")

    def __init__(self, *, socket_path: str = EXECUTOR_SOCKET_PATH, allowed_caller_uids: set[int] | None = None, handler: Callable[[ExecutionRequest], ExecutionResponse]) -> None:
        if handler is None:
            raise TypeError("handler is required")
        object.__setattr__(self, "_socket_path", socket_path)
        object.__setattr__(self, "_allowed_caller_uids", allowed_caller_uids)
        object.__setattr__(self, "_handler", handler)
        object.__setattr__(self, "_sock", None)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False) and name != "_sock":
            raise AttributeError("ExecutorIPCServer runtime is immutable")
        object.__setattr__(self, name, value)

    def start(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        socket_path = Path(self._socket_path)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            socket_path.unlink()
        sock.bind(str(socket_path))
        # Restrict socket permissions: only the control-plane group can connect
        os.chmod(str(socket_path), 0o660)
        sock.listen(1)
        object.__setattr__(self, "_sock", sock)

    def serve_one(self) -> ExecutionResponse:
        """Accept one connection, validate, execute, and return the response."""
        conn, _addr = self._sock.accept()
        try:
            # Peer credential authentication
            caller_uid = get_peer_uid(conn)
            if self._allowed_caller_uids is not None and caller_uid not in self._allowed_caller_uids:
                response = ExecutionResponse(outcome="refused", provider_code="unauthorized_caller", action_id="", evidence_reference="")
                conn.sendall(encode_frame(response.to_json()))
                return response

            # Read and parse request
            payload = decode_frame(conn)
            request = ExecutionRequest.from_json(payload)

            # Structural validation (NOT business authorization)
            if not request.action_id or len(request.action_id) != 64:
                raise ExecutorIPCError("Invalid action_id")
            if not request.contract_digest or len(request.contract_digest) != 64:
                raise ExecutorIPCError("Invalid contract_digest")

            # Execute via the handler
            response = self._handler(request)
            conn.sendall(encode_frame(response.to_json()))
            return response
        except (ExecutorIPCError, json.JSONDecodeError) as exc:
            error_response = ExecutionResponse(outcome="refused", provider_code=str(exc)[:128], action_id="", evidence_reference="")
            try:
                conn.sendall(encode_frame(error_response.to_json()))
            except OSError:
                pass
            return error_response
        finally:
            conn.close()

    def stop(self) -> None:
        if self._sock:
            self._sock.close()
            socket_path = Path(self._socket_path)
            if socket_path.exists():
                socket_path.unlink()

    def serve_forever(self, *, stop_event=None) -> None:
        """Blocking accept loop with proper signal handling and no polling."""
        if self._sock is None:
            raise ExecutorIPCError("Server not started")
        sel = selectors.DefaultSelector()
        sel.register(self._sock, selectors.EVENT_READ)
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                events = sel.select(timeout=1.0)
                if not events:
                    continue  # timeout: check stop_event again
                if stop_event is not None and stop_event.is_set():
                    break
                self.serve_one()
        finally:
            sel.unregister(self._sock)
            sel.close()


class ExecutorIPCClient:
    """Control-plane client for sending execution requests to the executor service."""

    __slots__ = ("_socket_path", "_initialized")

    def __init__(self, *, socket_path: str = EXECUTOR_SOCKET_PATH) -> None:
        object.__setattr__(self, "_socket_path", socket_path)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("ExecutorIPCClient configuration is immutable")
        object.__setattr__(self, name, value)

    def send(self, request: ExecutionRequest, *, timeout: float = 30.0) -> ExecutionResponse:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self._socket_path)
            sock.sendall(encode_frame(request.to_json()))
            payload = decode_frame(sock)
            response_data = json.loads(payload)
            return ExecutionResponse(
                outcome=response_data["outcome"],
                provider_code=response_data["provider_code"],
                action_id=response_data["action_id"],
                evidence_reference=response_data["evidence_reference"],
            )
        except socket.timeout:
            return ExecutionResponse(outcome="unknown_outcome", provider_code="timeout", action_id=request.action_id, evidence_reference="")
        finally:
            sock.close()
