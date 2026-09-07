"""C6.2: the composed update-runtime seam (binding → contract → engine).

Covers the mandated scenarios:

* happy path: one full disposable vertical — canonical approval, snapshot,
  lease, gated in-process execution, exactly-once confirmation consumption,
  and the real update engine (git transaction + runtime script +
  verification) driven through the composition-root adapter.
* rejections (each asserts NO project/plan/runtime mutation):
  1 wrong project, 2 wrong plan digest, 3 stale control-plane plan,
  4 expired confirmation, 5 already-consumed confirmation,
  6 expired lease, 7 wrong fencing token (+ tamper containment),
  8 engaged kill switch, 9 malformed IPC request, 10 unauthorized IPC
  caller, 11 tampered execution contract, 12 client-supplied foreign
  digest (approval refusal; the execution seam has no digest parameter),
  13 engine-side world change after approval (TOCTOU, refused
  pre-mutation), 14 replay never re-invokes the runtime, 15 IPC timeout
  → unknown_outcome without retry, 16 unknown outcome is never retried.
* boundary: the adapter source is pure composition (no I/O, no secrets).
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from aipm.control_plane.executor import (
    EXECUTION_CONTRACT_VERSION,
    ExecutionContract,
    ExecutorCapability,
)
from aipm.control_plane.executor_ipc import (
    ExecutionRequest,
    ExecutorIPCClient,
    ExecutorIPCServer,
    encode_frame,
    decode_frame,
)
from aipm.control_plane.models import UpdateExecutionBinding
from aipm.control_plane.project_plan import Environment, ProjectPlan
from aipm.services.update.plan_identity import UpdatePlanIdentity
from aipm.services.update.runtime_adapter import compose_update_runtime

from tests.test_mc612_stage9_transport import NOW, SECRET, VERIFIER, _Clock, db_path
from tests.update_fixtures import MARKER_RUNTIME_SCRIPT, fetch_origin, make_remote_commit, make_repo, rev_parse_head

PROJECT_ID = "full-update-demo"
OTHER_PROJECT_ID = "other-update-demo"


# ---------------------------------------------------------------------------
# Harness: full disposable vertical on canonical durable stores
# ---------------------------------------------------------------------------


def _engine_plan_digest(engine) -> str:
    """The authoritative digest: the UpdatePlanIdentity digest space of the
    exact plan the engine will execute (dry_run=False, the execution mode).

    The composition root injects this as the ``current_plan_digest`` port so
    approval verification, the durable binding, and the engine's recomputed
    identity digest all speak one digest space (TOCTOU-real: the engine
    re-plans at execution time and refuses a changed world pre-mutation).
    """

    return UpdatePlanIdentity.from_plan(engine.plan_update(PROJECT_ID, dry_run=False)).digest()


def _full_harness(tmp_path: Path) -> SimpleNamespace:
    from aipm.control_plane.approval import OwnerConfirmationService
    from aipm.control_plane.audit import SQLiteAuditLedger
    from aipm.control_plane.kill_switch import KillSwitchRegistry
    from aipm.control_plane.owner_auth import Argon2idVerifier, OwnerAuthenticator
    from aipm.control_plane.planner import PlanOnlyPlanner
    from aipm.control_plane.policy import AuthorizationPolicy
    from aipm.control_plane.session import OwnerSessionStore
    from aipm.control_plane.storage import (
        ControlPlaneDatabase,
        SQLiteActionRepository,
        SQLiteProjectPlanStore,
    )
    from aipm.control_plane.storage.sqlite_store import SQLiteKillSwitchStore
    from aipm.control_plane.service import OwnerControlPlaneService

    clock = _Clock(NOW)
    project = make_repo(tmp_path, name=PROJECT_ID, runtime_script=MARKER_RUNTIME_SCRIPT)
    engine = build_engine(tmp_path, project)
    db = ControlPlaneDatabase(db_path(tmp_path), clock=clock)
    ledger = SQLiteAuditLedger(db)
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=clock)
    sessions = OwnerSessionStore(clock=clock)
    policy = AuthorizationPolicy(policy_version="policy-v1", allowed_scopes=frozenset({(PROJECT_ID, "staging")}))
    confirmations = OwnerConfirmationService(clock=clock)
    plans = SQLiteProjectPlanStore(db)
    for target_id in (PROJECT_ID, OTHER_PROJECT_ID):
        plans.create(
            ProjectPlan.create(
                target_id=target_id,
                environment=Environment.STAGING,
                title="Old title",
                objective="Objective",
                now=NOW,
            )
        )
    planner = PlanOnlyPlanner(clock=clock, target_allow_list=frozenset({PROJECT_ID}))
    actions = SQLiteActionRepository(db, audit=ledger)
    kill_switches = KillSwitchRegistry(clock=clock, store=SQLiteKillSwitchStore(db))
    kill_switches.disengage(Environment.STAGING, reason="test window", now=NOW)

    def _current_plan_digest(target_id: str) -> str:
        if target_id != PROJECT_ID:
            # Unknown target: a deterministic non-matching digest fails the
            # approval's verification closed (no exception leakage).
            return "f" * 64
        return _engine_plan_digest(engine)

    runtime_calls: list[UpdateExecutionBinding] = []
    runtime_results: list[dict] = []
    real_runtime = compose_update_runtime(engine)

    def _update_runtime(binding) -> dict:
        runtime_calls.append(binding)
        result = real_runtime(binding)
        runtime_results.append(result)
        return result

    service = OwnerControlPlaneService(
        authenticator=authenticator,
        sessions=sessions,
        policy=policy,
        confirmations=confirmations,
        plans=plans,
        planner=planner,
        audit=ledger,
        actions=actions,
        kill_switches=kill_switches,
        clock=clock,
        execution_mode="test",
        current_plan_digest=_current_plan_digest,
        update_runtime=_update_runtime,
    )
    return SimpleNamespace(
        service=service,
        plans=plans,
        engine=engine,
        project=project,
        clock=clock,
        runtime_calls=runtime_calls,
        runtime_results=runtime_results,
        kill_switches=kill_switches,
        marker=tmp_path / "runtime-marker.txt",
    )


def _approve(h: SimpleNamespace, *, target_id: str = PROJECT_ID, digest: str | None = None, key: str = "k1") -> dict:
    session = h.service.login(SECRET)
    result = h.service.approve_update_plan(
        session.session_id,
        target_id=target_id,
        environment="staging",
        presented_digest=digest if digest is not None else _engine_plan_digest(h.engine),
        idempotency_key=key,
    )
    return {"session_id": session.session_id, **result}


def _prepared(tmp_path: Path) -> SimpleNamespace:
    """Harness with one approved + snapshot-captured update action."""

    h = _full_harness(tmp_path)
    approval = _approve(h)
    assert approval["allowed"] is True
    h.service.capture_snapshot(approval["session_id"], approval["action_id"], now=NOW)
    h.approval = approval
    return h


def _engine_world_unchanged(h: SimpleNamespace, head_before: str | None = None, *, expected_revision: int = 1) -> None:
    work = Path(h.project.path)
    assert (work / "config.txt").read_text(encoding="utf-8") == "v1\n"
    assert not (work / "docs.md").exists()
    assert not h.marker.exists()
    if head_before is not None:
        assert rev_parse_head(h.project) == head_before
    assert h.plans.read(PROJECT_ID).revision == expected_revision
    assert len(h.runtime_calls) == 0


def _canonical_contract(h: SimpleNamespace, action_id: str) -> ExecutionContract:
    """Rebuild the exact canonical contract the service derives from durable
    state (mirroring OwnerControlPlaneService.execute_action's construction)
    so tests can exercise the executor's refusal paths directly."""

    from aipm.control_plane.verification import VERIFICATION_VERSION

    service = h.service
    action = service._actions.get_action(action_id)
    decision = service._actions.get_decision(action.decision_id)
    confirmation_id = next(
        binding.confirmation_id
        for binding in service._confirmations.store.values()
        if binding.action_id == action_id
    )
    snapshot = service._snapshot_repo.snapshot_for_action(action_id)
    lease = service._actions.active_lease(action_id, now=h.clock())
    return ExecutionContract(
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
        snapshot_id=snapshot.snapshot_id,
        decision_id=decision.decision_id,
        confirmation_id=confirmation_id,
        policy_version=action.scope.policy_version,
        verification_version=VERIFICATION_VERSION,
        kill_switch_epoch=service._kill_switches.switch(action.scope.environment).epoch,
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        expires_at=action.expires_at,
    )


def build_engine(tmp_path: Path, project):
    from aipm.services.backup.engine import BackupEngine
    from aipm.services.update.audit import AuditService
    from aipm.services.update.engine import UpdateEngine
    from aipm.services.update.rollback import RollbackManager
    from aipm.services.update.verifier import UpdateVerifier

    from tests.update_fixtures import FixedProjectService, GuardCompose, GitService, hermetic_health_engine

    git_service = GitService()
    return UpdateEngine(
        project_service=FixedProjectService(project, git_service),
        git_service=git_service,
        backup_engine=BackupEngine(tmp_path / "backups"),
        compose_provider=GuardCompose(),
        health_engine=hermetic_health_engine(),
        audit_service=AuditService(tmp_path / "audit"),
        rollback_manager=RollbackManager(),
        verifier=UpdateVerifier(),
    )


# ---------------------------------------------------------------------------
# Happy path: the full disposable vertical
# ---------------------------------------------------------------------------


def test_full_vertical_happy_path_end_to_end(tmp_path, monkeypatch):
    h = _full_harness(tmp_path)
    monkeypatch.setenv("AIPM_INTEGRATION_MARKER", str(h.marker))
    approval = _approve(h)
    assert approval["allowed"] is True
    assert approval["action_id"]
    assert approval["confirmation_id"]

    head_before = rev_parse_head(h.project)
    result = h.service.run_approved_update(approval["session_id"], action_id=approval["action_id"])

    assert result["executed"] is True
    assert result["outcome"] == "verification_succeeded"
    assert result["lifecycle_state"] == "verified_success"

    # The real engine ran the real runtime exactly once (marker written by
    # the subprocess OUTSIDE the worktree).
    assert h.marker.exists()
    assert h.marker.read_text(encoding="utf-8") == "started"
    assert len(h.runtime_calls) == 1

    # The runtime received the trusted durable binding, never client input.
    binding = h.runtime_calls[0]
    assert isinstance(binding, UpdateExecutionBinding)
    assert binding.project_name == PROJECT_ID
    assert binding.plan_digest == _engine_plan_digest(h.engine)
    assert binding.confirmation_id == approval["confirmation_id"]
    # The adapter's bounded result carries no secrets or engine types.
    assert set(h.runtime_results[0]) == {"project_name", "outcome", "mode", "risk", "audit_path"}
    assert h.runtime_results[0]["outcome"] == "success"

    # Control-plane canonical mutation: exactly one revision advance.
    assert h.plans.read(PROJECT_ID).revision == 2

    # Exactly-once confirmation consumption.
    confirmation = h.service._confirmations.store.get(approval["confirmation_id"])
    assert confirmation.state.value == "consumed"

    # Durable lease released at the terminal outcome.
    assert h.service._actions.active_lease(approval["action_id"], now=h.clock()) is None

    # Engine-side git transaction: clean pull (no remote divergence), so the
    # worktree content is unchanged and HEAD did not move.
    assert rev_parse_head(h.project) == head_before
    assert (Path(h.project.path) / "config.txt").read_text(encoding="utf-8") == "v1\n"

    # Real engine artifacts landed under the disposable fixture tree.
    assert (tmp_path / "backups").is_dir()
    audit_files = list((tmp_path / "audit").glob("*.json"))
    assert len(audit_files) == 1



# ---------------------------------------------------------------------------
# Rejection 1-2: approval boundary (wrong project / wrong digest)
# ---------------------------------------------------------------------------


def test_approval_refuses_wrong_project(tmp_path):
    h = _full_harness(tmp_path)
    # The digest port only speaks for the registered runtime project: an
    # approval naming any other target fails verification closed before a
    # decision or confirmation can exist (policy would deny it too — the
    # allowed_scopes list only PROJECT_ID).
    with pytest.raises(Exception) as excinfo:
        _approve(h, target_id=OTHER_PROJECT_ID)
    assert "does not match the authoritative plan" in str(excinfo.value)
    assert len(h.service._confirmations.store) == 0
    assert h.plans.read(PROJECT_ID).revision == 1
    assert len(h.runtime_calls) == 0
    _engine_world_unchanged(h)


def test_approval_refuses_wrong_plan_digest(tmp_path):
    h = _full_harness(tmp_path)
    with pytest.raises(Exception) as excinfo:
        _approve(h, digest="b" * 64)
    assert "does not match the authoritative plan" in str(excinfo.value)
    # Fail-closed: no action, no confirmation, no mutation.
    assert len(h.service._confirmations.store) == 0
    _engine_world_unchanged(h)


# ---------------------------------------------------------------------------
# Rejection 3: stale control-plane plan (gate STALE_PLAN)
# ---------------------------------------------------------------------------


def test_gate_refuses_stale_plan_after_drift(tmp_path):
    h = _prepared(tmp_path)
    action_id = h.approval["action_id"]
    h.plans.update(PROJECT_ID, expected_revision=1, fields={"title": "Drifted title"}, now=NOW)
    assert h.plans.read(PROJECT_ID).revision == 2

    with pytest.raises(Exception) as excinfo:
        h.service.run_approved_update(h.approval["session_id"], action_id=action_id)
    assert "stale_plan" in str(excinfo.value)

    _engine_world_unchanged(h, expected_revision=2)
    confirmation = h.service._confirmations.store.get(h.approval["confirmation_id"])
    assert confirmation.state.value == "confirmed"


# ---------------------------------------------------------------------------
# Rejection 4-5: expired / already-consumed confirmation
# ---------------------------------------------------------------------------


def test_gate_refuses_expired_confirmation(tmp_path):
    h = _prepared(tmp_path)
    action_id = h.approval["action_id"]
    confirmation_id = h.approval["confirmation_id"]
    # Shorten the confirmation's expiry in the disposable test store so the
    # gate's confirmation check (not the action/decision expiry, which the
    # service checks earlier) is what refuses the execution.
    binding = h.service._confirmations.store.get(confirmation_id)
    expired = dataclasses.replace(binding, expires_at=NOW + timedelta(seconds=1))
    h.service._confirmations._store.put(expired)
    h.clock.value = NOW + timedelta(minutes=2)
    fresh_session = h.service.login(SECRET)

    with pytest.raises(Exception) as excinfo:
        h.service.run_approved_update(fresh_session.session_id, action_id=action_id)
    assert "confirmation_expired" in str(excinfo.value)

    _engine_world_unchanged(h)


def test_gate_refuses_already_consumed_confirmation(tmp_path):
    h = _prepared(tmp_path)
    confirmation_id = h.approval["confirmation_id"]
    binding = h.service._confirmations.store.get(confirmation_id)
    h.service._confirmations.consume(binding, now=NOW)

    with pytest.raises(Exception) as excinfo:
        h.service.run_approved_update(h.approval["session_id"], action_id=h.approval["action_id"])
    assert "confirmation_consumed" in str(excinfo.value)

    _engine_world_unchanged(h)


# ---------------------------------------------------------------------------
# Rejection 6-7: expired lease / wrong fencing token (+ tamper containment)
# ---------------------------------------------------------------------------


def test_execute_refuses_expired_lease(tmp_path):
    h = _prepared(tmp_path)
    action_id = h.approval["action_id"]
    action = h.service._actions.get_action(action_id)
    h.service._actions.acquire_lease(action_id, expected_version=action.version, now=NOW)
    # Shorten the durable lease's expiry in the disposable test store. The
    # service's own active-lease resolution (a fail-closed precondition
    # ahead of the gate's lease check) refuses with "no active lease".
    with h.service._actions._db.connection as conn:
        conn.execute(
            "UPDATE execution_leases SET expires_at = ? WHERE action_id = ? AND state = 'granted'",
            ((NOW + timedelta(seconds=1)).isoformat(), action_id),
        )
    h.clock.value = NOW + timedelta(minutes=2)
    fresh_session = h.service.login(SECRET)

    with pytest.raises(Exception) as excinfo:
        h.service.run_approved_update(fresh_session.session_id, action_id=action_id)
    assert "no active lease" in str(excinfo.value)

    _engine_world_unchanged(h)


def test_gate_refuses_wrong_fencing_token_and_contains_tamper(tmp_path):
    h = _prepared(tmp_path)
    action_id = h.approval["action_id"]
    action = h.service._actions.get_action(action_id)
    h.service._actions.acquire_lease(action_id, expected_version=action.version, now=NOW)
    contract = _canonical_contract(h, action_id)
    executor = h.service._executor()

    with pytest.raises(Exception) as excinfo:
        executor.execute(dataclasses.replace(contract, fencing_token=contract.fencing_token + 999), now=NOW)
    assert "lease_fence_mismatch" in str(excinfo.value)

    # The mutation never happened and the confirmation was never consumed.
    _engine_world_unchanged(h)
    confirmation = h.service._confirmations.store.get(h.approval["confirmation_id"])
    assert confirmation.state.value == "confirmed"

    # Tamper containment: the refused attempt durably bound ITS digest, so
    # the action refuses every other contract; the canonical one can no
    # longer run either. Fail-closed in every direction.
    with pytest.raises(Exception) as excinfo:
        executor.execute(contract, now=NOW)
    assert "already bound to a different value" in str(excinfo.value)
    _engine_world_unchanged(h)


# ---------------------------------------------------------------------------
# Rejection 8: engaged kill switch
# ---------------------------------------------------------------------------


def test_executor_refuses_engaged_kill_switch(tmp_path):
    h = _prepared(tmp_path)
    h.kill_switches.engage(Environment.STAGING, reason="incident", now=NOW)

    with pytest.raises(Exception) as excinfo:
        h.service.run_approved_update(h.approval["session_id"], action_id=h.approval["action_id"])
    assert "kill_switch_engaged" in str(excinfo.value)

    _engine_world_unchanged(h)
    confirmation = h.service._confirmations.store.get(h.approval["confirmation_id"])
    assert confirmation.state.value == "confirmed"


# ---------------------------------------------------------------------------
# Rejection 9-10: IPC protocol boundary (real server + real client)
# ---------------------------------------------------------------------------


def _ipc_server(tmp_path, handler, *, allowed_caller_uids=None):
    server = ExecutorIPCServer(
        socket_path=str(tmp_path / "c62-executor.sock"),
        handler=handler,
        allowed_caller_uids=allowed_caller_uids,
    )
    server.start()
    return server


def _ipc_request(**overrides):
    values = {
        "action_id": "a" * 64,
        "capability_id": "update_project_plan",
        "target_id": PROJECT_ID,
        "contract_digest": "d" * 64,
        "lease_id": "l" * 32,
        "fencing_token": 1,
    }
    values.update(overrides)
    return ExecutionRequest(**values)


class _RecordingHandler:
    def __init__(self, outcome="succeeded", provider_code="restart_ok"):
        self.outcome = outcome
        self.provider_code = provider_code
        self.received: list[ExecutionRequest] = []

    def __call__(self, request: ExecutionRequest):
        self.received.append(request)
        from aipm.control_plane.executor_ipc import ExecutionResponse

        return ExecutionResponse(
            outcome=self.outcome,
            provider_code=self.provider_code,
            action_id=request.action_id,
            evidence_reference=f"test:{request.action_id[:8]}",
        )


def test_ipc_refuses_malformed_request_frame(tmp_path):
    handler = _RecordingHandler()
    server = _ipc_server(tmp_path, handler)
    try:
        threading.Thread(target=server.serve_one, daemon=True).start()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(tmp_path / "c62-executor.sock"))
        sock.sendall(encode_frame(b"not-json"))
        response = json.loads(decode_frame(sock))
        sock.close()
        assert response["outcome"] == "refused"
        assert len(handler.received) == 0  # the handler never saw the request
    finally:
        server.stop()


