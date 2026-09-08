"""C6.5-B: minimal read-only executor receipt evidence RPC.

Covers: the query wire contract (strict framing/bounds), server dispatch
(query vs execution framing, handler crash containment, forward/backward
compatibility), the composition-root SELECT-only query handler, the client
read-only lookup, the control-plane reconciliation truth table (evidence
only annotates UNKNOWN — never promotes), and the boundary guarantee that
reconciliation NEVER issues an execution request.
"""
from __future__ import annotations

import json
import struct
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.control_plane.executor_ipc import (
    MAX_REQUEST_SIZE,
    PROTOCOL_VERSION,
    QUERY_MESSAGE_TYPE,
    ExecutionResponse,
    ExecutorIPCClient,
    ExecutorIPCServer,
    ReceiptQueryRequest,
    ReceiptQueryResponse,
    encode_frame,
)
from aipm.control_plane.executor import (
    EXECUTION_CONTRACT_VERSION,
    ExecutionContract,
    ExecutorCapability,
)
from aipm.control_plane.service import _verification_version_value
from aipm.control_plane.mutation_receipt import (
    MutationReceiptStore,
    MutationStatus,
    RECEIPT_EVIDENCE_NOT_FOUND,
    RECEIPT_EVIDENCE_UNAVAILABLE,
)
from aipm.composition.executor_update import compose_receipt_query_handler

from tests.test_mc612_stage8_executor import (
    NOW,
    _confirmation_id,
    prepared_action,
)

ACTION = "a" * 64


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubQueryClient:
    """ExecutorIPCClient stand-in for CP-side tests.

    Only exposes ``query_receipt`` (read-only evidence). ``send`` raises:
    the reconciliation path must NEVER construct or send an execution
    request.
    """

    def __init__(self, response=None):
        self.queries: list[tuple[str, int]] = []
        self.response = response

    def send(self, request_):  # pragma: no cover - guarded by assertion
        raise AssertionError("reconciliation must never send an execution request")

    def query_receipt(self, action_id: str, fencing_token: int):
        self.queries.append((action_id, fencing_token))
        if self.response is None:
            return None
        return self.response


def _receipt_response(status: str, *, evidence: dict | None = None, action_id: str = ACTION, token: int = 1):
    return ReceiptQueryResponse(
        mutation_status=status,
        provider_code="test",
        evidence=evidence if evidence is not None else {
            "action_id": action_id,
            "fencing_token": token,
            "contract_digest": "c" * 64,
            "mutation_status": status,
        },
    )


# ---------------------------------------------------------------------------
# A. Wire contract
# ---------------------------------------------------------------------------


def test_query_request_roundtrips():
    parsed = ReceiptQueryRequest.from_json(ReceiptQueryRequest(action_id=ACTION, fencing_token=3).to_json())
    assert parsed.action_id == ACTION
    assert parsed.fencing_token == 3


def test_query_request_rejects_unknown_fields():
    payload = json.loads(ReceiptQueryRequest(action_id=ACTION, fencing_token=1).to_json())
    payload["extra"] = "x"
    with pytest.raises(Exception, match="Unexpected fields"):
        ReceiptQueryRequest.from_json(json.dumps(payload).encode())


def test_query_request_rejects_missing_message_type():
    payload = {"action_id": ACTION, "fencing_token": 1}
    with pytest.raises(Exception, match="Unexpected fields"):
        ReceiptQueryRequest.from_json(json.dumps(payload).encode())


def test_query_request_rejects_wrong_message_type():
    payload = {"message_type": "other", "action_id": ACTION, "fencing_token": 1}
    with pytest.raises(Exception, match="Unknown message type"):
        ReceiptQueryRequest.from_json(json.dumps(payload).encode())


def test_query_request_rejects_bad_action_id():
    for bad in ("A" * 64, "g" * 64, "a" * 63, "a" * 65, 42, None):
        payload = {"message_type": QUERY_MESSAGE_TYPE, "action_id": bad, "fencing_token": 1}
        with pytest.raises(Exception, match="Invalid action_id"):
            ReceiptQueryRequest.from_json(json.dumps(payload).encode())


def test_query_request_rejects_bad_fencing_token():
    for bad in (0, -1, "1", 1.5, True, None):
        payload = {"message_type": QUERY_MESSAGE_TYPE, "action_id": ACTION, "fencing_token": bad}
        with pytest.raises(Exception, match="Invalid fencing_token"):
            ReceiptQueryRequest.from_json(json.dumps(payload).encode())


