"""Shot 5 (durable control-plane state) tests.

Covers: dedicated database architecture, schema versioning, permissions,
ProjectPlan CAS persistence, action/decision/confirmation persistence,
database-enforced idempotency, CAS-guarded lifecycle transitions, kill-switch
persistence, crash/restart recovery, corruption fail-closed behavior, and
database isolation from the telemetry plane.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.control_plane.action_state import InMemoryActionRepository
from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.audit import SQLiteAuditLedger
from aipm.control_plane.contracts import LifecycleTransition
from aipm.control_plane.identity import AuthenticationMethod, OwnerPrincipal, PrincipalVerification
from aipm.control_plane.kill_switch import KillSwitchError, KillSwitchRegistry, KillSwitchState
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
from aipm.control_plane.project_plan import Environment, PlanConflict, ProjectPlan
from aipm.control_plane.service import OwnerControlPlaneService
from aipm.control_plane.session import OwnerSessionStore
from aipm.control_plane.storage import (
    ControlPlaneDatabase,
    ControlPlaneStorageUnavailable,
    ExecutionLease,
    PlanSnapshot,
    SQLiteActionRepository,
    SQLiteKillSwitchStore,
    SQLiteLeaseRepository,
    SQLitePlanSnapshotRepository,
    SQLiteProjectPlanStore,
)
from aipm.control_plane.storage.schema import SCHEMA_VERSION

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


def build_durable_service(tmp_path: Path, *, plan=None, clock=None):
    clock = clock or _Clock(NOW)
    db = ControlPlaneDatabase(db_path(tmp_path))
    targets = {"project-demo"}
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=clock)
    sessions = OwnerSessionStore(clock=clock)
    policy = AuthorizationPolicy(policy_version="policy-v1", allowed_scopes=frozenset({("project-demo", "staging")}))
    confirmations = OwnerConfirmationService(clock=clock)
    plans = SQLiteProjectPlanStore(db)
    try:
        plans.create(plan or make_plan())
    except PlanConflict:
        pass  # restart simulation: the plan already exists in this database
    planner = PlanOnlyPlanner(clock=clock, target_allow_list=targets)
    ledger = SQLiteAuditLedger(db)
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
        execution_mode='test',
        clock=clock,
    )
    return service, db, plans, clock


# ---------------------------------------------------------------------------
# Database architecture
# ---------------------------------------------------------------------------


def test_database_is_created_with_versioned_schema_and_safe_permissions(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path))
    try:
        assert db.journal_mode() == "delete"
        assert db.schema_version() == SCHEMA_VERSION == 5  # v5 adds contract evidence + csrf_token_hash
        assert stat.S_IMODE(os.stat(db.path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(db.path.parent).st_mode) == 0o700
        names = {row[0] for row in db.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"project_plans", "authorization_decisions", "actions", "confirmations", "kill_switch_state", "execution_leases", "plan_snapshots"} <= names
    finally:
        db.close()


def test_database_refuses_too_open_permissions(tmp_path: Path):
    path = db_path(tmp_path)
    path.write_bytes(b"")
    os.chmod(path, 0o644)
    with pytest.raises(ControlPlaneStorageUnavailable, match="permissions"):
        ControlPlaneDatabase(path)


def test_database_refuses_newer_schema_version(tmp_path: Path):
    path = db_path(tmp_path)
    db = ControlPlaneDatabase(path)
    db.close()
    raw = sqlite3.connect(str(path))
    raw.execute("UPDATE control_plane_schema_meta SET schema_version = 99")
    raw.commit()
    raw.close()
    with pytest.raises(ControlPlaneStorageUnavailable, match="newer"):
        ControlPlaneDatabase(path)


def test_database_default_path_is_dedicated_and_configurable():
    from aipm.control_plane.storage.sqlite_store import default_database_path

    default = default_database_path()
    assert default.name == "control_plane.db"
    assert "control_plane" in str(default)
    assert "mission_control" not in str(default)
    monkey_env = "/tmp/cp-explicit/control_plane.db"
    os.environ["AIPM_CONTROL_PLANE_DB"] = monkey_env
    try:
        assert default_database_path() == Path(monkey_env)
    finally:
        os.environ.pop("AIPM_CONTROL_PLANE_DB", None)


# ---------------------------------------------------------------------------
# ProjectPlan persistence
# ---------------------------------------------------------------------------


def test_project_plan_create_read_update_survives_restart(tmp_path: Path):
    plans = SQLiteProjectPlanStore(ControlPlaneDatabase(db_path(tmp_path)))
    plan = make_plan()
    plans.create(plan)
    assert plans.read("project-demo") == plan
    updated = plans.update("project-demo", expected_revision=1, fields={"title": "Rev2"}, now=NOW + timedelta(minutes=1))
    assert updated.revision == 2
    assert updated.digest() == updated.canonical_digest

    reopened = SQLiteProjectPlanStore(ControlPlaneDatabase(db_path(tmp_path)))
    assert reopened.read("project-demo") == updated
    further = reopened.update("project-demo", expected_revision=2, fields={"objective": "New objective"}, now=NOW + timedelta(minutes=2))
    assert further.revision == 3


def test_project_plan_stale_revision_is_rejected_without_partial_update(tmp_path: Path):
    plans = SQLiteProjectPlanStore(ControlPlaneDatabase(db_path(tmp_path)))
    plans.create(make_plan())
    plans.update("project-demo", expected_revision=1, fields={"title": "Rev2"}, now=NOW)
    with pytest.raises(PlanConflict, match="stale"):
        plans.update("project-demo", expected_revision=1, fields={"title": "Rev3"}, now=NOW)
    current = plans.read("project-demo")
    assert current.revision == 2
    assert current.title == "Rev2"


def test_project_plan_concurrent_update_only_one_wins(tmp_path: Path):
    plans_a = SQLiteProjectPlanStore(ControlPlaneDatabase(db_path(tmp_path)))
    plans_b = SQLiteProjectPlanStore(ControlPlaneDatabase(db_path(tmp_path)))
    plans_a.create(make_plan())
    first = plans_a.update("project-demo", expected_revision=1, fields={"title": "From A"}, now=NOW)
    with pytest.raises(PlanConflict):
        plans_b.update("project-demo", expected_revision=1, fields={"title": "From B"}, now=NOW)
    assert plans_b.read("project-demo") == first


def test_project_plan_immutables_and_production_are_enforced(tmp_path: Path):
    plans = SQLiteProjectPlanStore(ControlPlaneDatabase(db_path(tmp_path)))
    plans.create(make_plan())
    with pytest.raises((PlanConflict, ValueError)):
        plans.update("project-demo", expected_revision=1, fields={"environment": "production"}, now=NOW)
    with pytest.raises(ValueError):
        plans.create(ProjectPlan.create(target_id="prod-target", environment=Environment.PRODUCTION, title="T", objective="O", now=NOW))


def test_project_plan_missing_record_fails(tmp_path: Path):
    plans = SQLiteProjectPlanStore(ControlPlaneDatabase(db_path(tmp_path)))
    with pytest.raises(Exception, match="not registered"):
        plans.read("absent-target")


# ---------------------------------------------------------------------------
# Action + decision persistence and exact identity preservation
# ---------------------------------------------------------------------------


def test_action_save_load_preserves_exact_identity_fields(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path))
    actions = SQLiteActionRepository(db)
    service, db2, plans, clock = build_durable_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    loaded_action = actions.get_action(identity.action_id)
    loaded_decision = actions.get_decision(decision.decision_id)
    assert loaded_action is not None and loaded_decision is not None
    assert loaded_decision.action_identity == identity
    assert loaded_decision.action_identity.action_id == identity.action_id
    assert loaded_decision.action_identity.plan_id == identity.plan_id
    assert loaded_decision.action_identity.plan_digest == identity.plan_digest
    assert loaded_decision.action_identity.policy_version == identity.policy_version
    assert loaded_decision.action_identity.requester_subject == identity.requester_subject
    assert loaded_decision.action_identity.target_id == identity.target_id
    assert loaded_decision.action_identity.environment == identity.environment
    assert loaded_decision.action_identity.target_revision == identity.target_revision
    assert loaded_action == service.lifecycle(identity.action_id)
    assert loaded_action.state is LifecycleState.CONFIRMATION_REQUIRED
    db.close()
    db2.close()


def test_action_recovery_after_process_restart(tmp_path: Path):
    service, db1, _plans, _clock = build_durable_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    db1.close()

    actions = SQLiteActionRepository(ControlPlaneDatabase(db_path(tmp_path)))
    recovered = actions.get_action(identity.action_id)
    assert recovered is not None
    assert recovered.state is LifecycleState.CONFIRMATION_REQUIRED
    assert recovered.decision_id == decision.decision_id
    assert recovered.idempotency_key == "idem-001"
    found = actions.find_action_by_idempotency(target_id="project-demo", operation="update_project_plan", idempotency_key="idem-001")
    assert found is not None and found.action_id == identity.action_id


def test_confirmation_persists_and_survives_restart_until_expiry(tmp_path: Path):
    service, db1, _plans, clock = build_durable_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    binding = service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    db1.close()

    actions = SQLiteActionRepository(ControlPlaneDatabase(db_path(tmp_path)))
    stored = actions.get_confirmation(binding.confirmation_id)
    assert stored is not None
    assert stored == binding
    assert stored.state is ConfirmationState.CONFIRMED
    assert stored.action_id == binding.action_id
    assert stored.plan_digest == binding.plan_digest
    assert stored.target_revision == binding.target_revision


def test_consumed_confirmation_remains_consumed_after_restart(tmp_path: Path):
    service, db1, _plans, clock = build_durable_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    binding = service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    confirmations = OwnerConfirmationService(clock=clock, store=SQLiteActionRepository(db1))
    consumed = confirmations.consume(binding, now=NOW + timedelta(minutes=2))
    assert consumed.state is ConfirmationState.CONSUMED
    db1.close()

    repo = SQLiteActionRepository(ControlPlaneDatabase(db_path(tmp_path)))
    stored = repo.get_confirmation(binding.confirmation_id)
    assert stored is not None
    assert stored.state is ConfirmationState.CONSUMED
    confirmations2 = OwnerConfirmationService(clock=clock, store=repo)
    with pytest.raises(ControlPlaneError):
        confirmations2.consume(stored, now=NOW + timedelta(minutes=3))
    with pytest.raises(ControlPlaneError):
        confirmations2.confirm(stored, confirmed_by_subject="local-owner", now=NOW + timedelta(minutes=3))


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_service_idempotent_replay_returns_existing_decision(tmp_path: Path):
    service, _db, _plans, _clock = build_durable_service(tmp_path)
    session = service.login(SECRET)
    first = service.authorize(session.session_id, request())
    second = service.authorize(session.session_id, request())
    assert first.decision_id == second.decision_id
    total = _db.connection.execute("SELECT COUNT(*) AS c FROM actions").fetchone()["c"]
    assert total == 1


def test_same_key_different_request_is_a_deterministic_conflict(tmp_path: Path):
    service, _db, _plans, _clock = build_durable_service(tmp_path)
    session = service.login(SECRET)
    first = service.authorize(session.session_id, request())
    assert first.allowed is True
    with pytest.raises(ControlPlaneError) as error:
        service.authorize(session.session_id, request(metadata=(("title", "Different request"),)))
    assert error.value.code is PlanningErrorCode.IDEMPOTENCY_CONFLICT
    total = _db.connection.execute("SELECT COUNT(*) AS c FROM actions").fetchone()["c"]
    assert total == 1


def test_idempotent_replay_after_process_restart(tmp_path: Path):
    service, db1, _plans, _clock = build_durable_service(tmp_path)
    session1 = service.login(SECRET)
    first = service.authorize(session1.session_id, request())
    identity = first.action_identity
    assert identity is not None
    db1.close()

    service2, db2, _plans2, _clock2 = build_durable_service(tmp_path)
    session2 = service2.login(SECRET, now=NOW + timedelta(minutes=1))
    replayed = service2.authorize(session2.session_id, request(), now=NOW + timedelta(minutes=1))
    assert replayed.decision_id == first.decision_id
    assert replayed.action_identity == identity
    total = db2.connection.execute("SELECT COUNT(*) AS c FROM actions").fetchone()["c"]
    assert total == 1
    db2.close()


def test_idempotency_conflict_after_restart_for_changed_request(tmp_path: Path):
    service, db1, _plans, _clock = build_durable_service(tmp_path)
    session1 = service.login(SECRET)
    service.authorize(session1.session_id, request())
    db1.close()

    service2, _db2, _plans2, _clock2 = build_durable_service(tmp_path)
    session2 = service2.login(SECRET, now=NOW + timedelta(minutes=1))
    with pytest.raises(ControlPlaneError) as error:
        service2.authorize(session2.session_id, request(metadata=(("title", "Different"),)), now=NOW + timedelta(minutes=1))
    assert error.value.code is PlanningErrorCode.IDEMPOTENCY_CONFLICT


def test_concurrent_creation_attempts_produce_exactly_one_action(tmp_path: Path):
    db_a = ControlPlaneDatabase(db_path(tmp_path))
    db_b = ControlPlaneDatabase(db_path(tmp_path))
    repo_a = SQLiteActionRepository(db_a)
    repo_b = SQLiteActionRepository(db_b)
    from aipm.control_plane.identity import derive_action_identity
    from aipm.control_plane.models import ActionLifecycle, ActionScope

    plan = PlanOnlyPlanner(clock=lambda: NOW, target_allow_list={"project-demo"}).plan(request())
    current = make_plan()
    policy = AuthorizationPolicy(policy_version="policy-v1", allowed_scopes=frozenset({("project-demo", "staging")}))
    principal = OwnerPrincipal(
        subject="local-owner",
        issuer="aipm-owner-auth",
        authentication_method=AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE,
        verification=PrincipalVerification.VERIFIED,
        auth_epoch=1,
        authenticated_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
    )
    decision = policy.authorize(principal, request(), plan, current, now=NOW)
    identity = decision.action_identity
    assert identity is not None
    lifecycle = ActionLifecycle(
        action_id=identity.action_id,
        plan_id=identity.plan_id,
        plan_digest=identity.plan_digest,
        plan_revision=identity.target_revision,
        operation=OperationKind.UPDATE_PROJECT_PLAN,
        scope=ActionScope(target_id=identity.target_id, environment=identity.environment, policy_version=identity.policy_version),
        state=LifecycleState.CONFIRMATION_REQUIRED,
        requester_subject=identity.requester_subject,
        idempotency_key="idem-001",
        created_at=decision.decided_at,
        expires_at=decision.expires_at,
        version=2,
    )

    outcomes = {}
    barrier = threading.Barrier(2)

    def worker(name, repo, decision_value, lifecycle_value):
        barrier.wait()
        try:
            repo.register_action(decision_value, lifecycle_value)
            outcomes[name] = "ok"
        except ControlPlaneError as error:
            outcomes[name] = error.code

    thread_a = threading.Thread(target=worker, args=("a", repo_a, decision, lifecycle))
    thread_b = threading.Thread(target=worker, args=("b", repo_b, decision, lifecycle))
    thread_a.start()
    thread_b.start()
    thread_a.join()
    thread_b.join()
    assert sorted(str(value) for value in outcomes.values()) == ["ok", "ok"]
    total = db_a.connection.execute("SELECT COUNT(*) AS c FROM actions").fetchone()["c"]
    assert total == 1
    db_a.close()
    db_b.close()


# ---------------------------------------------------------------------------
# CAS / concurrency on lifecycle state
# ---------------------------------------------------------------------------


def test_cas_advance_success_stale_and_version_increment(tmp_path: Path):
    service, db, _plans, _clock = build_durable_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    actions = SQLiteActionRepository(db)
    lifecycle = actions.get_action(identity.action_id)
    assert lifecycle is not None
    advanced = actions.advance_action(
        identity.action_id,
        expected_version=lifecycle.version,
        next_state=LifecycleState.CONFIRMED,
        approver_subject="local-owner",
        now=NOW + timedelta(minutes=1),
    )
    assert advanced.version == lifecycle.version + 1
    assert advanced.state is LifecycleState.CONFIRMED
    with pytest.raises(ControlPlaneError) as error:
        actions.advance_action(
            identity.action_id,
            expected_version=lifecycle.version,
            next_state=LifecycleState.INVALIDATED,
            approver_subject="local-owner",
            now=NOW + timedelta(minutes=1),
        )
    assert error.value.code is PlanningErrorCode.STATE_CONFLICT
    reread = actions.get_action(identity.action_id)
    assert reread is not None and reread.version == advanced.version


# ---------------------------------------------------------------------------
# Kill switch persistence
# ---------------------------------------------------------------------------


def test_kill_switch_engage_and_disengage_survive_restart_for_staging(tmp_path: Path):
    clock = _Clock(NOW)
    registry = KillSwitchRegistry(clock=clock, store=SQLiteKillSwitchStore(ControlPlaneDatabase(db_path(tmp_path), clock=clock)))
    disengaged = registry.disengage(Environment.STAGING, reason="maintenance window", now=NOW)
    assert disengaged.state is KillSwitchState.DISENGAGED
    assert registry.permits(Environment.STAGING) is True

    clock.value = NOW + timedelta(minutes=5)
    reopened = KillSwitchRegistry(clock=clock, store=SQLiteKillSwitchStore(ControlPlaneDatabase(db_path(tmp_path), clock=clock)))
    assert reopened.permits(Environment.STAGING) is True
    engaged_again = reopened.engage(Environment.STAGING, reason="window closed", now=NOW + timedelta(minutes=5))
    assert engaged_again.state is KillSwitchState.ENGAGED

    clock.value = NOW + timedelta(minutes=6)
    final = KillSwitchRegistry(clock=clock, store=SQLiteKillSwitchStore(ControlPlaneDatabase(db_path(tmp_path), clock=clock)))
    assert final.permits(Environment.STAGING) is False
    assert final.switch(Environment.STAGING).epoch == 3


def test_kill_switch_production_remains_permanent_across_restart(tmp_path: Path):
    clock = _Clock(NOW)
    registry = KillSwitchRegistry(clock=clock, store=SQLiteKillSwitchStore(ControlPlaneDatabase(db_path(tmp_path), clock=clock)))
    with pytest.raises(KillSwitchError):
        registry.disengage(Environment.PRODUCTION)
    reopened = KillSwitchRegistry(clock=clock, store=SQLiteKillSwitchStore(ControlPlaneDatabase(db_path(tmp_path), clock=clock)))
    assert reopened.switch(Environment.PRODUCTION).state is KillSwitchState.PERMANENT
    assert reopened.permits(Environment.PRODUCTION) is False
    with pytest.raises(KillSwitchError):
        reopened.engage(Environment.PRODUCTION)


def test_kill_switch_unknown_and_missing_environment_fail_closed(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path))
    store = SQLiteKillSwitchStore(db)
    registry = KillSwitchRegistry(clock=_Clock(NOW), store=store)
    with pytest.raises(KillSwitchError):
        registry.permits("unknown-environment")
    with db.connection:
        db.connection.execute("DELETE FROM kill_switch_state WHERE environment = 'staging'")
    assert registry.switch(Environment.STAGING).state is KillSwitchState.ENGAGED
    assert registry.permits(Environment.STAGING) is False


def test_kill_switch_invalid_stored_state_fails_closed(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path))
    store = SQLiteKillSwitchStore(db)
    registry = KillSwitchRegistry(clock=_Clock(NOW), store=store)
    with db.connection:
        db.connection.execute("UPDATE kill_switch_state SET state = 'bogus' WHERE environment = 'staging'")
    with pytest.raises(ControlPlaneError, match="kill-switch"):
        registry.switch(Environment.STAGING)


# ---------------------------------------------------------------------------
# Crash recovery through the composition service
# ---------------------------------------------------------------------------


def test_full_flow_survives_restart_with_reauthentication(tmp_path: Path):
    service1, db1, _plans1, _clock1 = build_durable_service(tmp_path)
    session1 = service1.login(SECRET)
    decision = service1.authorize(session1.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    db1.close()

    # New process: fresh sessions, fresh service, same database.
    service2, db2, _plans2, _clock2 = build_durable_service(tmp_path)
    session2 = service2.login(SECRET, now=NOW + timedelta(minutes=1))
    binding = service2.confirm(session2.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    assert binding.state is ConfirmationState.CONFIRMED
    db2.close()

    service3, db3, _plans3, _clock3 = build_durable_service(tmp_path)
    recovered = service3.lifecycle(identity.action_id)
    assert recovered is not None
    assert recovered.state is LifecycleState.CONFIRMED
    assert recovered.approver_subject == "local-owner"
    total = db3.connection.execute("SELECT COUNT(*) AS c FROM actions").fetchone()["c"]
    assert total == 1
    db3.close()


def test_restart_replay_does_not_create_a_second_action(tmp_path: Path):
    service1, db1, _plans1, _clock1 = build_durable_service(tmp_path)
    session1 = service1.login(SECRET)
    first = service1.authorize(session1.session_id, request())
    db1.close()
    service2, db2, _plans2, _clock2 = build_durable_service(tmp_path)
    session2 = service2.login(SECRET, now=NOW + timedelta(minutes=1))
    replayed = service2.authorize(session2.session_id, request(), now=NOW + timedelta(minutes=1))
    assert replayed.decision_id == first.decision_id
    assert db2.connection.execute("SELECT COUNT(*) AS c FROM actions").fetchone()["c"] == 1
    db2.close()


# ---------------------------------------------------------------------------
# Corruption / fail-closed
# ---------------------------------------------------------------------------


def _authorize_into_db(tmp_path: Path):
    service, db, _plans, _clock = build_durable_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    return db, identity


def test_corrupt_lifecycle_state_fails_closed(tmp_path: Path):
    db, identity = _authorize_into_db(tmp_path)
    with db.connection:
        db.connection.execute("UPDATE actions SET lifecycle_state = 'running' WHERE action_id = ?", (identity.action_id,))
    repo = SQLiteActionRepository(db)
    with pytest.raises(ControlPlaneError, match="corrupt|reconstructed|Stored"):
        repo.get_action(identity.action_id)
    db.close()


def test_corrupt_identity_digest_fails_closed(tmp_path: Path):
    db, identity = _authorize_into_db(tmp_path)
    with db.connection:
        db.connection.execute("UPDATE actions SET plan_digest = 'zzzz' WHERE action_id = ?", (identity.action_id,))
    repo = SQLiteActionRepository(db)
    with pytest.raises(ControlPlaneError):
        repo.get_action(identity.action_id)
    db.close()


def test_corrupt_identity_value_fails_verification(tmp_path: Path):
    db, identity = _authorize_into_db(tmp_path)
    with db.connection:
        db.connection.execute(
            "UPDATE actions SET plan_revision = 42 WHERE action_id = ?",
            (identity.action_id,),
        )
    repo = SQLiteActionRepository(db)
    with pytest.raises(ControlPlaneError, match="verification"):
        repo.get_action(identity.action_id)
    db.close()


def test_corrupt_timestamp_fails_closed(tmp_path: Path):
    db, identity = _authorize_into_db(tmp_path)
    with db.connection:
        db.connection.execute("UPDATE actions SET created_at = 'not-a-timestamp' WHERE action_id = ?", (identity.action_id,))
    repo = SQLiteActionRepository(db)
    with pytest.raises(ControlPlaneError):
        repo.get_action(identity.action_id)
    db.close()


def test_duplicate_idempotency_rows_are_impossible(tmp_path: Path):
    db, identity = _authorize_into_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        with db.connection:
            db.connection.execute(
                "INSERT INTO actions (action_id, decision_id, idempotency_key, operation, target_id, environment, plan_id,"
                " plan_revision, plan_digest, target_digest, requester_subject, policy_version, lifecycle_state,"
                " confirmation_kind, approver_subject, created_at, updated_at, expires_at, version)"
                " SELECT hex(zeroblob(32)), decision_id, idempotency_key, operation, target_id, environment,"
                " plan_id, plan_revision, plan_digest, target_digest, requester_subject, policy_version, lifecycle_state,"
                " confirmation_kind, approver_subject, created_at, updated_at, expires_at, version"
                " FROM actions WHERE action_id = ?",
                (identity.action_id,),
            )
    db.close()


def test_service_confirm_refuses_action_missing_from_store(tmp_path: Path):
    db, identity = _authorize_into_db(tmp_path)
    decision_id = db.connection.execute("SELECT decision_id FROM authorization_decisions LIMIT 1").fetchone()["decision_id"]
    with db.connection:
        db.connection.execute("DELETE FROM actions WHERE action_id = ?", (identity.action_id,))
    service, _db2, _plans, _clock = build_durable_service(tmp_path)
    session = service.login(SECRET, now=NOW + timedelta(minutes=1))
    with pytest.raises(ControlPlaneError):
        service.confirm(session.session_id, decision_id, now=NOW + timedelta(minutes=1))
    db.close()


# ---------------------------------------------------------------------------
# Database isolation
# ---------------------------------------------------------------------------


def test_control_plane_database_is_isolated_from_telemetry_files(tmp_path: Path):
    telemetry = tmp_path / "mission_control.db"
    telemetry.write_bytes(b"telemetry-bytes")
    before = telemetry.read_bytes()
    db = ControlPlaneDatabase(db_path(tmp_path))
    plans = SQLiteProjectPlanStore(db)
    plans.create(make_plan())
    plans.update("project-demo", expected_revision=1, fields={"title": "Isolated"}, now=NOW)
    files = sorted(p.name for p in tmp_path.iterdir())
    assert "mission_control.db" in files
    assert telemetry.read_bytes() == before
    assert not (tmp_path / "mission_control.db-wal").exists()
    assert not (tmp_path / "mission_control.db-shm").exists()
    db.close()


def test_storage_never_imports_telemetry_or_mission_control_modules():
    code = (
        "import sys;"
        "import aipm.control_plane.storage as storage;"
        "forbidden = sorted(m for m in sys.modules if m.startswith(('aipm.repositories', 'aipm.services', 'aipm.dashboard', 'aipm.capabilities')));"
        "print('FORBIDDEN=' + repr(forbidden))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert "FORBIDDEN=[]" in result.stdout


def test_control_plane_source_references_only_its_own_database():
    root = Path("src/aipm/control_plane")
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "mission_control.db" not in source, path
        assert "aipm.repositories" not in source, path
        assert "aipm.services" not in source, path
        assert "aipm.dashboard" not in source, path
    storage_root = root / "storage"
    for path in storage_root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "import subprocess",
            "os.system",
            "systemctl",
            "docker.",
            "socket.",
            "requests.",
            "httpx.",
            "urllib.",
            "FROM telemetry",
            "INTO telemetry",
            "FROM mission_control",
            "INTO mission_control",
            "FROM sample_",
            "INTO sample_",
            "FROM events",
            "INTO events",
            "FROM incidents",
            "FROM notifications",
            "INTO notifications",
        ):
            assert forbidden not in source, (path, forbidden)


# ---------------------------------------------------------------------------
# Lease and snapshot scaffolding (no executor exists)
# ---------------------------------------------------------------------------


def test_execution_lease_records_round_trip_and_are_not_granted_by_anything(tmp_path: Path):
    repo = SQLiteLeaseRepository(ControlPlaneDatabase(db_path(tmp_path)))
    lease = ExecutionLease(
        lease_id="lease-001",
        action_id="a" * 64,
        environment="staging",
        fencing_token=7,
        state="granted",
        granted_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
    )
    repo.save(lease)
    reloaded = repo.get("lease-001")
    assert reloaded == lease
    assert repo.leases_for_action("a" * 64) == (lease,)
    assert repo.get("absent") is None


def test_plan_snapshots_are_immutable_history(tmp_path: Path):
    repo = SQLitePlanSnapshotRepository(ControlPlaneDatabase(db_path(tmp_path)))
    snapshot = PlanSnapshot(
        snapshot_id="snap-001",
        target_id="project-demo",
        environment="staging",
        revision=1,
        canonical_digest="a" * 64,
        payload_canonical=json.dumps({"title": "Old title"}),
        action_id=None,
        captured_at=NOW,
    )
    repo.save(snapshot)
    assert repo.get("snap-001") == snapshot
    assert repo.snapshots_for_target("project-demo") == (snapshot,)
    with pytest.raises(ControlPlaneError, match="exists"):
        repo.save(snapshot)
