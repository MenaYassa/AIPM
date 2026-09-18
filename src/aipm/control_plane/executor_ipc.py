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
import re
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

# C6.4 capability identifiers carried on the wire. The legacy systemd-restart
# capability keeps its historical id for deployment compatibility; the update
# capability requires the engine binding fields below.
CAPABILITY_LEGACY_RESTART = "update_project_plan"
CAPABILITY_EXECUTE_UPDATE_PLAN = "execute_update_plan"
CAPABILITY_EXECUTE_SERVICE_UPDATE = "execute_service_update"
# C6.5-B read-only evidence marker. This is a message type, not an execution
# capability: it selects SELECT-only receipt lookup and can never drive the
# engine, consume confirmations, or mutate any state.
QUERY_MESSAGE_TYPE = "query_mutation_receipt"

_HEX = set("0123456789abcdef")
_SERVICE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_FORBIDDEN_CHARS = frozenset(";&|$`><\n\r\t\0'\"/\\:@ ")


class ExecutorIPCError(ValueError):
    """Raised when an IPC request fails structural validation."""


def validate_service_scope(scope: Any) -> tuple[str, ...]:
    """Validate that service_scope is a strictly bounded, non-empty tuple of canonical service names.

    Adversarial validation invariants (MC-6.15-C.3):
    1. Must be list or tuple.
    2. Cannot be empty.
    3. Every service name must be a non-empty string <= 64 chars.
    4. No duplicate service names.
    5. No path separators ('/' or '\\').
    6. No shell metacharacters (';', '&', '|', '$', '`', '>', '<', etc.).
    7. No whitespace tricks (no whitespace, leading/trailing or embedded).
    8. No arbitrary Docker flags (cannot start with '-').
    9. No image references embedded (no ':', '@', or '/').
    10. No scope expansion: converted to canonical immutable tuple exactly once.
    """
    if not isinstance(scope, (list, tuple)):
        raise ExecutorIPCError("service_scope must be a list or tuple")
    if not scope:
        raise ExecutorIPCError("service_scope cannot be empty")
    seen = set()
    cleaned = []
    for item in scope:
        if not isinstance(item, str) or not item:
            raise ExecutorIPCError("Invalid service name in service_scope: must be non-empty string")
        if item.startswith("-"):
            raise ExecutorIPCError(f"Invalid service name: cannot start with dash ({item!r})")
        if item != item.strip() or any(c in _FORBIDDEN_CHARS for c in item) or any(c.isspace() for c in item):
            raise ExecutorIPCError(f"Invalid service name: forbidden characters or whitespace in {item!r}")
        if not _SERVICE_NAME_PATTERN.match(item) or len(item) > 64:
            raise ExecutorIPCError(f"Invalid service name: does not match canonical pattern ({item!r})")
        if item in seen:
            raise ExecutorIPCError(f"Duplicate service name in service_scope: {item!r}")
        seen.add(item)
        cleaned.append(item)
    return tuple(cleaned)


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """Bounded execution request from the control plane.

    C6.4: ``plan_digest`` and ``confirmation_id`` are optional engine
    binding fields, REQUIRED together when ``capability_id`` is
    ``execute_update_plan`` and absent-or-ignored for the legacy restart
    capability. They name the canonical ``UpdatePlanIdentity`` digest of
    the exact plan the operator approved and the consumed confirmation
    reference; no commands, argv, paths, env, or shell content is ever
    transmitted.

    MC-6.15-C.3: ``service_scope`` carries the already-authorized canonical
    derived execution scope for Compose service updates.
    """

    action_id: str
    capability_id: str
    target_id: str
    contract_digest: str
    lease_id: str
    fencing_token: int
    plan_digest: str | None = None
    confirmation_id: str | None = None
    service_scope: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.service_scope is not None:
            validated = validate_service_scope(self.service_scope)
            if validated != self.service_scope:
                object.__setattr__(self, "service_scope", validated)

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
        plan_digest = payload.get("plan_digest")
        confirmation_id = payload.get("confirmation_id")
        service_scope_raw = payload.get("service_scope")
        capability_id = payload["capability_id"]
        if capability_id in (CAPABILITY_EXECUTE_UPDATE_PLAN, CAPABILITY_EXECUTE_SERVICE_UPDATE):
            if not isinstance(plan_digest, str) or not plan_digest:
                raise ExecutorIPCError(f"{capability_id} requires plan_digest")
            if not isinstance(confirmation_id, str) or not confirmation_id:
                raise ExecutorIPCError(f"{capability_id} requires confirmation_id")
        if capability_id == CAPABILITY_EXECUTE_SERVICE_UPDATE:
            if service_scope_raw is None:
                raise ExecutorIPCError("execute_service_update requires service_scope")
        for name, value, size in (
            ("plan_digest", plan_digest, 64),
            ("confirmation_id", confirmation_id, 32),
        ):
            if value is None:
                continue
            if not isinstance(value, str) or len(value) != size or not set(value) <= _HEX:
                raise ExecutorIPCError(f"Invalid {name}")
        service_scope: tuple[str, ...] | None = None
        if service_scope_raw is not None:
            service_scope = validate_service_scope(service_scope_raw)

        action_id = payload["action_id"]
        if not isinstance(action_id, str) or not action_id:
            raise ExecutorIPCError("Invalid action_id")
        contract_digest = payload["contract_digest"]
        if not isinstance(contract_digest, str) or not contract_digest:
            raise ExecutorIPCError("Invalid contract_digest")
        lease_id = payload["lease_id"]
        if not isinstance(lease_id, str) or not lease_id:
            raise ExecutorIPCError("Invalid lease_id")
        fencing_token = payload["fencing_token"]
        if not isinstance(fencing_token, int) or isinstance(fencing_token, bool):
            raise ExecutorIPCError("Invalid fencing_token")

        return cls(
            action_id=action_id,
            capability_id=capability_id,
            target_id=payload["target_id"],
            contract_digest=contract_digest,
            lease_id=lease_id,
            fencing_token=fencing_token,
            plan_digest=plan_digest if isinstance(plan_digest, str) else None,
            confirmation_id=confirmation_id if isinstance(confirmation_id, str) else None,
            service_scope=service_scope,
        )

    def to_json(self) -> bytes:
        payload = {
            "action_id": self.action_id,
            "capability_id": self.capability_id,
            "target_id": self.target_id,
            "contract_digest": self.contract_digest,
            "lease_id": self.lease_id,
            "fencing_token": self.fencing_token,
        }
        # Legacy frames stay byte-compatible with the deployed executor.
        if self.plan_digest is not None:
            payload["plan_digest"] = self.plan_digest
        if self.confirmation_id is not None:
            payload["confirmation_id"] = self.confirmation_id
        if self.service_scope is not None:
            payload["service_scope"] = list(self.service_scope)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


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