def test_query_response_roundtrips_safe_dict_evidence(tmp_path: Path):
    receipts = MutationReceiptStore(tmp_path / "r.db")
    receipts.claim(
        action_id=ACTION, fencing_token=1, capability_id="execute_update_plan",
        target_id="project-demo", contract_digest="c" * 64, now="2026-01-01T00:00:00+00:00",
    )
    evidence = receipts.get(action_id=ACTION, fencing_token=1).safe_dict()
    evidence.update({"protocol_version": PROTOCOL_VERSION, "evidence_reference": ""})
    response = ReceiptQueryResponse(mutation_status="receipt_created", provider_code="", evidence=evidence)
    parsed = ReceiptQueryResponse.from_json(response.to_json())
    assert parsed.mutation_status == "receipt_created"
    assert parsed.evidence["version"] == "mc612-mutation-receipt-v1"
    assert parsed.evidence["protocol_version"] == PROTOCOL_VERSION
    assert parsed.evidence["completed_at"] is None


def test_query_response_rejects_unknown_evidence_key():
    response = ReceiptQueryResponse(mutation_status="receipt_created", provider_code="", evidence={"bogus": "x"})
    with pytest.raises(Exception, match="Invalid receipt response fields"):
        ReceiptQueryResponse.from_json(response.to_json())


def test_query_response_rejects_non_scalar_evidence_values():
    for bad in (["list"], {"nested": 1}, 1.5, True):
        response = ReceiptQueryResponse(mutation_status="receipt_created", provider_code="", evidence={"fencing_token": bad})
        with pytest.raises(Exception, match="Invalid evidence value"):
            ReceiptQueryResponse.from_json(response.to_json())


def test_query_response_rejects_oversized_strings():
    response = ReceiptQueryResponse(mutation_status="s" * 33, provider_code="", evidence={})
    with pytest.raises(Exception, match="Invalid mutation_status"):
        ReceiptQueryResponse.from_json(response.to_json())
    response = ReceiptQueryResponse(mutation_status="ok", provider_code="p" * 129, evidence={})
    with pytest.raises(Exception, match="Invalid provider_code"):
        ReceiptQueryResponse.from_json(response.to_json())
    response = ReceiptQueryResponse(mutation_status="ok", provider_code="", evidence={"receipt_id": "x" * 161})
    with pytest.raises(Exception, match="Invalid evidence value"):
        ReceiptQueryResponse.from_json(response.to_json())


def test_query_response_rejects_extra_top_level_fields():
    payload = json.loads(ReceiptQueryResponse(mutation_status="ok", provider_code="", evidence={}).to_json())
    payload["extra"] = 1
    with pytest.raises(Exception, match="Unexpected fields"):
        ReceiptQueryResponse.from_json(json.dumps(payload).encode())


# ---------------------------------------------------------------------------
# B. Server dispatch (real socket)
# ---------------------------------------------------------------------------


SOCKET_PATH = "/tmp/test_c65b_query_ipc.sock"


def _exec_handler(request_):
    return ExecutionResponse(outcome="succeeded", provider_code="restart_ok", action_id=request_.action_id, evidence_reference="")


def _query_recorder(response):
    calls = []

    def handler(request_):
        calls.append(request_)
        return response

    handler.calls = calls
    return handler


def _serve_once(server):
    done = threading.Event()

    def run():
        try:
            server.serve_one()
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    return done


def test_server_dispatches_query_frame(tmp_path: Path):
    receipts = MutationReceiptStore(tmp_path / "r.db")
    handler = _query_recorder(ReceiptQueryResponse(mutation_status="not_found", provider_code="", evidence={}))
    server = ExecutorIPCServer(socket_path=SOCKET_PATH, handler=_exec_handler, allowed_caller_uids=None, query_handler=handler)
    server.start()
    try:
        client = ExecutorIPCClient(socket_path=SOCKET_PATH)
        done = _serve_once(server)
        response = client.query_receipt(ACTION, 1)
        assert done.wait(timeout=5)
        assert response is not None
        assert response.mutation_status == "not_found"
        assert len(handler.calls) == 1
        assert handler.calls[0].action_id == ACTION
        assert handler.calls[0].fencing_token == 1
    finally:
        server.stop()


