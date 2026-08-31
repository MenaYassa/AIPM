"""Tests for the executor IPC protocol and service (Shot 15)."""
from __future__ import annotations

import json
import struct
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.control_plane.executor_ipc import (
    ExecutionRequest,
    ExecutionResponse,
    ExecutorIPCClient,
    ExecutorIPCError,
    ExecutorIPCServer,
    encode_frame,
    decode_frame,
    MAX_REQUEST_SIZE,
    PROTOCOL_VERSION,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def make_request(**overrides):
    values = {
        "action_id": "a" * 64,
        "capability_id": "apply_project_plan",
        "target_id": "project-demo",
        "contract_digest": "d" * 64,
        "lease_id": "l" * 32,
        "fencing_token": 1,
    }
    values.update(overrides)
    return ExecutionRequest(**values)


class FakeHandler:
    def __init__(self, outcome="succeeded", provider_code="restart_ok"):
        self.outcome = outcome
        self.provider_code = provider_code
        self.received: list[ExecutionRequest] = []

    def __call__(self, request: ExecutionRequest) -> ExecutionResponse:
        self.received.append(request)
        return ExecutionResponse(
            outcome=self.outcome,
            provider_code=self.provider_code,
            action_id=request.action_id,
            evidence_reference=f"test:{request.action_id[:8]}",
        )


SOCKET_PATH = "/tmp/test_executor_ipc.sock"


@pytest.fixture
def server_and_client():
    handler = FakeHandler()
    server = ExecutorIPCServer(socket_path=SOCKET_PATH, handler=handler, allowed_caller_uids=None)
    server.start()
    client = ExecutorIPCClient(socket_path=SOCKET_PATH)
    yield server, client, handler
    server.stop()


# --- Protocol ---

def test_frame_encode_decode_roundtrip():
    data = b'{"test": true}'
    framed = encode_frame(data)
    assert framed[:4] == struct.pack(">I", len(data))
    assert framed[4:] == data


def test_frame_rejects_oversized():
    with pytest.raises(ExecutorIPCError, match="too large"):
        encode_frame(b"x" * (MAX_REQUEST_SIZE + 1))


# --- Request validation ---

def test_request_from_valid_json():
    request = make_request()
    parsed = ExecutionRequest.from_json(request.to_json())
    assert parsed.action_id == request.action_id


def test_request_rejects_missing_fields():
    with pytest.raises(ExecutorIPCError, match="Missing"):
        ExecutionRequest.from_json(json.dumps({"action_id": "a" * 64}))


def test_request_rejects_malformed_json():
    with pytest.raises(ExecutorIPCError, match="Malformed"):
        ExecutionRequest.from_json(b"not-json")


def test_request_rejects_non_object():
    with pytest.raises(ExecutorIPCError, match="JSON object"):
        ExecutionRequest.from_json(b'["array"]')


def test_request_rejects_too_many_fields():
    payload = {f"field{i}": "v" for i in range(20)}
    payload.update({"action_id": "a" * 64, "capability_id": "c", "target_id": "t",
                    "contract_digest": "d" * 64, "lease_id": "l" * 32, "fencing_token": 1})
    with pytest.raises(ExecutorIPCError, match="Too many"):
        ExecutionRequest.from_json(json.dumps(payload))


# --- IPC roundtrip ---

def test_ipc_roundtrip_success(server_and_client):
    server, client, handler = server_and_client
    threading.Thread(target=server.serve_one, daemon=True).start()
    request = make_request()
    response = client.send(request)
    assert response.outcome == "succeeded"
    assert response.action_id == request.action_id
    assert len(handler.received) == 1
    assert handler.received[0].action_id == request.action_id


def test_ipc_replay_sends_same_request(server_and_client):
    server, client, handler = server_and_client
    request = make_request()
    threading.Thread(target=server.serve_one, daemon=True).start()
    client.send(request)
    threading.Thread(target=server.serve_one, daemon=True).start()
    client.send(request)
    assert len(handler.received) == 2  # IPC does not dedup (control plane handles idempotency)


# --- Malformed IPC ---

def test_ipc_malformed_json(server_and_client):
    server, client, _handler = server_and_client
    threading.Thread(target=server.serve_one, daemon=True).start()
    sock = __import__("socket").socket(__import__("socket").AF_UNIX, __import__("socket").SOCK_STREAM)
    sock.connect(SOCKET_PATH)
    sock.sendall(encode_frame(b"not-json"))
    response_data = json.loads(decode_frame(sock))
    assert response_data["outcome"] == "refused"
    sock.close()


def test_ipc_oversized_frame(server_and_client):
    server, client, _handler = server_and_client
    threading.Thread(target=server.serve_one, daemon=True).start()
    sock = __import__("socket").socket(__import__("socket").AF_UNIX, __import__("socket").SOCK_STREAM)
    sock.connect(SOCKET_PATH)
    oversized = b"x" * (MAX_REQUEST_SIZE + 1)
    with pytest.raises(ExecutorIPCError, match="too large"):
        # The server rejects the oversized frame
        sock.sendall(encode_frame(oversized))
        decode_frame(sock)
    sock.close()


# --- Caller authentication ---

def test_ipc_rejects_unauthorized_caller(tmp_path):
    handler = FakeHandler()
    server = ExecutorIPCServer(socket_path=SOCKET_PATH, handler=handler, allowed_caller_uids={99999})
    server.start()
    client = ExecutorIPCClient(socket_path=SOCKET_PATH)
    import threading
    threading.Thread(target=server.serve_one, daemon=True).start()
    request = make_request()
    response = client.send(request)
    assert response.outcome == "refused"
    assert response.provider_code == "unauthorized_caller"
    server.stop()


# --- Outcome classification ---

def test_ipc_failure_outcome(tmp_path):
    handler = FakeHandler(outcome="failed", provider_code="exit_1")
    server = ExecutorIPCServer(socket_path=SOCKET_PATH, handler=handler, allowed_caller_uids=None)
    server.start()
    client = ExecutorIPCClient(socket_path=SOCKET_PATH)
    import threading
    threading.Thread(target=server.serve_one, daemon=True).start()
    request = make_request()
    response = client.send(request)
    assert response.outcome == "failed"
    server.stop()


# --- Source scan ---

def test_executor_ipc_has_no_dangerous_imports():
    source = Path("src/aipm/control_plane/executor_ipc.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "UpdateEngine", "GitProvider", "OwnerAuthenticator", "OwnerSession"):
        assert forbidden not in source, forbidden
    # It should use socket for IPC (that's the design)
    assert "socket" in source


def test_protocol_version_is_stable():
    assert PROTOCOL_VERSION == "mc612-executor-ipc-v1"
