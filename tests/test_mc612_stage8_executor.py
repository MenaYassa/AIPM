"""Shot 8 (first bounded execution vertical slice) tests.

Covers: the execution contract, the tiny executor, lease/fencing, atomic plan
mutation with lifecycle+evidence, confirmation consumption, independent
verification, UNKNOWN_OUTCOME reconciliation, the rollback vertical slice,
crash recovery, concurrency, kill-switch enforcement, and closed-capability
security.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.control_plane.audit import SQLiteAuditLedger
from aipm.control_plane.audit.models import AuditEventType
from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.executor import (
    EXECUTION_CONTRACT_VERSION,
    ExecutionContract,
    Executor,
    ExecutorCapability,
)
from aipm.control_plane.kill_switch import KillSwitchRegistry
from aipm.control_plane.models import (
    ActionRequest,
    ConfirmationState,
    ControlPlaneError,
    LifecycleState,
    OperationKind,
    PlanningErrorCode,
)
from aipm.control_plane.owner_auth import Argon2idVerifier, OwnerAuthenticator
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import AuthorizationPolicy
from aipm.control_plane.project_plan import Environment, ProjectPlan
from aipm.control_plane.service import OwnerControlPlaneService
from aipm.control_plane.session import OwnerSessionStore
from aipm.control_plane.storage import (
    ControlPlaneDatabase,
    SQLiteActionRepository,
    SQLiteKillSwitchStore,
    SQLitePlanSnapshotRepository,
    SQLiteProjectPlanStore,
    SQLiteVerificationRepository,
)

VERIFIER = "$argon2id$v=19$m=65536,t=2,p=1$c3RhZ2UzLXNhbHQtMTIzNA$zho28DBNr2G2cGbxzr0Dl6AKwhbd8hEeTkti1pn7TW0"
SECRET = "test-owner-secret"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value


def db_path(tmp_path: Path) -> Path:
    return tmp_path / "control_plane.db"


def make_plan(**overrides):
    values = {"target_id": "project-demo", "title": "Old title", "objective": "Objective", "now": NOW}
    values.update(overrides)
    return ProjectPlan.create(target_id=values["target_id"], environment=Environment.STAGING, title=values["title"], objective=values["objective"], now=values["now"])


def request(**overrides):
    values = {
        "operation": OperationKind.UPDATE_PROJECT_PLAN,
        "target_id": "project-demo",
        "idempotency_key": "idem-001",
        "metadata": (("title", "New title"),),
        "environment": "staging",
    }
    values.update(overrides)
    return ActionRequest(**values)


def build_service(tmp_path: Path, *, clock=None, kill_switches=None):
    clock = clock or _Clock(NOW)
    db = ControlPlaneDatabase(db_path(tmp_path), clock=clock)
    targets = {"project-demo"}
    ledger = SQLiteAuditLedger(db)
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=clock)
    sessions = OwnerSessionStore(clock=clock)
    policy = AuthorizationPolicy(policy_version="policy-v1", allowed_scopes=frozenset({("project-demo", "staging")}))
    confirmations = OwnerConfirmationService(clock=clock)
    plans = SQLiteProjectPlanStore(db)
    try:
        plans.create(make_plan())
    except Exception:
        pass
    planner = PlanOnlyPlanner(clock=clock, target_allow_list=targets)
    actions = SQLiteActionRepository(db, audit=ledger)
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
        execution_mode='test',
    )
    return service, db, ledger, plans, clock


def prepared_action(tmp_path: Path, *, clock=None, metadata=(("title", "New title"),), kill_switches=None):
    """Drive one action all the way to SNAPSHOT_CAPTURED, ready for execution."""
    service, db, ledger, plans, clock = build_service(tmp_path, clock=clock, kill_switches=kill_switches)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request(metadata=metadata))
    identity = decision.action_identity
    assert identity is not None
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    snapshot = service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    return service, db, ledger, plans, clock, session, decision, identity, snapshot


# ---------------------------------------------------------------------------
# End-to-end success
# ---------------------------------------------------------------------------


def test_end_to_end_execution_success(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, snapshot = prepared_action(tmp_path)
    result = service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert result.outcome.value == "verification_succeeded"
    assert result.lifecycle_state is LifecycleState.VERIFIED_SUCCESS
    assert result.mutated_revision == 2
    assert result.verification_success is True

    # The durable plan actually changed exactly as authorized.
    plan = plans.read("project-demo")
    assert plan.revision == 2
    assert plan.title == "New title"
    assert plan.objective == "Objective"

    # Lifecycle, outcome, and confirmation consumption are durable.
    action = service.lifecycle(identity.action_id)
    assert action.state is LifecycleState.VERIFIED_SUCCESS
    row = db.connection.execute("SELECT outcome FROM actions WHERE action_id = ?", (identity.action_id,)).fetchone()
    assert row["outcome"] == "verification_succeeded"
    confirmation_row = db.connection.execute(
        "SELECT state FROM confirmations WHERE confirmation_id = ?",
        (result.verification_id and _confirmation_id(db, identity.action_id),),
    ).fetchone()
    assert confirmation_row["state"] == "consumed"

    # Lease evidence exists and the full chain verifies.
    types = [event.event_type for event in ledger.events()]
    assert AuditEventType.LEASE_ACQUIRED in types
    assert AuditEventType.EXECUTION_STARTED in types
    assert AuditEventType.EXECUTION_SUCCEEDED in types
    assert AuditEventType.VERIFICATION_STARTED in types
    assert AuditEventType.VERIFICATION_SUCCEEDED in types
    assert ledger.verify_chain().ok is True
    db.close()


def _confirmation_id(db, action_id: str) -> str:
    row = db.connection.execute(
        "SELECT c.confirmation_id FROM confirmations c JOIN actions a ON a.decision_id = c.decision_id WHERE a.action_id = ?",
        (action_id,),
    ).fetchone()
    return row["confirmation_id"]


def test_replay_after_success_never_mutates_twice(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, snapshot = prepared_action(tmp_path)
    first = service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert first.lifecycle_state is LifecycleState.VERIFIED_SUCCESS
    replay = service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    assert replay.lifecycle_state is LifecycleState.VERIFIED_SUCCESS
    assert plans.read("project-demo").revision == 2
    execution_events = [event for event in ledger.events() if event.event_type is AuditEventType.EXECUTION_SUCCEEDED]
    assert len(execution_events) == 1
    db.close()


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------


def test_contract_rejects_wrong_version_operation_and_fields():
    base = dict(
        contract_version=EXECUTION_CONTRACT_VERSION,
        action_id="a" * 64,
        action_version=1,
        operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
        target_id="project-demo",
        environment="staging",
        plan_id="p" * 32,
        expected_plan_revision=1,
        expected_plan_digest="a" * 64,
        mutation_fields=(("title", "X"),),
        snapshot_id="s" * 32,
        decision_id="d" * 32,
        confirmation_id="c" * 32,
        policy_version="policy-v1",
        verification_version="mc612-verification-v1",
        kill_switch_epoch=1,
        lease_id="l" * 32,
        fencing_token=1,
        expires_at=NOW + timedelta(minutes=5),
    )
    assert ExecutionContract(**base) is not None
    with pytest.raises(Exception, match="version"):
        ExecutionContract(**{**base, "contract_version": "mc612-execution-contract-v0"})
    with pytest.raises(Exception):
        ExecutionContract(**{**base, "operation": "run_shell"})
    with pytest.raises(Exception, match="allow-list"):
        ExecutionContract(**{**base, "mutation_fields": (("command", "rm -rf"),)})
    with pytest.raises(Exception, match="fencing"):
        ExecutionContract(**{**base, "fencing_token": 0})
    with pytest.raises(Exception, match="digest"):
        ExecutionContract(**{**base, "expected_plan_digest": "nope"})
    with pytest.raises(Exception, match="revision"):
        ExecutionContract(**{**base, "expected_plan_revision": 0})


def test_executor_refuses_stale_plan_before_mutation(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, snapshot = prepared_action(tmp_path)
    # The world moved after authorization: the target plan drifted.
    plans.update("project-demo", expected_revision=1, fields={"title": "Concurrent edit"}, now=NOW)
    from aipm.control_plane.executor import ExecutionRefused

    with pytest.raises((ExecutionRefused, ControlPlaneError), match="stale|no longer matches"):
        service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    # Nothing mutated; the lease was granted but the action cannot proceed.
    assert plans.read("project-demo").title == "Concurrent edit"
    assert service.lifecycle(identity.action_id).state is LifecycleState.LEASED
    db.close()


# ---------------------------------------------------------------------------
# Kill switch at the mutation boundary
# ---------------------------------------------------------------------------


def test_kill_switch_recheck_blocks_execution(tmp_path: Path):
    class _Switches:
        def __init__(self) -> None:
            self.epoch = 1
            self.permit = True

        def switch(self, environment):
            from aipm.control_plane.kill_switch import KillSwitch, KillSwitchState

            state = KillSwitchState.DISENGAGED if self.permit else KillSwitchState.ENGAGED
            return KillSwitch(environment=Environment(environment), state=state, created_at=NOW, updated_at=NOW, epoch=self.epoch)

        def permits(self, environment) -> bool:
            return self.permit

    switches = _Switches()
    service, db, ledger, plans, clock, session, decision, identity, snapshot = prepared_action(tmp_path, kill_switches=switches)
    # Engage after the lease was granted: the executor's boundary re-check refuses.
    switches.permit = False
    from aipm.control_plane.executor import ExecutionRefused

    with pytest.raises(ExecutionRefused, match="Kill switch"):
        service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert plans.read("project-demo").revision == 1
    db.close()


def test_kill_switch_epoch_mismatch_refuses_execution(tmp_path: Path):
    class _Switches:
        def __init__(self) -> None:
            self.epoch = 1

        def switch(self, environment):
            from aipm.control_plane.kill_switch import KillSwitch, KillSwitchState

            return KillSwitch(environment=Environment(environment), state=KillSwitchState.DISENGAGED, created_at=NOW, updated_at=NOW, epoch=self.epoch)

        def permits(self, environment) -> bool:
            return True

    switches = _Switches()
    service, db, ledger, plans, clock, session, decision, identity, snapshot = prepared_action(tmp_path, kill_switches=switches)
    # Build the contract while the switch is at epoch 1, then let it cycle.
    from aipm.control_plane.executor import ExecutionRefused

    executor = service._executor()
    contract = ExecutionContract(
        contract_version=EXECUTION_CONTRACT_VERSION,
        action_id=identity.action_id,
        action_version=service.lifecycle(identity.action_id).version,
        operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
        target_id="project-demo",
        environment="staging",
        plan_id=identity.plan_id,
        expected_plan_revision=1,
        expected_plan_digest=identity.target_digest,
        mutation_fields=(("title", "New title"),),
        snapshot_id=snapshot.snapshot_id,
        decision_id=decision.decision_id,
        confirmation_id=_confirmation_id(db, identity.action_id),
        policy_version="policy-v1",
        verification_version="mc612-verification-v1",
        kill_switch_epoch=1,
        lease_id="l" * 32,
        fencing_token=1,
        expires_at=NOW + timedelta(minutes=30),
    )
    switches.epoch = 2  # the switch cycled after the contract was issued
    with pytest.raises(ExecutionRefused, match="Kill switch changed"):
        executor.execute(contract, now=NOW + timedelta(minutes=3))
    db.close()


# ---------------------------------------------------------------------------
# Confirmation consumption
# ---------------------------------------------------------------------------


def test_confirmation_is_consumed_exactly_once(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, snapshot = prepared_action(tmp_path)
    service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    confirmation_id = _confirmation_id(db, identity.action_id)
    row = db.connection.execute("SELECT state FROM confirmations WHERE confirmation_id = ?", (confirmation_id,)).fetchone()
    assert row["state"] == "consumed"
    # A second execution attempt cannot re-consume: the action is terminal anyway.
    service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    row = db.connection.execute("SELECT state FROM confirmations WHERE confirmation_id = ?", (confirmation_id,)).fetchone()
    assert row["state"] == "consumed"
    db.close()


# ---------------------------------------------------------------------------
# Lease / fencing
# ---------------------------------------------------------------------------


def test_lease_is_unique_and_fencing_token_is_monotonic(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, snapshot = prepared_action(tmp_path)
    repo = service._actions
    action = repo.get_action(identity.action_id)
    granted, advanced_grant = repo.acquire_lease(identity.action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
    assert granted.fencing_token == 1 and advanced_grant.state is LifecycleState.LEASED
    with pytest.raises(ControlPlaneError, match="snapshot-captured|version"):
        # A second grant is refused: the action left the grantable state and
        # the version moved; exactly one active lease exists.
        repo.acquire_lease(identity.action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
    lease = repo.active_lease(identity.action_id, now=NOW + timedelta(minutes=3))
    assert lease is not None and lease.fencing_token == 1
    db.close()


def test_stale_fencing_token_cannot_execute(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, snapshot = prepared_action(tmp_path)
    result = service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert result.lifecycle_state is LifecycleState.VERIFIED_SUCCESS
    # A forged contract carrying the superseded token cannot execute anything.
    from aipm.control_plane.executor import ExecutionRefused

    executor = service._executor()
    contract = ExecutionContract(
        contract_version=EXECUTION_CONTRACT_VERSION,
        action_id=identity.action_id,
        action_version=1,
        operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
        target_id="project-demo",
        environment="staging",
        plan_id=identity.plan_id,
        expected_plan_revision=1,
        expected_plan_digest=identity.target_digest,
        mutation_fields=(("title", "New title"),),
        snapshot_id=snapshot.snapshot_id,
        decision_id=decision.decision_id,
        confirmation_id=_confirmation_id(db, identity.action_id),
        policy_version="policy-v1",
        verification_version="mc612-verification-v1",
        kill_switch_epoch=1,
        lease_id="f" * 32,
        fencing_token=99,
        expires_at=NOW + timedelta(minutes=30),
    )
    with pytest.raises(ExecutionRefused, match="version|lease"):
        executor.execute(contract, now=NOW + timedelta(minutes=5))
    db.close()


def test_expired_lease_cannot_commit_after_execution_started(tmp_path: Path):
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
    # The lease expires after the mutation boundary was crossed: no commit.
    with db.connection:
        db.connection.execute(
            "UPDATE execution_leases SET expires_at = ? WHERE action_id = ?",
            ((NOW - timedelta(minutes=1)).isoformat(), identity.action_id),
        )
    from aipm.control_plane.executor import EXECUTION_CONTRACT_VERSION, ExecutionContract, ExecutionRefused, ExecutorCapability

    executor = service._executor()
    contract = ExecutionContract(
        contract_version=EXECUTION_CONTRACT_VERSION,
        action_id=identity.action_id,
        action_version=running.version,
        operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
        target_id="project-demo",
        environment="staging",
        plan_id=identity.plan_id,
        expected_plan_revision=1,
        expected_plan_digest=identity.target_digest,
        mutation_fields=(("title", "New title"),),
        snapshot_id=snapshot.snapshot_id,
        decision_id=decision.decision_id,
        confirmation_id=_confirmation_id(db, identity.action_id),
        policy_version="policy-v1",
        verification_version="mc612-verification-v1",
        kill_switch_epoch=1,
        lease_id=_lease.lease_id,
        fencing_token=_lease.fencing_token,
        expires_at=NOW + timedelta(minutes=30),
    )
    with pytest.raises(ExecutionRefused, match="lease"):
        executor.execute(contract, now=NOW + timedelta(minutes=4))
    assert plans.read("project-demo").revision == 1
    assert repo.outcome_for_action(identity.action_id) == "mutation_not_started"
    db.close()


def test_stale_action_version_refuses_execution(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, snapshot = prepared_action(tmp_path)
    from aipm.control_plane.executor import ExecutionRefused

    executor = service._executor()
    from aipm.control_plane.executor import EXECUTION_CONTRACT_VERSION, ExecutorCapability

    contract = ExecutionContract(
        contract_version=EXECUTION_CONTRACT_VERSION,
        action_id=identity.action_id,
        action_version=41,
        operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
        target_id="project-demo",
        environment="staging",
        plan_id=identity.plan_id,
        expected_plan_revision=1,
        expected_plan_digest=identity.target_digest,
        mutation_fields=(("title", "New title"),),
        snapshot_id=snapshot.snapshot_id,
        decision_id=decision.decision_id,
        confirmation_id=_confirmation_id(db, identity.action_id),
        policy_version="policy-v1",
        verification_version="mc612-verification-v1",
        kill_switch_epoch=1,
        lease_id="l" * 32,
        fencing_token=1,
        expires_at=NOW + timedelta(minutes=30),
    )
    with pytest.raises(ExecutionRefused, match="version"):
        executor.execute(contract, now=NOW + timedelta(minutes=3))
    db.close()


# ---------------------------------------------------------------------------
# Crash recovery around the mutation boundary
# ---------------------------------------------------------------------------


def test_crash_before_mutation_leaves_retry_allowed(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, snapshot = prepared_action(tmp_path)
    # Injected evidence failure at the mutation boundary rolls everything back.
    actions_repo = service._actions

    class _FailingSink:
        def append_in_transaction(self, draft):
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "injected")

    object.__setattr__(actions_repo, "_audit", _FailingSink())
    with pytest.raises(ControlPlaneError, match="injected"):
        service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    object.__setattr__(actions_repo, "_audit", ledger)
    # The mutation did NOT happen and the plan is untouched.
    assert plans.read("project-demo").revision == 1
    # The lease is still active and execution can be retried to completion.
    result = service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    assert result.lifecycle_state is LifecycleState.VERIFIED_SUCCESS
    assert plans.read("project-demo").revision == 2
    db.close()


def test_lost_response_after_mutation_is_reconciled_not_retried(tmp_path: Path):
    """Simulate: the mutation commits, the response is lost, the process dies.

    Recovery reconstructs the world by hand (as a restart would) and the
    reconciler classifies the outcome from independent observation — never by
    blindly re-executing the mutation.
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
    # The mutation "happened" (the world moved) but no durable success state exists.
    plans.update("project-demo", expected_revision=1, fields={"title": "New title"}, now=NOW + timedelta(minutes=3))
    repo.mark_outcome(identity.action_id, expected_version=running.version, outcome=ExecutionOutcomeRef.MUTATION_STARTED, now=NOW + timedelta(minutes=3))
    repo.mark_outcome(identity.action_id, expected_version=running.version, outcome=ExecutionOutcomeRef.UNKNOWN_OUTCOME, now=NOW + timedelta(minutes=3))

    reconciled = service.reconcile_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    assert reconciled.outcome.value == "mutation_succeeded"
    assert reconciled.lifecycle_state is LifecycleState.EXECUTED_PENDING_VERIFICATION
    # The plan was mutated exactly once.
    assert plans.read("project-demo").revision == 2
    db.close()