def test_server_query_handler_none_falls_through_to_legacy_refusal():
    server = ExecutorIPCServer(socket_path=SOCKET_PATH, handler=_exec_handler, allowed_caller_uids=None)
    server.start()
    try:
        client = ExecutorIPCClient(socket_path=SOCKET_PATH)
        done = _serve_once(server)
        response = client.query_receipt(ACTION, 1)
        assert done.wait(timeout=5)
        # No query handler: the frame is parsed as an ExecutionRequest and
        # refused on missing fields — forward/backward compatible.
        assert response is None
    finally:
        server.stop()


def test_server_query_handler_crash_yields_evidence_unavailable():
    def broken(request_):
        raise RuntimeError("boom")

    server = ExecutorIPCServer(socket_path=SOCKET_PATH, handler=_exec_handler, allowed_caller_uids=None, query_handler=broken)
    server.start()
    try:
        client = ExecutorIPCClient(socket_path=SOCKET_PATH)
        done = _serve_once(server)
        response = client.query_receipt(ACTION, 1)
        assert done.wait(timeout=5)
        assert response is not None
        assert response.mutation_status == "evidence_unavailable"
        assert response.provider_code == "query_handler_failure"
    finally:
        server.stop()


def test_server_accept_loop_survives_query_crash():
    def broken(request_):
        raise RuntimeError("boom")

    server = ExecutorIPCServer(socket_path=SOCKET_PATH, handler=_exec_handler, allowed_caller_uids=None, query_handler=broken)
    server.start()
    try:
        client = ExecutorIPCClient(socket_path=SOCKET_PATH)
        for _ in range(3):
            done = _serve_once(server)
            response = client.query_receipt(ACTION, 1)
            assert done.wait(timeout=5)
            assert response.mutation_status == "evidence_unavailable"
        # Legacy execution path still works after repeated query crashes.
        done = _serve_once(server)
        response = client.send(_legacy_request())
        assert done.wait(timeout=5)
        assert response.outcome == "succeeded"
    finally:
        server.stop()


def _legacy_request():
    from aipm.control_plane.executor_ipc import ExecutionRequest

    return ExecutionRequest(
        action_id=ACTION,
        capability_id="update_project_plan",
        target_id="project-demo",
        contract_digest="d" * 64,
        lease_id="l" * 32,
        fencing_token=1,
    )


def test_server_unauthorized_uid_refuses_query():
    import os

    handler = _query_recorder(ReceiptQueryResponse(mutation_status="not_found", provider_code="", evidence={}))
    server = ExecutorIPCServer(socket_path=SOCKET_PATH, handler=_exec_handler, allowed_caller_uids={os.geteuid() + 1}, query_handler=handler)
    server.start()
    try:
        client = ExecutorIPCClient(socket_path=SOCKET_PATH)
        done = _serve_once(server)
        response = client.query_receipt(ACTION, 1)
        assert done.wait(timeout=5)
        assert response is None  # refusal frame fails strict query parse
        assert handler.calls == []
    finally:
        server.stop()


def test_server_query_rejects_malformed_and_oversized_frames():
    handler = _query_recorder(ReceiptQueryResponse(mutation_status="not_found", provider_code="", evidence={}))
    server = ExecutorIPCServer(socket_path=SOCKET_PATH, handler=_exec_handler, allowed_caller_uids=None, query_handler=handler)
    server.start()
    try:
        raw = _raw_connect()
        raw.sendall(encode_frame(b"not-json"))
        done = _serve_once(server)
        assert done.wait(timeout=5)
        payload = _read_frame(raw)
        # Malformed non-query frame goes to the legacy parse: refusal.
        assert b"refused" in payload
        assert handler.calls == []

        raw2 = _raw_connect()
        raw2.sendall(struct.pack(">I", MAX_REQUEST_SIZE + 1) + b"x" * 64)
        done = _serve_once(server)
        assert done.wait(timeout=5)
        payload = _read_frame(raw2)
        # Frame cap: bounded refusal (never a crash, never the query handler).
        assert b"refused" in payload
        assert b"Frame too large" in payload
        assert handler.calls == []
    finally:
        server.stop()


def _raw_connect():
    import socket

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(SOCKET_PATH)
    return sock


def _read_frame(sock) -> bytes:
    header = b""
    while len(header) < 4:
        chunk = sock.recv(4 - len(header))
        if not chunk:
            return b""
        header += chunk
    (length,) = struct.unpack(">I", header)
    if length > MAX_REQUEST_SIZE:
        return b""
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            break
        payload += chunk
    return payload


