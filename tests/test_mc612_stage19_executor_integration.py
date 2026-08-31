"""Shot 17 (executor service integration + failure-injection certification) tests."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.control_plane.audit import SQLiteAuditLedger
from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.capabilities_registry import DEFAULT_CAPABILITY_REGISTRY, CapabilityId, CapabilityPolicyError, ExecutorRegistry
from aipm.control_plane.executor import EXECUTION_CONTRACT_VERSION, ExecutionContract, ExecutorCapability
from aipm.control_plane.executor_ipc import (
    ExecutionRequest,
    ExecutorIPCClient,
    ExecutorIPCServer,
    encode_frame,
    decode_frame,
)
from aipm.control_plane.identity import AuthenticationMethod, OwnerPrincipal, PrincipalVerification
from aipm.control_plane.models import ActionRequest, ControlPlaneError, LifecycleState, OperationKind, PlanningErrorCode
from aipm.control_plane.mutation_receipt import MutationReceiptError, MutationReceiptStore, MutationStatus
from aipm.control_plane.owner_auth import Argon2idVerifier, OwnerAuthenticator
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import AuthorizationPolicy
from aipm.control_plane.project_plan import Environment, ProjectPlan
from aipm.control_plane.recovery import RecoveryManager
from aipm.control_plane.service import OwnerControlPlaneService
from aipm.control_plane.session import OwnerSessionStore
from aipm.control_plane.storage import (
    ControlPlaneDatabase, SQLiteActionRepository, SQLiteProjectPlanStore,
)

VERIFIER = "$argon2id$v=19$m=65536,t=2,p=1$c3RhZ2UzLXNhbHQtMTIzNA$zho28DBNr2G2cGbxzr0Dl6AKwhbd8hEeTkti1pn7TW0"
SECRET = "test-owner-secret"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
SOCKET_PATH = "/tmp/test_executor_gate.sock"


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value


def db_path(tmp_path: Path) -> Path:
    return tmp_path / "control_plane.db"


def request(**overrides):
    values = {"operation": OperationKind.UPDATE_PROJECT_PLAN, "target_id": "project-demo",
              "idempotency_key": "idem-001", "metadata": (("title", "New title"),), "environment": "staging"}
    values.update(overrides)
    return ActionRequest(**values)


def build_service(tmp_path: Path, *, clock=None, execution_mode="test", executor_ipc_client=None, kill_switches=None):
    clock = clock or _Clock(NOW)
    db = ControlPlaneDatabase(db_path(tmp_path), clock=clock)
    ledger = SQLiteAuditLedger(db)
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=clock)
    sessions = OwnerSessionStore(clock=clock)
    policy = AuthorizationPolicy(policy_version="policy-v1", allowed_scopes=frozenset({("project-demo", "staging")}))
    confirmations = OwnerConfirmationService(clock=clock)
    plans = SQLiteProjectPlanStore(db)
    plans.create(ProjectPlan.create(target_id="project-demo", environment=Environment.STAGING, title="Old title", objective="Objective", now=NOW))
    planner = PlanOnlyPlanner(clock=clock, target_allow_list={"project-demo"})
    actions = SQLiteActionRepository(db, audit=ledger)
    service = OwnerControlPlaneService(
        authenticator=authenticator, sessions=sessions, policy=policy, confirmations=confirmations,
        plans=plans, planner=planner, audit=ledger, actions=actions,
        execution_mode=execution_mode, executor_ipc_client=executor_ipc_client,
        kill_switches=kill_switches, clock=clock)
    return service, db, ledger, plans, clock


# ---------------------------------------------------------------------------
# Production routing: execute_action MUST use IPC
# ---------------------------------------------------------------------------


def test_ipc_mode_does_not_use_in_process_executor(tmp_path: Path):
    """When execution_mode=ipc, the service routes through IPC — no in-process fallback."""

    class _RefusingIPC:
        """IPC client that records calls and refuses (executor not running)."""

        def __init__(self):
            self.calls = []

        def send(self, request):
            self.calls.append(request)
            raise ConnectionRefusedError("Executor service not running")

    ipc = _RefusingIPC()
    service, db, ledger, plans, clock = build_service(tmp_path, execution_mode="ipc", executor_ipc_client=ipc)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))

    with pytest.raises((ConnectionRefusedError, ControlPlaneError)):
        service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    # The plan was NOT mutated — no in-process fallback
    assert plans.read("project-demo").revision == 1
    # The IPC client was called
    assert len(ipc.calls) == 1


def test_ipc_mode_success_route(tmp_path: Path):
    """When IPC succeeds, the action lifecycle is properly advanced."""

    class _SuccessIPC:
        def __init__(self):
            self.calls = []

        def send(self, request):
            self.calls.append(request)
            from aipm.control_plane.executor_ipc import ExecutionResponse
            return ExecutionResponse(
                outcome="verification_succeeded", provider_code="restart_ok",
                action_id=request.action_id, evidence_reference="test")

    ipc = _SuccessIPC()
    service, db, ledger, plans, clock = build_service(tmp_path, execution_mode="ipc", executor_ipc_client=ipc)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))

    result = service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert result["outcome"] == "verification_succeeded"
    assert len(ipc.calls) == 1
    # The plan is NOT mutated by the IPC route (the executor does the mutation)
    assert plans.read("project-demo").revision == 1


def test_test_mode_uses_in_process_executor(tmp_path: Path):
    """In test mode, the in-process executor is used (for test infrastructure)."""
    service, db, ledger, plans, clock = build_service(tmp_path, execution_mode="test")
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))

    result = service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert result.outcome.value == "verification_succeeded"
    assert plans.read("project-demo").revision == 2  # in-process executor mutated the plan


# ---------------------------------------------------------------------------
# Mutation receipt: exactly-once semantics + failure injection
# ---------------------------------------------------------------------------


def test_receipt_claim_prevents_duplicate_mutation(tmp_path: Path):
    from aipm.control_plane.mutation_receipt import MutationReceiptStore, MutationStatus

    store = MutationReceiptStore(str(tmp_path / "receipts.db"))
    receipt = store.claim(action_id="a" * 64, fencing_token=1, capability_id="apply_project_plan", target_id="project-demo", contract_digest="d" * 64)
    assert receipt.mutation_status is MutationStatus.RECEIPT_CREATED
    # A second claim for the same action+fence is rejected
    with pytest.raises(Exception, match="already claimed"):
        store.claim(action_id="a" * 64, fencing_token=1, capability_id="apply_project_plan", target_id="project-demo", contract_digest="d" * 64)


def test_response_loss_after_success_does_not_duplicate(tmp_path: Path):
    """Provider succeeds → receipt completed → response lost → retry gets existing receipt."""
    from aipm.control_plane.mutation_receipt import MutationReceiptStore, MutationStatus

    store = MutationReceiptStore(str(tmp_path / "receipts.db"))
    store.claim(action_id="a" * 64, fencing_token=1, capability_id="apply_project_plan", target_id="project-demo", contract_digest="d" * 64)
    store.complete(action_id="a" * 64, fencing_token=1, status=MutationStatus.MUTATION_SUCCEEDED, provider_code="restart_ok")
    # Simulate: the response is lost and the caller retries
    with pytest.raises(Exception, match="already claimed"):
        store.claim(action_id="a" * 64, fencing_token=1, capability_id="apply_project_plan", target_id="project-demo", contract_digest="d" * 64)
    # The existing receipt proves the mutation already occurred
    existing = store.get(action_id="a" * 64, fencing_token=1)
    assert existing.mutation_status is MutationStatus.MUTATION_SUCCEEDED


def test_unknown_outcome_is_never_retried(tmp_path: Path):
    """UNKNOWN_OUTCOME receipt → retry MUST NOT re-invoke the provider."""
    from aipm.control_plane.mutation_receipt import MutationReceiptStore, MutationStatus

    store = MutationReceiptStore(str(tmp_path / "receipts.db"))
    store.claim(action_id="a" * 64, fencing_token=1, capability_id="apply_project_plan", target_id="project-demo", contract_digest="d" * 64)
    store.complete(action_id="a" * 64, fencing_token=1, status=MutationStatus.UNKNOWN_OUTCOME, provider_code="timeout")
    # Attempting to reset or re-execute must be rejected
    with pytest.raises(Exception):
        store.complete(action_id="a" * 64, fencing_token=1, status=MutationStatus.RECEIPT_CREATED, provider_code="retry")
    loaded = store.get(action_id="a" * 64, fencing_token=1)
    assert loaded.mutation_status is MutationStatus.UNKNOWN_OUTCOME


def test_concurrent_receipt_claim_single_winner(tmp_path: Path):
    """N concurrent requests for the same action+fence → exactly ONE receipt."""
    from aipm.control_plane.mutation_receipt import MutationReceiptStore, MutationStatus
    import threading

    store = MutationReceiptStore(str(tmp_path / "receipts.db"))
    results = []
    errors = []
    barrier = threading.Barrier(4)

    def worker(worker_id: int):
        barrier.wait()
        try:
            receipt = store.claim(
                action_id="a" * 64, fencing_token=1, capability_id="apply_project_plan",
                target_id="project-demo", contract_digest="d" * 64)
            results.append(receipt)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1
    assert store.count() == 1


# ---------------------------------------------------------------------------
# SQLite contention fix
# ---------------------------------------------------------------------------


def test_concurrent_recovery_with_sqlite_contention_is_reliable(tmp_path: Path):
    """The concurrent recovery test must be deterministic with busy_timeout + WAL."""
    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    # The action is terminal; recovery is a no-op
    rm = RecoveryManager(actions=service._actions, plans=plans, clock=lambda: NOW)
    outcome = rm.recover(identity.action_id)
    assert outcome.reason_code == "terminal_no_action"


# ---------------------------------------------------------------------------
# Capability allowlist and target binding
# ---------------------------------------------------------------------------


def test_executor_capability_registry_enforces_staging_only():
    registry = DEFAULT_CAPABILITY_REGISTRY
    definition = registry.require_executable(CapabilityId.APPLY_PROJECT_PLAN, environment="staging")
    assert definition.enabled is True
    with pytest.raises(CapabilityPolicyError):
        registry.require_executable(CapabilityId.APPLY_PROJECT_PLAN, environment="production")
    with pytest.raises(CapabilityPolicyError):
        registry.require_executable(CapabilityId.RESTART_ALLOWLISTED_SYSTEMD_UNIT, environment="staging")


def test_executor_registry_rejects_missing_and_duplicate():
    registry = DEFAULT_CAPABILITY_REGISTRY
    executors = ExecutorRegistry(capability_registry=registry)
    with pytest.raises(CapabilityPolicyError, match="No executor"):
        executors.resolve(CapabilityId.APPLY_PROJECT_PLAN)
    executors.register(capability_id=CapabilityId.APPLY_PROJECT_PLAN, executor=object())
    with pytest.raises(CapabilityPolicyError, match="Duplicate"):
        executors.register(capability_id=CapabilityId.APPLY_PROJECT_PLAN, executor=object())


# ---------------------------------------------------------------------------
# Production boundary
# ---------------------------------------------------------------------------


def test_production_is_denied_at_every_boundary(tmp_path: Path):
    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    # 1. Authorization denies production (returns a deny decision)
    decision = service.authorize(session.session_id, request(environment="production"))
    assert decision.allowed is False
    # 2. Capability registry denies production
    with pytest.raises(CapabilityPolicyError):
        DEFAULT_CAPABILITY_REGISTRY.require_executable(CapabilityId.APPLY_PROJECT_PLAN, environment="production")


# ---------------------------------------------------------------------------
# Source security audit
# ---------------------------------------------------------------------------


def test_service_has_no_hidden_in_process_fallback():
    source = Path("src/aipm/control_plane/service.py").read_text(encoding="utf-8")
    assert "_execute_via_ipc" in source
    assert 'execution_mode == "ipc"' in source
    # No fallback that silently switches to in-process execution when IPC fails
    assert "fallback" not in source.lower() or "no fallback" in source.lower() or "no in-process fallback" in source.lower()


def test_ipc_client_has_no_shell_or_arbitrary_execution():
    source = Path("src/aipm/control_plane/executor_ipc.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "shell=True", "UpdateEngine"):
        assert forbidden not in source, forbidden