def test_ipc_refuses_unauthorized_caller(tmp_path):
    handler = _RecordingHandler()
    server = _ipc_server(tmp_path, handler, allowed_caller_uids={99999})
    try:
        threading.Thread(target=server.serve_one, daemon=True).start()
        client = ExecutorIPCClient(socket_path=str(tmp_path / "c62-executor.sock"))
        response = client.send(_ipc_request())
        assert response.outcome == "refused"
        assert response.provider_code == "unauthorized_caller"
        assert len(handler.received) == 0
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Rejection 11: tampered execution contract (engine-side, pre-mutation)
# ---------------------------------------------------------------------------


def test_engine_refuses_tampered_contract_digest_before_mutation(tmp_path, monkeypatch):
    h = _prepared(tmp_path)
    monkeypatch.setenv("AIPM_INTEGRATION_MARKER", str(h.marker))
    action_id = h.approval["action_id"]
    action = h.service._actions.get_action(action_id)
    h.service._actions.acquire_lease(action_id, expected_version=action.version, now=NOW)
    contract = _canonical_contract(h, action_id)
    executor = h.service._executor()

    tampered = dataclasses.replace(contract, expected_plan_digest="e" * 64)
    with pytest.raises(Exception) as excinfo:
        executor.execute(tampered, now=NOW)
    assert "stale_plan" in str(excinfo.value)
    _engine_world_unchanged(h)

    # Tamper containment: the tampered digest is now durably bound, so the
    # correct contract is refused too. The mutation can never happen.
    with pytest.raises(Exception) as excinfo:
        executor.execute(contract, now=NOW)
    assert "already bound to a different value" in str(excinfo.value)
    _engine_world_unchanged(h)
    confirmation = h.service._confirmations.store.get(h.approval["confirmation_id"])
    assert confirmation.state.value == "confirmed"