# ---------------------------------------------------------------------------
# C. Composition-root query handler
# ---------------------------------------------------------------------------


def test_handler_requires_receipts_contract():
    with pytest.raises(TypeError, match="MutationReceiptStore"):
        compose_receipt_query_handler(receipts=None)
    with pytest.raises(TypeError, match="MutationReceiptStore"):
        compose_receipt_query_handler(receipts=object())


def test_handler_found_mirrors_safe_dict(tmp_path: Path):
    receipts = MutationReceiptStore(tmp_path / "r.db")
    receipts.claim(
        action_id=ACTION, fencing_token=7, capability_id="execute_update_plan",
        target_id="project-demo", contract_digest="c" * 64, now="2026-01-01T00:00:00+00:00",
    )
    receipts.complete(
        action_id=ACTION, fencing_token=7, status=MutationStatus.MUTATION_SUCCEEDED,
        provider_code="update_ok", now="2026-01-01T00:01:00+00:00",
    )
    handler = compose_receipt_query_handler(receipts=receipts)
    response = handler(ReceiptQueryRequest(action_id=ACTION, fencing_token=7))
    assert response.mutation_status == "mutation_succeeded"
    assert response.provider_code == "update_ok"
    expected = receipts.get(action_id=ACTION, fencing_token=7).safe_dict()
    for key, value in expected.items():
        assert response.evidence[key] == value
    assert response.evidence["protocol_version"] == PROTOCOL_VERSION
    assert response.evidence["evidence_reference"] == ""


def test_handler_not_found(tmp_path: Path):
    handler = compose_receipt_query_handler(receipts=MutationReceiptStore(tmp_path / "r.db"))
    response = handler(ReceiptQueryRequest(action_id=ACTION, fencing_token=1))
    assert response.mutation_status == RECEIPT_EVIDENCE_NOT_FOUND
    assert response.evidence == {}


def test_handler_corrupt_db_yields_evidence_unavailable(tmp_path: Path):
    import sqlite3

    db_file = tmp_path / "r.db"
    receipts = MutationReceiptStore(db_file)
    handler = compose_receipt_query_handler(receipts=receipts)
    db_file.write_bytes(b"corrupt, not a database")
    response = handler(ReceiptQueryRequest(action_id=ACTION, fencing_token=1))
    assert response.mutation_status == RECEIPT_EVIDENCE_UNAVAILABLE
    assert response.provider_code == "receipt_store_failure"
    assert response.evidence == {}
    assert sqlite3.Error is not None  # silence lint on unused import intent


def test_handler_is_select_only(tmp_path: Path):
    class _RecordingStore:
        def __init__(self, inner):
            self.inner = inner
            self.calls = []

        def get(self, **kwargs):
            self.calls.append(("get", kwargs))
            return self.inner.get(**kwargs)

        def __getattr__(self, name):
            def _forbidden(*args, **kwargs):  # pragma: no cover - guarded below
                raise AssertionError(f"query handler must not call {name}")
            return _forbidden

    inner = MutationReceiptStore(tmp_path / "r.db")
    inner.claim(
        action_id=ACTION, fencing_token=2, capability_id="execute_update_plan",
        target_id="project-demo", contract_digest="c" * 64, now="2026-01-01T00:00:00+00:00",
    )
    store = _RecordingStore(inner)
    handler = compose_receipt_query_handler(receipts=store)
    response = handler(ReceiptQueryRequest(action_id=ACTION, fencing_token=2))
    assert response.mutation_status == "receipt_created"
    assert [name for name, _ in store.calls] == ["get"]


# ---------------------------------------------------------------------------
# D. Client read-only lookup
# ---------------------------------------------------------------------------


def test_client_query_roundtrip(tmp_path: Path):
    receipts = MutationReceiptStore(tmp_path / "r.db")
    receipts.claim(
        action_id=ACTION, fencing_token=5, capability_id="execute_update_plan",
        target_id="project-demo", contract_digest="c" * 64, now="2026-01-01T00:00:00+00:00",
    )
    handler = compose_receipt_query_handler(receipts=receipts)
    server = ExecutorIPCServer(socket_path=SOCKET_PATH, handler=_exec_handler, allowed_caller_uids=None, query_handler=handler)
    server.start()
    try:
        client = ExecutorIPCClient(socket_path=SOCKET_PATH)
        done = _serve_once(server)
        response = client.query_receipt(ACTION, 5)
        assert done.wait(timeout=5)
        assert response is not None
        assert response.mutation_status == "receipt_created"
        assert response.evidence["fencing_token"] == 5
        assert response.evidence["contract_digest"] == "c" * 64
        assert response.evidence["protocol_version"] == PROTOCOL_VERSION
    finally:
        server.stop()