def test_reconciliation_of_pre_state_allows_policy_retry(tmp_path: Path):
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
    repo.mark_outcome(identity.action_id, expected_version=running.version, outcome=ExecutionOutcomeRef.UNKNOWN_OUTCOME, now=NOW)
    # The world did NOT move: the pre-state is intact.
    reconciled = service.reconcile_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    assert reconciled.outcome.value == "mutation_not_started"
    db.close()


def test_crash_after_success_state_resumes_to_verification_without_remutation(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, snapshot = prepared_action(tmp_path)
    result = service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert result.lifecycle_state is LifecycleState.VERIFIED_SUCCESS
    # Replay after the durable success state never repeats the mutation.
    service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    assert plans.read("project-demo").revision == 2
    db.close()


# ---------------------------------------------------------------------------
# End-to-end failure + rollback vertical slice
# ---------------------------------------------------------------------------


def test_end_to_end_verification_failure_and_rollback(tmp_path: Path):
    metadata = (("title", "New title"),)
    service, db, ledger, plans, clock, session, decision, identity, snapshot = prepared_action(tmp_path, metadata=metadata)
    actions_repo = service._actions

    # Deterministic test-induced mismatch: the mutation applies different
    # values than the contract authorized (a real mutation, wrong content).
    original_execute = SQLiteActionRepository.execute_plan_mutation

    def mismatched_mutation(self, action_id, expected_version, *, expected_revision, mutation_fields, now, audit_drafts=()):
        return original_execute(
            self,
            action_id,
            expected_version,
            expected_revision=expected_revision,
            mutation_fields={"title": "Wrong title"},
            now=now,
            audit_drafts=audit_drafts,
        )

    SQLiteActionRepository.execute_plan_mutation = mismatched_mutation
    result = service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2, seconds=30))
    SQLiteActionRepository.execute_plan_mutation = original_execute
    assert result.outcome.value == "verification_failed"
    assert result.lifecycle_state is LifecycleState.VERIFICATION_FAILED
    assert plans.read("project-demo").title == "Wrong title"
    assert plans.read("project-demo").revision == 2

    # Rollback request: a NEW bounded action referencing the original + snapshot.
    rollback_decision = service.request_rollback(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert rollback_decision.allowed is True
    rollback_identity = rollback_decision.action_identity
    assert rollback_identity is not None
    original = service.lifecycle(identity.action_id)
    assert original.state is LifecycleState.ROLLBACK_REQUESTED

    # Confirm the rollback action under the same single-owner policy.
    service.confirm(session.session_id, rollback_decision.decision_id, now=NOW + timedelta(minutes=3, seconds=20))
    service.capture_snapshot(session.session_id, rollback_identity.action_id, now=NOW + timedelta(minutes=3, seconds=40))

    rollback_result = service.execute_rollback(
        session.session_id,
        original_action_id=identity.action_id,
        rollback_action_id=rollback_identity.action_id,
        now=NOW + timedelta(minutes=4),
    )
    assert rollback_result.outcome.value == "rollback_succeeded"
    assert service.lifecycle(identity.action_id).state is LifecycleState.ROLLED_BACK
    assert service.lifecycle(rollback_identity.action_id).state is LifecycleState.VERIFIED_SUCCESS

    # The plan is restored to the snapshot's mutable values at a new revision.
    restored = plans.read("project-demo")
    assert restored.title == "Old title"
    assert restored.objective == "Objective"
    assert restored.revision == 3

    types = [event.event_type for event in ledger.events()]
    assert AuditEventType.ROLLBACK_REQUESTED in types
    assert AuditEventType.ROLLBACK_SUCCEEDED in types
    assert ledger.verify_chain().ok is True
    db.close()


def test_rollback_refused_when_concurrent_change_happened(tmp_path: Path):
    metadata = (("title", "New title"),)
    service, db, ledger, plans, clock, session, decision, identity, snapshot = prepared_action(tmp_path, metadata=metadata)
    actions_repo = service._actions
    original_execute = SQLiteActionRepository.execute_plan_mutation

    def mismatched_mutation(self, action_id, expected_version, *, expected_revision, mutation_fields, now, audit_drafts=()):
        return original_execute(
            self,
            action_id,
            expected_version,
            expected_revision=expected_revision,
            mutation_fields={"title": "Wrong title"},
            now=now,
            audit_drafts=audit_drafts,
        )

    SQLiteActionRepository.execute_plan_mutation = mismatched_mutation
    service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2, seconds=30))
    SQLiteActionRepository.execute_plan_mutation = original_execute
    rollback_decision = service.request_rollback(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    rollback_identity = rollback_decision.action_identity
    assert rollback_identity is not None
    service.confirm(session.session_id, rollback_decision.decision_id, now=NOW + timedelta(minutes=3, seconds=20))
    service.capture_snapshot(session.session_id, rollback_identity.action_id, now=NOW + timedelta(minutes=3, seconds=40))
    # A later legitimate change lands on top of the failed mutation.
    plans.update("project-demo", expected_revision=2, fields={"objective": "Later legitimate change"}, now=NOW + timedelta(minutes=4))
    from aipm.control_plane.executor import ExecutionRefused

    with pytest.raises(ExecutionRefused, match="stale_plan|post-condition"):
        service.execute_rollback(
            session.session_id,
            original_action_id=identity.action_id,
            rollback_action_id=rollback_identity.action_id,
            now=NOW + timedelta(minutes=4, seconds=20),
        )
    assert plans.read("project-demo").objective == "Later legitimate change"
    assert service.lifecycle(identity.action_id).state is LifecycleState.ROLLBACK_REQUESTED
    db.close()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_two_actions_on_one_plan_never_lose_updates(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, snapshot = prepared_action(tmp_path)
    # Authorize a second action against the SAME plan revision.
    decision2 = service.authorize(session.session_id, request(idempotency_key="idem-002", metadata=(("objective", "Second objective"),)), now=NOW + timedelta(minutes=2, seconds=30))
    identity2 = decision2.action_identity
    assert identity2 is not None
    service.confirm(session.session_id, decision2.decision_id, now=NOW + timedelta(minutes=3))
    service.capture_snapshot(session.session_id, identity2.action_id, now=NOW + timedelta(minutes=3, seconds=20))

    first = service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3, seconds=40))
    assert first.lifecycle_state is LifecycleState.VERIFIED_SUCCESS
    # The second action's authorized precondition (revision 1) is now stale.
    from aipm.control_plane.executor import ExecutionRefused

    with pytest.raises(ExecutionRefused, match="stale_plan|no longer matches"):
        service.execute_action(session.session_id, identity2.action_id, now=NOW + timedelta(minutes=4))
    assert plans.read("project-demo").revision == 2
    db.close()


# ---------------------------------------------------------------------------
# Security: closed capability surface
# ---------------------------------------------------------------------------


def test_executor_capability_is_closed_and_source_is_clean():
    assert [capability.value for capability in ExecutorCapability] == ["update_project_plan"]
    from pathlib import Path as _Path

    source = (_Path("src/aipm/control_plane") / "executor.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "systemctl", "docker", "socket", "urllib", "requests", "httpx", "UpdateEngine"):
        assert forbidden not in source, forbidden


from aipm.control_plane.verification import ExecutionOutcome as ExecutionOutcomeRef  # noqa: E402