# ---------------------------------------------------------------------------
# Rejection 12: client-supplied foreign digest; execution seam is action-only
# ---------------------------------------------------------------------------


def test_foreign_digest_refused_at_approval_and_execution_takes_no_digest(tmp_path):
    h = _full_harness(tmp_path)
    # A digest that is valid 64-hex but not the authoritative plan digest.
    with pytest.raises(Exception) as excinfo:
        _approve(h, digest="c" * 64)
    assert "does not match the authoritative plan" in str(excinfo.value)
    assert len(h.service._confirmations.store) == 0
    _engine_world_unchanged(h)

    # The canonical execution seam accepts ONLY an action reference: a
    # client cannot inject a digest, contract, or plan at execution time.
    parameters = inspect.signature(h.service.run_approved_update).parameters
    assert set(parameters) == {"session_id", "action_id", "now"}


# ---------------------------------------------------------------------------
# Rejection 13: engine-side world change after approval (TOCTOU)
# ---------------------------------------------------------------------------


def test_engine_refuses_when_world_changed_after_approval(tmp_path, monkeypatch):
    h = _prepared(tmp_path)
    monkeypatch.setenv("AIPM_INTEGRATION_MARKER", str(h.marker))
    head_before = rev_parse_head(h.project)

    # The world changes after approval and snapshot capture but before
    # execution: the remote diverges and the work repo fetches it.
    make_remote_commit(h.project, "docs.md", "from remote\n")
    fetch_origin(h.project)

    with pytest.raises(Exception) as excinfo:
        h.service.run_approved_update(h.approval["session_id"], action_id=h.approval["action_id"])
    # The engine re-plans at execution time and refuses pre-mutation: the
    # contract was issued for the plan the operator actually approved.
    assert "different plan" in str(excinfo.value)

    # No repository mutation, no runtime marker; the runtime was attempted
    # exactly once and never retried.
    assert rev_parse_head(h.project) == head_before
    assert not (Path(h.project.path) / "docs.md").exists()
    assert not h.marker.exists()
    assert len(h.runtime_calls) == 1

    # The control-plane mutation is durable and terminal on its own plane.
    assert h.plans.read(PROJECT_ID).revision == 2
    confirmation = h.service._confirmations.store.get(h.approval["confirmation_id"])
    assert confirmation.state.value == "consumed"