def test_client_query_transport_failure_yields_none():
    client = ExecutorIPCClient(socket_path="/tmp/c65b-nonexistent.sock")
    assert client.query_receipt(ACTION, 1) is None


def test_client_query_refusal_frame_yields_none():
    # Server with no query handler replies with a legacy ExecutionResponse
    # refusal; the strict query parse fails and the client maps it to None.
    server = ExecutorIPCServer(socket_path=SOCKET_PATH, handler=_exec_handler, allowed_caller_uids=None)
    server.start()
    try:
        client = ExecutorIPCClient(socket_path=SOCKET_PATH)
        done = _serve_once(server)
        assert client.query_receipt(ACTION, 1) is None
        assert done.wait(timeout=5)
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# E. Control-plane reconciliation truth table
# ---------------------------------------------------------------------------


def _drive_to_unknown(tmp_path: Path, *, title="New title"):
    """Drive one action to UNKNOWN_OUTCOME with the plan left at pre-state.

    The action never reached the executor (the fixture bypasses it), so the
    contract digest is NOT durably bound. Tests that exercise receipt
    evidence call :func:`_bind_digest` to simulate the binding that
    ``Executor.execute`` performs at LEASED. Returns (service, db, ledger,
    plans, clock, session, identity, running).
    """
    service, db, ledger, plans, clock, session, decision, identity, snapshot = prepared_action(tmp_path)
    repo = service._actions
    action = repo.get_action(identity.action_id)
    _lease, leased = repo.acquire_lease(identity.action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
    running = repo.begin_execution(
        identity.action_id,
        expected_version=leased.version,
        confirmation_id=_confirmation_id(db, identity.action_id),
        now=NOW + timedelta(minutes=3),
    )
    repo.mark_outcome(
        identity.action_id, expected_version=running.version,
        outcome="unknown_outcome", now=NOW + timedelta(minutes=3),
    )
    return service, db, ledger, plans, clock, session, identity, running


def _bind_digest(service, identity, digest: str) -> str:
    """Bind the reconcile-time contract digest durably (immutable at LEASED).

    The reconcile contract is rebuilt deterministically from durable state,
    so its digest equals the digest the executor would have bound at
    LEASED for the same binding material. Binding it here simulates the
    durable evidence the executor writes before its mutation.
    """
    service._actions.bind_contract_evidence(
        identity.action_id,
        expected_version=service._actions.get_action(identity.action_id).version,
        contract_version=EXECUTION_CONTRACT_VERSION,
        capability_version="1",
        contract_digest=digest,
        now=NOW + timedelta(minutes=3),
    )
    return digest


def _reconcile_contract_digest(service, session, identity) -> str:
    """Compute the digest of the contract reconcile_action will build."""
    action = service._actions.get_action(identity.action_id)
    decision = service._actions.get_decision(action.decision_id)
    lease = service._actions.active_lease(identity.action_id, now=NOW + timedelta(minutes=4)) or service._actions.last_lease(identity.action_id)
    kill_switch_epoch = service._kill_switches.switch(action.scope.environment).epoch if service._kill_switches is not None else 1
    contract = ExecutionContract(
        contract_version=EXECUTION_CONTRACT_VERSION,
        action_id=action.action_id,
        action_version=action.version,
        operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
        target_id=action.scope.target_id,
        environment=action.scope.environment,
        plan_id=action.plan_id,
        expected_plan_revision=action.plan_revision,
        expected_plan_digest=decision.action_identity.target_digest,
        mutation_fields=tuple(decision.request.mutation_metadata),
        snapshot_id=action.snapshot_id or "unknown",
        decision_id=decision.decision_id,
        confirmation_id=service._confirmation_id_for(identity.action_id) or "unknown",
        policy_version=action.scope.policy_version,
        verification_version=_verification_version_value(),
        kill_switch_epoch=kill_switch_epoch,
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        expires_at=action.expires_at,
    )
    return contract.digest()


def _install_client(service, response):
    stub = _StubQueryClient(response)
    object.__setattr__(service, "_executor_ipc_client", stub)
    return stub


def _receipt_evidence(status: str, *, digest="c" * 64, action_id: str = ACTION, token: int = 1) -> ReceiptQueryResponse:
    """A realistic ReceiptQueryResponse the IPC client would return."""
    return ReceiptQueryResponse(
        mutation_status=status,
        provider_code="test",
        evidence={
            "action_id": action_id,
            "fencing_token": token,
            "contract_digest": digest,
            "mutation_status": status,
        },
    )


def _drive_to_inconclusive(tmp_path: Path):
    """Drive one action to UNKNOWN_OUTCOME into an INCONCLUSIVE plan state.

    The plan is advanced to revision 2 with a DIFFERENT title than the
    contract authorized: not post-state (fields mismatch), not pre-state
    (revision moved) — exactly the branch-3 scenario where receipt evidence
    is consulted and the outcome must stay UNKNOWN.
    """
    service, db, ledger, plans, clock, session, identity, running = _drive_to_unknown(tmp_path)
    plans.update("project-demo", expected_revision=1, fields={"title": "Other title"}, now=NOW + timedelta(minutes=3))
    return service, db, ledger, plans, clock, session, identity, running


def test_no_ipc_client_legacy_behavior_unchanged(tmp_path: Path):
    service, db, ledger, plans, clock, session, identity, running = _drive_to_inconclusive(tmp_path)
    result = service.reconcile_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    assert result.outcome.value == "unknown_outcome"  # inconclusive plan state: branch 3
    assert result.receipt_status is None  # no client, no evidence
    assert plans.read("project-demo").revision == 2
    db.close()


def test_branch1_post_state_succeeds_without_query(tmp_path: Path):
    service, db, ledger, plans, clock, session, identity, running = _drive_to_unknown(tmp_path)
    plans.update("project-demo", expected_revision=1, fields={"title": "New title"}, now=NOW + timedelta(minutes=3))
    stub = _install_client(service, _receipt_evidence("mutation_succeeded", digest=_reconcile_contract_digest(service, session, identity)))
    result = service.reconcile_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    assert result.outcome.value == "mutation_succeeded"
    assert result.receipt_status is None  # plan-store observation decides; no query
    assert stub.queries == []  # spec D2: branch 1 never queries
    assert plans.read("project-demo").revision == 2
    db.close()


def test_branch2_pre_state_not_started_without_query(tmp_path: Path):
    service, db, ledger, plans, clock, session, identity, running = _drive_to_unknown(tmp_path)
    stub = _install_client(service, _receipt_evidence("mutation_succeeded", digest=_reconcile_contract_digest(service, session, identity)))
    result = service.reconcile_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    assert result.outcome.value == "mutation_not_started"
    assert result.receipt_status is None
    assert stub.queries == []  # spec D2: branch 2 never queries
    db.close()


def test_branch3_unknown_with_matching_receipt_status(tmp_path: Path):
    service, db, ledger, plans, clock, session, identity, running = _drive_to_inconclusive(tmp_path)
    _bind_digest(service, identity, _reconcile_contract_digest(service, session, identity))
    digest = _reconcile_contract_digest(service, session, identity)
    stub = _install_client(service, _receipt_evidence("mutation_succeeded", digest=digest, action_id=identity.action_id))
    result = service.reconcile_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    assert result.outcome.value == "unknown_outcome"  # never promoted
    assert result.receipt_status == "mutation_succeeded"
    assert stub.queries == [(identity.action_id, 1)]
    # No lifecycle/outcome change happened: still UNKNOWN, repeat-safe.
    again = service.reconcile_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    assert again.outcome.value == "unknown_outcome"
    assert again.receipt_status == "mutation_succeeded"
    db.close()


def test_branch3_receipt_statuses_surface(tmp_path: Path):
    for index, status in enumerate(("receipt_created", "mutation_failed", "unknown_outcome", "not_found", "evidence_unavailable")):
        case_dir = tmp_path / f"case{index}"
        case_dir.mkdir()
        service, db, ledger, plans, clock, session, identity, running = _drive_to_inconclusive(case_dir)
        _bind_digest(service, identity, _reconcile_contract_digest(service, session, identity))
        stub = _install_client(service, _receipt_evidence(status, digest=_reconcile_contract_digest(service, session, identity), action_id=identity.action_id))
        result = service.reconcile_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
        assert result.outcome.value == "unknown_outcome", status
        assert result.receipt_status == status, status
        db.close()


def test_branch3_mismatched_evidence_yields_none(tmp_path: Path):
    for index, kwargs in enumerate(
        (
            {"digest": "e" * 64},                 # wrong contract digest
            {"action_id": "b" * 64},              # wrong action
            {"token": 99},                        # wrong fencing token
        )
    ):
        case_dir = tmp_path / f"case{index}"
        case_dir.mkdir()
        service, db, ledger, plans, clock, session, identity, running = _drive_to_inconclusive(case_dir)
        _bind_digest(service, identity, _reconcile_contract_digest(service, session, identity))
        stub = _install_client(service, _receipt_evidence("mutation_succeeded", **kwargs))
        result = service.reconcile_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
        assert result.outcome.value == "unknown_outcome"
        assert result.receipt_status is None, kwargs
        db.close()


def test_branch3_malformed_and_failing_evidence_yields_none(tmp_path: Path):
    service, db, ledger, plans, clock, session, identity, running = _drive_to_inconclusive(tmp_path)
    _bind_digest(service, identity, _reconcile_contract_digest(service, session, identity))

    class _BadResponse:
        def __init__(self, kwargs):
            self.__dict__.update(kwargs)

    cases = [
        _BadResponse({"mutation_status": 7, "provider_code": "", "evidence": {}}),
        _BadResponse({"mutation_status": "mutation_succeeded", "provider_code": "", "evidence": "not-a-dict"}),
        _BadResponse({"mutation_status": "mutation_succeeded", "provider_code": "", "evidence": {}}),  # evidence lacks correlation keys
        None,  # transport failure
    ]

    class _Raising:
        def query_receipt(self, action_id, fencing_token):
            raise RuntimeError("transport")

    for stub in cases + [_Raising()]:
        object.__setattr__(service, "_executor_ipc_client", stub)
        result = service.reconcile_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
        assert result.outcome.value == "unknown_outcome"
        assert result.receipt_status is None, stub
    db.close()


def test_branch3_unbound_digest_yields_none(tmp_path: Path):
    service, db, ledger, plans, clock, session, identity, running = _drive_to_inconclusive(tmp_path)
    # The fixture never binds a digest (binding happens inside the real
    # executor); this is the crash-before-binding state.
    assert service._actions.get_contract_evidence(identity.action_id)["contract_digest"] is None
    stub = _install_client(service, _receipt_evidence("mutation_succeeded", digest="c" * 64))
    result = service.reconcile_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    assert result.outcome.value == "unknown_outcome"
    assert result.receipt_status is None
    assert stub.queries == []  # no digest: no query at all
    db.close()


def test_reconciliation_never_acquires_lease_or_changes_lifecycle(tmp_path: Path):
    service, db, ledger, plans, clock, session, identity, running = _drive_to_inconclusive(tmp_path)
    _bind_digest(service, identity, _reconcile_contract_digest(service, session, identity))
    stub = _install_client(service, _receipt_evidence("mutation_succeeded", digest=_reconcile_contract_digest(service, session, identity), action_id=identity.action_id))
    before_version = service._actions.get_action(identity.action_id).version
    before_state = service._actions.get_action(identity.action_id).state
    result = service.reconcile_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    assert result.outcome.value == "unknown_outcome"
    action_after = service._actions.get_action(identity.action_id)
    assert action_after.version == before_version
    assert action_after.state is before_state
    assert service._actions.active_lease(identity.action_id, now=NOW + timedelta(minutes=4)) is not None
    db.close()


# ---------------------------------------------------------------------------
# F. Boundary: reconciliation NEVER executes
# ---------------------------------------------------------------------------


def test_reconciliation_never_sends_execution_request(tmp_path: Path):
    service, db, ledger, plans, clock, session, identity, running = _drive_to_inconclusive(tmp_path)
    _bind_digest(service, identity, _reconcile_contract_digest(service, session, identity))
    stub = _install_client(service, _receipt_evidence("unknown_outcome", digest=_reconcile_contract_digest(service, session, identity), action_id=identity.action_id))
    service.reconcile_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    # _StubQueryClient.send raises AssertionError; reaching here proves
    # reconciliation never called send().


def test_executor_source_never_imports_ipc_or_requests():
    source = Path("src/aipm/control_plane/executor.py").read_text(encoding="utf-8")
    assert "executor_ipc" not in source
    assert "ExecutionRequest" not in source