@dataclass(frozen=True, slots=True)
class ReceiptQueryRequest:
    """C6.5-B read-only receipt lookup request.

    Bounded to the authoritative attempt identity
    ``(action_id, fencing_token)``. No paths, SQL, filenames, commands,
    argv, environment, or filesystem selectors are accepted. Structural
    validation only: this is NOT business authorization.
    """

    action_id: str
    fencing_token: int

    @classmethod
    def from_json(cls, data: bytes) -> "ReceiptQueryRequest":
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ExecutorIPCError("Malformed JSON request") from exc
        if not isinstance(payload, dict):
            raise ExecutorIPCError("Request must be a JSON object")
        if set(payload) != {"message_type", "action_id", "fencing_token"}:
            raise ExecutorIPCError("Unexpected fields for query_mutation_receipt")
        if payload["message_type"] != QUERY_MESSAGE_TYPE:
            raise ExecutorIPCError("Unknown message type")
        action_id = payload["action_id"]
        if not isinstance(action_id, str) or len(action_id) != 64 or not set(action_id) <= _HEX:
            raise ExecutorIPCError("Invalid action_id")
        token = payload["fencing_token"]
        if not isinstance(token, int) or isinstance(token, bool) or token < 1:
            raise ExecutorIPCError("Invalid fencing_token")
        return cls(action_id=action_id, fencing_token=token)

    def to_json(self) -> bytes:
        payload = {
            "message_type": QUERY_MESSAGE_TYPE,
            "action_id": self.action_id,
            "fencing_token": self.fencing_token,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ReceiptQueryResponse:
    """Bounded read-only receipt evidence response.

    ``mutation_status`` is one of the existing MutationReceiptStore status
    values, ``not_found``, or ``evidence_unavailable``. The evidence map is
    the existing receipt representation (``MutationReceipt.safe_dict``
    equivalent fields); it is empty for non-found outcomes. No audit-file
    contents, no filesystem data.
    """

    mutation_status: str
    provider_code: str
    evidence: dict

    def to_json(self) -> bytes:
        return json.dumps({
            "mutation_status": self.mutation_status,
            "provider_code": self.provider_code,
            "evidence": self.evidence,
        }, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> "ReceiptQueryResponse":
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ExecutorIPCError("Malformed JSON response") from exc
        if not isinstance(payload, dict):
            raise ExecutorIPCError("Response must be a JSON object")
        if set(payload) != {"mutation_status", "provider_code", "evidence"}:
            raise ExecutorIPCError("Unexpected fields in receipt response")
        mutation_status = payload["mutation_status"]
        provider_code = payload["provider_code"]
        if not isinstance(mutation_status, str) or not mutation_status or len(mutation_status) > 32:
            raise ExecutorIPCError("Invalid mutation_status")
        if not isinstance(provider_code, str) or len(provider_code) > 128:
            raise ExecutorIPCError("Invalid provider_code")
        evidence = payload["evidence"]
        if not isinstance(evidence, dict) or len(evidence) > 16:
            raise ExecutorIPCError("Invalid evidence map")
        # Mirrors MutationReceipt.safe_dict() plus the two marker keys the
        # query handler appends. Every value must be a bounded scalar: str
        # <= 160, a real int, or None. No nested structures, no lists.
        allowed = {
            "receipt_id", "action_id", "fencing_token", "capability_id",
            "target_id", "contract_digest", "mutation_status", "provider_code",
            "created_at", "completed_at", "version", "protocol_version",
            "evidence_reference",
        }
        for key, value in evidence.items():
            if key not in allowed:
                raise ExecutorIPCError("Invalid receipt response fields")
            if value is None:
                continue
            if isinstance(value, str):
                if len(value) > 160:
                    raise ExecutorIPCError("Invalid evidence value")
            elif isinstance(value, int) and not isinstance(value, bool):
                continue
            else:
                raise ExecutorIPCError("Invalid evidence value")
        return cls(mutation_status=mutation_status, provider_code=provider_code, evidence=evidence)


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


def _is_query_frame(payload: bytes) -> bool:
    """Best-effort sniff for the query message type.

    Never raises: any parse failure means the frame is not a well-formed
    query, and it will be handled by the legacy ExecutionRequest parse
    (which produces the bounded refusal response).
    """
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return False
    return isinstance(parsed, dict) and parsed.get("message_type") == QUERY_MESSAGE_TYPE


class ExecutorIPCServer:
    """Unix domain socket server for the executor service."""

    __slots__ = ("_socket_path", "_allowed_caller_uids", "_handler", "_query_handler", "_sock", "_initialized")

    def __init__(
        self,
        *,
        socket_path: str = EXECUTOR_SOCKET_PATH,
        allowed_caller_uids: set[int] | None = None,
        handler: Callable[[ExecutionRequest], ExecutionResponse],
        query_handler: Callable[[ReceiptQueryRequest], ReceiptQueryResponse] | None = None,
    ) -> None:
        if handler is None:
            raise TypeError("handler is required")
        if query_handler is not None and not callable(query_handler):
            raise TypeError("query_handler must be callable or None")
        object.__setattr__(self, "_socket_path", socket_path)
        object.__setattr__(self, "_allowed_caller_uids", allowed_caller_uids)
        object.__setattr__(self, "_handler", handler)
        object.__setattr__(self, "_query_handler", query_handler)
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

    def serve_one(self) -> ExecutionResponse | ReceiptQueryResponse:
        """Accept one connection, validate, execute, and return the response."""
        conn, _addr = self._sock.accept()
        try:
            # Peer credential authentication
            caller_uid = get_peer_uid(conn)
            if self._allowed_caller_uids is not None and caller_uid not in self._allowed_caller_uids:
                # Drain the (already-sent) request frame before replying.
                # A client that writes its full request before reading would
                # otherwise race our early close and get EPIPE instead of the
                # refusal frame.
                self._drain_request(conn)
                response = ExecutionResponse(outcome="refused", provider_code="unauthorized_caller", action_id="", evidence_reference="")
                conn.sendall(encode_frame(response.to_json()))
                return response

            # Read and parse request
            payload = decode_frame(conn)
            if self._query_handler is not None and _is_query_frame(payload):
                # C6.5-B read-only evidence channel. The query handler is
                # observation, not the update capability: it never touches
                # the engine, confirmations, or leases. Without a composed
                # query handler the frame falls through to the legacy
                # ExecutionRequest parse below — byte-identical to the
                # pre-C6.5-B executor (forward/backward compatible). A
                # handler crash is classified as evidence_unavailable; the
                # accept loop must survive any single-connection failure.
                try:
                    query = ReceiptQueryRequest.from_json(payload)
                    response = self._query_handler(query)
                    conn.sendall(encode_frame(response.to_json()))
                    return response
                except Exception:  # noqa: BLE001 - never kill the accept loop
                    response = ReceiptQueryResponse(
                        mutation_status="evidence_unavailable",
                        provider_code="query_handler_failure",
                        evidence={},
                    )
                    try:
                        conn.sendall(encode_frame(response.to_json()))
                    except OSError:
                        pass
                    return response

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

    @staticmethod
    def _drain_request(conn: socket.socket, *, timeout: float = 0.5) -> None:
        """Best-effort bounded drain of a pending request frame.

        Reads at most one frame header + MAX_REQUEST_SIZE payload, with a
        short timeout. Any error is swallowed: this is a courtesy drain so
        the refusal frame is not lost to an early close (EPIPE race); the
        refusal path never parses or trusts the drained bytes.
        """
        try:
            conn.settimeout(timeout)
            header = conn.recv(4)
            if len(header) < 4:
                return
            (length,) = struct.unpack(">I", header)
            if length > MAX_REQUEST_SIZE:
                return
            remaining = length
            while remaining > 0:
                chunk = conn.recv(min(remaining, 1024))
                if not chunk:
                    return
                remaining -= len(chunk)
        except OSError:
            pass

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
        """Send one bounded request; transport failure maps to unknown_outcome.

        The request may have crossed the executor's mutation boundary before
        the failure, so a timeout, connection refusal, reset, or unreadable
        reply is classified as ``unknown_outcome`` (receipt evidence decides),
        never as ``failed``. Structural validation errors raised by
        ``to_json``'s frame encoding bounds still propagate.
        """

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
        except (OSError, ExecutorIPCError, json.JSONDecodeError, KeyError, TypeError):
            # Connection refused/reset, frame cap breach on the reply, or a
            # malformed reply: the mutation may already have happened.
            return ExecutionResponse(outcome="unknown_outcome", provider_code="transport_failure", action_id=request.action_id, evidence_reference="")
        finally:
            sock.close()

    def query_receipt(self, action_id: str, fencing_token: int, *, timeout: float = 30.0) -> ReceiptQueryResponse | None:
        """Read-only receipt evidence lookup; any failure maps to None.

        This query performs no mutation, so unlike :meth:`send` there is no
        unknown-outcome ambiguity: a transport failure, refusal, oversized
        reply, or malformed evidence simply yields ``None`` (evidence
        unavailable). Exceptions never propagate to the caller.
        """

        request = ReceiptQueryRequest(action_id=action_id, fencing_token=fencing_token)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self._socket_path)
            sock.sendall(encode_frame(request.to_json()))
            payload = decode_frame(sock)
            return ReceiptQueryResponse.from_json(payload)
        except Exception:  # noqa: BLE001 - observation never raises
            return None
        finally:
            sock.close()