# ---------------------------------------------------------------------------
# Rejection 14-16: replay, IPC timeout, unknown outcome
# ---------------------------------------------------------------------------


def test_replay_never_reinvokes_runtime(tmp_path, monkeypatch):
    h = _full_harness(tmp_path)
    monkeypatch.setenv("AIPM_INTEGRATION_MARKER", str(h.marker))
    approval = _approve(h)
    first = h.service.run_approved_update(approval["session_id"], action_id=approval["action_id"])
    assert first["executed"] is True
    assert len(h.runtime_calls) == 1
    assert h.plans.read(PROJECT_ID).revision == 2

    second = h.service.run_approved_update(approval["session_id"], action_id=approval["action_id"])
    assert second["executed"] is False
    assert second["lifecycle_state"] == "verified_success"
    assert len(h.runtime_calls) == 1  # the runtime never re-ran
    assert h.plans.read(PROJECT_ID).revision == 2  # exactly one revision advance


def test_ipc_timeout_yields_unknown_outcome_without_retry(tmp_path):
    handler = _RecordingHandler()
    release = threading.Event()

    def blocking_handler(request):
        handler.received.append(request)
        release.wait(timeout=5)
        from aipm.control_plane.executor_ipc import ExecutionResponse

        return ExecutionResponse(
            outcome="succeeded",
            provider_code="restart_ok",
            action_id=request.action_id,
            evidence_reference="test:blocked",
        )

    server = _ipc_server(tmp_path, blocking_handler)
    try:
        # The client's timeout closes the socket; the server's later response
        # write may hit a broken pipe, which is expected here and contained.
        def _serve_one_contained():
            try:
                server.serve_one()
            except (BrokenPipeError, ConnectionResetError):
                pass

        threading.Thread(target=_serve_one_contained, daemon=True).start()
        client = ExecutorIPCClient(socket_path=str(tmp_path / "c62-executor.sock"))
        response = client.send(_ipc_request(), timeout=0.3)
        # The client classifies the loss as an unknown outcome — never a
        # success, never a failure that could justify a retry.
        assert response.outcome == "unknown_outcome"
        assert response.provider_code == "timeout"

        release.set()
        assert len(handler.received) == 1  # exactly one request was ever sent
    finally:
        server.stop()


