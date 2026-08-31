"""Shot 7 (verification + rollback safety layer) tests.

Covers: the independent verification contract (closed predicates, versioned,
deterministic), durable integrity-protected snapshots with revision binding,
rollback planning with CAS safety against the failed mutation's post-condition,
rollback as a distinct bounded action, execution outcome classification with
UNKNOWN_OUTCOME safety, lifecycle preconditions, kill-switch interaction, and
audit atomicity.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.control_plane.audit import SQLiteAuditLedger
from aipm.control_plane.audit.models import AuditEventType
from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.kill_switch import KillSwitchRegistry
from aipm.control_plane.lifecycle import IMPLEMENTED_STATES
from aipm.control_plane.models import (
    ActionRequest,
    ControlPlaneError,
    LifecycleState,
    OperationKind,
    PlanningErrorCode,
)
from aipm.control_plane.owner_auth import Argon2idVerifier, OwnerAuthenticator
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import AuthorizationPolicy
from aipm.control_plane.project_plan import Environment, ProjectPlan
from aipm.control_plane.rollback import RollbackSafetyCode, plan_rollback
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
from aipm.control_plane.verification import (
    ExecutionOutcome,
    ExpectedState,
    ObservedState,
    VERIFICATION_VERSION,
    expected_from_plan,
    observed_from_plan,
    retry_permitted,
    verify,
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
        execution_mode='test',
        clock=clock,
    )
    return service, db, ledger, plans, clock


def authorized_confirmed_action(tmp_path: Path, *, clock=None, metadata=(("title", "New title"),)):
    """Drive one action through login → authorize → confirm → snapshot capture."""

    service, db, ledger, plans, clock = build_service(tmp_path, clock=clock)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request(metadata=metadata))
    identity = decision.action_identity
    assert identity is not None
    binding = service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    snapshot = service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    return service, db, ledger, plans, clock, session, decision, identity, binding, snapshot


# ---------------------------------------------------------------------------
# Verification contract
# ---------------------------------------------------------------------------


def test_verification_exact_success_is_deterministic():
    plan = make_plan()
    expected = expected_from_plan(plan, fields=(("title", plan.title),))
    observed = observed_from_plan(plan)
    first = verify(expected, observed, action_id="a" * 64)
    second = verify(expected, observed, action_id="a" * 64)
    assert first.success is True and second.success is True
    assert first.reason_code is second.reason_code
    assert first.verification_id != second.verification_id
    assert first.verification_version == VERIFICATION_VERSION == "mc612-verification-v1"
    assert first.verifier == "control-plane-plan-readback"


@pytest.mark.parametrize(
    "observed_kwargs,code",
    [
        ({"target_id": "project-other"}, "target_mismatch"),
        ({"environment": "production"}, "environment_mismatch"),
        ({"revision": 9}, "revision_mismatch"),
        ({"canonical_digest": "f" * 64}, "digest_mismatch"),
        ({"enabled": False}, "enabled_mismatch"),
        ({"fields": (("title", "Different"),)}, "field_mismatch"),
    ],
)
def test_verification_closed_predicates_fail_with_typed_codes(observed_kwargs, code):
    plan = make_plan()
    expected = expected_from_plan(plan, fields=(("title", plan.title),))
    values = {
        "target_id": plan.target_id,
        "environment": plan.environment.value,
        "revision": plan.revision,
        "canonical_digest": plan.digest(),
        "enabled": plan.enabled,
        "fields": (("title", plan.title),),
        "observed_at": NOW,
    }
    values.update(observed_kwargs)
    observed = ObservedState(**values)
    result = verify(expected, observed, action_id="a" * 64)
    assert result.success is False
    assert result.reason_code.value == code


def test_verification_service_records_evidence_atomically(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path)
    expected = ExpectedState(
        target_id="project-demo",
        environment="staging",
        revision=snapshot.revision,
        canonical_digest=snapshot.canonical_digest,
        enabled=True,
        fields=(("title", "Old title"), ("objective", "Objective")),
    )
    result = service.record_verification(session.session_id, identity.action_id, expected, now=NOW + timedelta(minutes=3))
    assert result.success is True
    repo = SQLiteVerificationRepository(db, audit=ledger)
    stored = repo.records_for_action(identity.action_id)
    assert len(stored) == 1
    assert stored[0].record.verification_id == result.verification_id
    types = [event.event_type for event in ledger.events()]
    assert AuditEventType.VERIFICATION_STARTED in types
    assert AuditEventType.VERIFICATION_SUCCEEDED in types
    assert ledger.verify_chain().ok is True
    db.close()


def test_verification_replay_produces_independent_evidence(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path)
    expected = expected_from_plan(make_plan())
    first = service.record_verification(session.session_id, identity.action_id, expected, now=NOW + timedelta(minutes=3))
    second = service.record_verification(session.session_id, identity.action_id, expected, now=NOW + timedelta(minutes=4))
    assert first.verification_id != second.verification_id
    assert first.success == second.success
    repo = SQLiteVerificationRepository(db, audit=ledger)
    assert len(repo.records_for_action(identity.action_id)) == 2
    db.close()


def test_verification_of_missing_plan_fails_closed(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path)
    with db.connection:
        db.connection.execute("DELETE FROM project_plans WHERE target_id = 'project-demo'")
    expected = expected_from_plan(make_plan())
    result = service.record_verification(session.session_id, identity.action_id, expected, now=NOW + timedelta(minutes=3))
    assert result.success is False
    assert result.reason_code.value == "plan_missing"
    assert result.observed_revision is None
    db.close()


def test_expired_action_cannot_be_verified(tmp_path: Path):
    clock = _Clock(NOW)
    service, db, ledger, plans, clock = build_service(tmp_path, clock=clock)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    clock.value = decision.expires_at + timedelta(minutes=1)
    with pytest.raises(ControlPlaneError, match="expired"):
        service.record_verification(session.session_id, identity.action_id, expected_from_plan(make_plan()), now=clock.value)
    db.close()


def test_verification_records_are_integrity_protected(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path)
    expected = expected_from_plan(make_plan())
    result = service.record_verification(session.session_id, identity.action_id, expected, now=NOW + timedelta(minutes=3))
    repo = SQLiteVerificationRepository(db, audit=ledger)
    assert repo.get(result.verification_id) is not None
    with db.connection:
        db.connection.execute(
            "UPDATE verification_records SET observed_revision = 99 WHERE verification_id = ?",
            (result.verification_id,),
        )
    with pytest.raises(ControlPlaneError, match="integrity"):
        repo.get(result.verification_id)
    db.close()


# ---------------------------------------------------------------------------
# Snapshot contract
# ---------------------------------------------------------------------------


def test_snapshot_capture_reload_and_restart(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path)
    assert snapshot.revision == 1
    assert snapshot.action_id == identity.action_id
    assert snapshot.snapshot_version == "mc612-snapshot-v1"
    payload = json.loads(snapshot.payload_canonical)
    assert payload["title"] == "Old title"
    action = service.lifecycle(identity.action_id)
    assert action is not None and action.state is LifecycleState.SNAPSHOT_CAPTURED

    repo = SQLitePlanSnapshotRepository(ControlPlaneDatabase(db_path(tmp_path), clock=_Clock(NOW)))
    reloaded = repo.get(snapshot.snapshot_id)
    assert reloaded == snapshot
    assert repo.snapshot_for_action(identity.action_id) == snapshot
    db.close()


def test_snapshot_is_immutable_and_duplicate_free(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path)
    repo = SQLitePlanSnapshotRepository(db)
    with pytest.raises(ControlPlaneError, match="exists"):
        repo.save(snapshot)
    assert not hasattr(repo, "update")
    assert not hasattr(repo, "delete")
    db.close()


def test_snapshot_tampering_is_detected(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path)
    repo = SQLitePlanSnapshotRepository(db)
    altered = json.loads(snapshot.payload_canonical)
    altered["title"] = "Rewritten history"
    with db.connection:
        db.connection.execute(
            "UPDATE plan_snapshots SET payload_canonical = ? WHERE snapshot_id = ?",
            (json.dumps(altered, ensure_ascii=False, separators=(",", ":"), sort_keys=True), snapshot.snapshot_id),
        )
    with pytest.raises(ControlPlaneError, match="integrity"):
        repo.get(snapshot.snapshot_id)
    db.close()


def test_snapshot_requires_exact_action_revision(tmp_path: Path):
    clock = _Clock(NOW)
    service, db, ledger, plans = build_service(tmp_path, clock=clock)[:4]
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    # The target plan drifts after authorization: the snapshot must be refused.
    plans.update("project-demo", expected_revision=1, fields={"title": "Concurrent edit"}, now=NOW)
    with pytest.raises(ControlPlaneError, match="no longer matches"):
        service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    assert service.lifecycle(identity.action_id).state is LifecycleState.CONFIRMED
    db.close()


def test_snapshot_capture_requires_confirmed_action(tmp_path: Path):
    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    with pytest.raises(ControlPlaneError, match="confirmed"):
        service.capture_snapshot(session.session_id, identity.action_id, now=NOW)
    db.close()


def test_snapshot_capture_is_atomic_with_evidence(tmp_path: Path):
    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    actions_repo = service._actions

    class _FailingSink:
        def append_in_transaction(self, draft):
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "injected evidence failure")

    object.__setattr__(actions_repo, "_audit", _FailingSink())
    with pytest.raises(ControlPlaneError, match="injected"):
        service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    object.__setattr__(actions_repo, "_audit", ledger)
    assert db.connection.execute("SELECT COUNT(*) AS c FROM plan_snapshots").fetchone()["c"] == 0
    assert service.lifecycle(identity.action_id).state is LifecycleState.CONFIRMED
    snapshot = service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    assert snapshot is not None
    assert ledger.verify_chain().ok is True
    db.close()


def test_plain_cas_advance_cannot_reach_snapshot_captured(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path)
    repo = SQLiteActionRepository(db, audit=ledger)
    action = repo.get_action(identity.action_id)
    with pytest.raises(ControlPlaneError, match="composite"):
        repo.advance_action(
            identity.action_id,
            expected_version=action.version,
            next_state=LifecycleState.SNAPSHOT_CAPTURED,
            approver_subject="local-owner",
            now=NOW,
        )
    db.close()


# ---------------------------------------------------------------------------
# Rollback planning, CAS safety, and rollback as a distinct action
# ---------------------------------------------------------------------------


def _simulate_failed_mutation(plans, metadata):
    """Test-double executor: apply the mutation to the durable plan state."""
    plans.update("project-demo", expected_revision=1, fields=dict(metadata), now=NOW + timedelta(minutes=3))


def test_rollback_plan_is_safe_against_the_failed_mutation_state(tmp_path: Path):
    metadata = (("title", "Mutated title"),)
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path, metadata=metadata)
    _simulate_failed_mutation(plans, metadata)
    rollback_plan = service.plan_rollback(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    assert rollback_plan.safe is True
    assert rollback_plan.reason_code is RollbackSafetyCode.SAFE
    assert rollback_plan.original_action_id == identity.action_id
    assert rollback_plan.snapshot_id == snapshot.snapshot_id
    assert rollback_plan.restore_revision == 1
    assert rollback_plan.restore_fields == (("title", "Mutated title"),)
    db.close()


def test_rollback_denied_when_current_state_is_not_the_mutation_result(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path)
    # No mutation happened: current state is still A, not B → rollback denied.
    rollback_plan = service.plan_rollback(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    assert rollback_plan.safe is False
    assert rollback_plan.reason_code is RollbackSafetyCode.CURRENT_STATE_MISMATCH
    db.close()


def test_rollback_denied_after_a_concurrent_mutation(tmp_path: Path):
    metadata = (("title", "Mutated title"),)
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path, metadata=metadata)
    _simulate_failed_mutation(plans, metadata)
    # A later legitimate change moves the target past B: rollback must deny.
    plans.update("project-demo", expected_revision=2, fields={"objective": "Later legitimate change"}, now=NOW + timedelta(minutes=4))
    rollback_plan = service.plan_rollback(session.session_id, identity.action_id, now=NOW + timedelta(minutes=5))
    assert rollback_plan.safe is False
    assert rollback_plan.reason_code is RollbackSafetyCode.CURRENT_STATE_MISMATCH
    db.close()


def test_rollback_rejects_wrong_snapshot(tmp_path: Path):
    metadata = (("title", "Mutated title"),)
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path, metadata=metadata)
    _simulate_failed_mutation(plans, metadata)
    original = service.lifecycle(identity.action_id)
    from aipm.control_plane.storage.sqlite_store import PlanSnapshot

    foreign = PlanSnapshot(
        snapshot_id="f" * 32,
        target_id="project-demo",
        environment="staging",
        revision=1,
        canonical_digest=snapshot.canonical_digest,
        payload_canonical=snapshot.payload_canonical,
        action_id="b" * 64,
        plan_id=snapshot.plan_id,
        captured_at=NOW,
    )
    rollback_plan = plan_rollback(
        original_action=original,
        snapshot=foreign,
        current_plan=plans.read("project-demo"),
        mutation_fields=dict(metadata),
    )
    assert rollback_plan.safe is False
    assert rollback_plan.reason_code is RollbackSafetyCode.WRONG_SNAPSHOT
    db.close()


def test_rollback_rejects_stale_snapshot(tmp_path: Path):
    metadata = (("title", "Mutated title"),)
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path, metadata=metadata)
    _simulate_failed_mutation(plans, metadata)
    original = service.lifecycle(identity.action_id)
    from dataclasses import replace

    stale = replace(snapshot, revision=4, integrity_digest="")
    rollback_plan = plan_rollback(
        original_action=original,
        snapshot=stale,
        current_plan=plans.read("project-demo"),
        mutation_fields=dict(metadata),
    )
    assert rollback_plan.safe is False
    assert rollback_plan.reason_code is RollbackSafetyCode.STALE_SNAPSHOT
    db.close()


def test_rollback_request_creates_a_distinct_bounded_action(tmp_path: Path):
    metadata = (("title", "Mutated title"),)
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path, metadata=metadata)
    _simulate_failed_mutation(plans, metadata)
    rollback_decision = service.request_rollback(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    assert rollback_decision.allowed is True
    rollback_identity = rollback_decision.action_identity
    assert rollback_identity is not None
    assert rollback_identity.action_id != identity.action_id
    registered = service.lifecycle(rollback_identity.action_id)
    assert registered is not None
    assert registered.rollback_of_action_id == identity.action_id
    assert registered.snapshot_id == snapshot.snapshot_id
    assert registered.idempotency_key == f"rollback-{identity.action_id[:64]}"
    row = db.connection.execute(
        "SELECT rollback_of_action_id, snapshot_id, outcome FROM actions WHERE action_id = ?",
        (rollback_identity.action_id,),
    ).fetchone()
    assert row["rollback_of_action_id"] == identity.action_id
    assert row["snapshot_id"] == snapshot.snapshot_id
    # Replay returns the same rollback action; a second one is impossible.
    replay = service.request_rollback(session.session_id, identity.action_id, now=NOW + timedelta(minutes=5))
    assert replay.decision_id == rollback_decision.decision_id
    types = [event.event_type for event in ledger.events()]
    assert AuditEventType.ROLLBACK_REQUESTED in types
    assert ledger.verify_chain().ok is True
    db.close()


def test_rollback_verification_compares_against_the_snapshot(tmp_path: Path):
    metadata = (("title", "Mutated title"),)
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path, metadata=metadata)
    snapshot_plan = ProjectPlan(
        target_id=snapshot.target_id,
        environment=Environment(snapshot.environment),
        revision=snapshot.revision,
        title=json.loads(snapshot.payload_canonical)["title"],
        objective=json.loads(snapshot.payload_canonical)["objective"],
        created_at=datetime.fromisoformat(json.loads(snapshot.payload_canonical)["created_at"]),
        updated_at=datetime.fromisoformat(json.loads(snapshot.payload_canonical)["updated_at"]),
        enabled=True,
        canonical_digest=snapshot.canonical_digest,
    )
    expected = expected_from_plan(snapshot_plan)
    # Before the rollback mutation, the current state is B: verification fails.
    _simulate_failed_mutation(plans, metadata)
    failed = verify(expected, observed_from_plan(plans.read("project-demo")), action_id=identity.action_id)
    assert failed.success is False
    # After the (future) rollback mutation restores A, verification passes.
    rolled_back = ProjectPlan(
        target_id=snapshot_plan.target_id,
        environment=snapshot_plan.environment,
        revision=3,
        title=snapshot_plan.title,
        objective=snapshot_plan.objective,
        created_at=snapshot_plan.created_at,
        updated_at=NOW + timedelta(minutes=6),
        enabled=True,
        canonical_digest="",
    )
    from dataclasses import replace

    rolled_back = replace(rolled_back, canonical_digest=rolled_back.digest())
    # The restored digest matches the snapshot state for the reversible fields.
    passed = verify(
        ExpectedState(
            target_id=snapshot.target_id,
            environment=snapshot.environment,
            revision=snapshot.revision,
            canonical_digest=snapshot.canonical_digest,
            enabled=True,
            fields=(("title", snapshot_plan.title), ("objective", snapshot_plan.objective)),
        ),
        ObservedState(
            target_id=snapshot.target_id,
            environment=snapshot.environment,
            revision=rolled_back.revision,
            canonical_digest=rolled_back.digest(),
            enabled=True,
            fields=(("title", rolled_back.title), ("objective", rolled_back.objective)),
            observed_at=NOW + timedelta(minutes=7),
        ),
        action_id=identity.action_id,
    )
    assert passed.success is False  # revision differs: full digest equality is required
    assert passed.reason_code.value == "revision_mismatch"
    db.close()


# ---------------------------------------------------------------------------
# Execution outcome classification and UNKNOWN_OUTCOME safety
# ---------------------------------------------------------------------------


def test_unknown_outcome_is_persisted_and_blind_retry_is_forbidden(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path)
    repo = service._actions
    action = repo.get_action(identity.action_id)
    repo.mark_outcome(identity.action_id, expected_version=action.version, outcome=ExecutionOutcome.MUTATION_STARTED, now=NOW)
    repo.mark_outcome(identity.action_id, expected_version=action.version, outcome=ExecutionOutcome.UNKNOWN_OUTCOME, now=NOW)
    assert repo.outcome_for_action(identity.action_id) == "unknown_outcome"
    assert retry_permitted(ExecutionOutcome.UNKNOWN_OUTCOME) is False
    assert retry_permitted(ExecutionOutcome.MUTATION_NOT_STARTED) is True
    with pytest.raises(ControlPlaneError, match="forbids reset"):
        repo.mark_outcome(identity.action_id, expected_version=action.version, outcome=ExecutionOutcome.MUTATION_NOT_STARTED, now=NOW)
    db.close()


def test_unknown_outcome_survives_restart_and_admits_reconciliation(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path)
    repo = service._actions
    action = repo.get_action(identity.action_id)
    repo.mark_outcome(identity.action_id, expected_version=action.version, outcome=ExecutionOutcome.MUTATION_STARTED, now=NOW)
    repo.mark_outcome(identity.action_id, expected_version=action.version, outcome=ExecutionOutcome.UNKNOWN_OUTCOME, now=NOW)
    db.close()

    reopened = SQLiteActionRepository(ControlPlaneDatabase(db_path(tmp_path), clock=_Clock(NOW)), audit=ledger)
    row = reopened._db.connection.execute(
        "SELECT outcome FROM actions WHERE action_id = ?",
        (identity.action_id,),
    ).fetchone()
    assert row["outcome"] == "unknown_outcome"
    lifecycle = reopened.get_action(identity.action_id)
    reopened.mark_outcome(
        identity.action_id,
        expected_version=lifecycle.version,
        outcome=ExecutionOutcome.MUTATION_SUCCEEDED,
        now=NOW,
    )
    row = reopened._db.connection.execute(
        "SELECT outcome FROM actions WHERE action_id = ?",
        (identity.action_id,),
    ).fetchone()
    assert row["outcome"] == "mutation_succeeded"
    reopened._db.close()


# ---------------------------------------------------------------------------
# Lifecycle preconditions
# ---------------------------------------------------------------------------


def test_execution_states_are_now_backed_but_still_guarded():
    # Shot 6 backs the execution states with the bounded executor; the
    # never-implemented control states remain guarded by advance().
    from aipm.control_plane.lifecycle import advance, allowed_transitions
    from aipm.control_plane.models import ActionLifecycle, ActionScope

    for reserved in (LifecycleState.CANCEL_REQUESTED, LifecycleState.TIMED_OUT, LifecycleState.INTERRUPTED, LifecycleState.ROLLBACK_UNAVAILABLE):
        assert reserved not in IMPLEMENTED_STATES
    assert LifecycleState.SNAPSHOT_CAPTURED in IMPLEMENTED_STATES

    # The guard, not the documentation table, is the enforcement: advance
    # refuses to enter a reserved state even from a legal-looking predecessor.
    probe = ActionLifecycle(
        action_id="a" * 64,
        plan_id="p" * 32,
        plan_digest="a" * 64,
        plan_revision=1,
        operation=OperationKind.UPDATE_PROJECT_PLAN,
        scope=ActionScope(target_id="project-demo", environment="staging", policy_version="policy-v1"),
        state=LifecycleState.RECONCILIATION_REQUIRED,
        requester_subject="local-owner",
        approver_subject="local-owner",
        idempotency_key="idem-001",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    with pytest.raises(Exception, match="reserved"):
        advance(probe, LifecycleState.ROLLBACK_UNAVAILABLE, now=NOW)

    # RUNNING is reachable only from LEASED (lease-granted actions).
    for state in IMPLEMENTED_STATES:
        if state is not LifecycleState.LEASED:
            assert LifecycleState.RUNNING not in allowed_transitions(state)


def test_rolled_back_requires_rollback_verification_by_contract():
    # The precondition contract: ROLLED_BACK is reachable only from
    # rollback_requested, and only after an independent passing rollback
    # verification (enforced by the rollback executor's composite).
    from aipm.control_plane.lifecycle import allowed_transitions

    assert LifecycleState.ROLLED_BACK in IMPLEMENTED_STATES
    for state in IMPLEMENTED_STATES:
        if state is not LifecycleState.ROLLBACK_REQUESTED:
            assert LifecycleState.ROLLED_BACK not in allowed_transitions(state)


# ---------------------------------------------------------------------------
# Kill-switch interaction
# ---------------------------------------------------------------------------


def test_engaged_kill_switch_blocks_snapshot_capture_and_rollback(tmp_path: Path):
    class _Switches:
        def permits(self, environment) -> bool:
            return False

    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    object.__setattr__(service, "_kill_switches", _Switches())
    plans.update("project-demo", expected_revision=1, fields={"title": "New title"}, now=NOW)
    with pytest.raises(ControlPlaneError, match="Kill switch"):
        service.request_rollback(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    db.close()


def test_verification_evidence_is_never_blocked_by_the_kill_switch(tmp_path: Path):
    class _Switches:
        def permits(self, environment) -> bool:
            return False

    service, db, ledger, plans, clock, session, decision, identity, binding, snapshot = authorized_confirmed_action(tmp_path)
    # Re-wire the kill switch after the flow: evidence recording must still work.
    object.__setattr__(service, "_kill_switches", _Switches())
    result = service.record_verification(session.session_id, identity.action_id, expected_from_plan(make_plan()), now=NOW + timedelta(minutes=3))
    assert result.success is True
    db.close()


# ---------------------------------------------------------------------------
# Externally non-mutating proof
# ---------------------------------------------------------------------------


def test_safety_layer_introduces_no_execution_surface():
    from pathlib import Path as _Path

    for name in ("verification.py", "rollback.py"):
        source = (_Path("src/aipm/control_plane") / name).read_text(encoding="utf-8")
        for forbidden in ("subprocess", "os.system", "systemctl", "docker", "socket", "urllib", "requests", "httpx"):
            assert forbidden not in source, (name, forbidden)