def test_unknown_outcome_is_never_retried(tmp_path):
    """C6.0 semantics: an UNKNOWN_OUTCOME receipt can never be reset or
    re-executed; reconciliation — never blind retry — is the only path."""

    from aipm.control_plane.mutation_receipt import MutationReceiptStore, MutationStatus

    store = MutationReceiptStore(str(tmp_path / "receipts.db"))
    store.claim(
        action_id="a" * 64,
        fencing_token=1,
        capability_id="update_project_plan",
        target_id=PROJECT_ID,
        contract_digest="d" * 64,
    )
    store.complete(action_id="a" * 64, fencing_token=1, status=MutationStatus.UNKNOWN_OUTCOME, provider_code="timeout")

    with pytest.raises(Exception):
        store.complete(action_id="a" * 64, fencing_token=1, status=MutationStatus.RECEIPT_CREATED, provider_code="retry")
    loaded = store.get(action_id="a" * 64, fencing_token=1)
    assert loaded.mutation_status is MutationStatus.UNKNOWN_OUTCOME
    assert store.count() == 1


# ---------------------------------------------------------------------------
# Boundary: the adapter is pure composition
# ---------------------------------------------------------------------------


def test_runtime_adapter_source_is_pure_composition():
    from aipm.services.update import runtime_adapter as module

    source = inspect.getsource(module)
    for forbidden in (
        "subprocess", "os.system", "Popen", "socket", "requests.", "urllib",
        "httpx", "sqlite3", "password", "secret", "token", "Route", "FastAPI",
    ):
        assert forbidden not in source, forbidden
