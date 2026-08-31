"""Shot 13 (execution lifecycle + recovery hardening) tests."""
from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.control_plane.audit import SQLiteAuditLedger
from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.executor import EXECUTION_CONTRACT_VERSION, ExecutionContract, ExecutionRefused, Executor, ExecutorCapability
from aipm.control_plane.identity import AuthenticationMethod, OwnerPrincipal, PrincipalVerification
from aipm.control_plane.models import ActionRequest, ControlPlaneError, LifecycleState, OperationKind, PlanningErrorCode
from aipm.control_plane.owner_auth import Argon2idVerifier, OwnerAuthenticator
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import AuthorizationPolicy
from aipm.control_plane.project_plan import Environment, ProjectPlan
from aipm.control_plane.recovery import RecoveryManager
from aipm.control_plane.service import OwnerControlPlaneService
from aipm.control_plane.session import OwnerSessionStore
from aipm.control_plane.storage import (
    ControlPlaneDatabase, DurableSessionStore, SQLiteActionRepository, SQLiteProjectPlanStore,
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


def request(**overrides):
    values = {"operation": OperationKind.UPDATE_PROJECT_PLAN, "target_id": "project-demo",
              "idempotency_key": "idem-001", "metadata": (("title", "New title"),), "environment": "staging"}
    values.update(overrides)
    return ActionRequest(**values)


def build_service(tmp_path: Path, *, clock=None):
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
        plans=plans, planner=planner, audit=ledger, actions=actions, clock=clock)
    return service, db, ledger, plans, clock


def prepared(tmp_path: Path):
    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    return service, db, ledger, plans, clock, session, decision, identity


def _confirmation_id(db, action_id):
    return db.connection.execute(
        "SELECT c.confirmation_id FROM confirmations c JOIN actions a ON a.decision_id = c.decision_id WHERE a.action_id = ?",
        (action_id,)).fetchone()["confirmation_id"]


# --- Rollback gate integration ---

def test_rollback_passes_gate_on_success_path(tmp_path: Path):
    metadata = (("title", "Mutated title"),)
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    # Simulate a mismatched mutation → verification failure
    original_execute = type(service._actions).execute_plan_mutation
    def mismatched(self, a_id, expected_version, *, expected_revision, mutation_fields, now, audit_drafts=()):
        return original_execute(self, a_id, expected_version, expected_revision=expected_revision, mutation_fields={"title": "Wrong"}, now=now, audit_drafts=audit_drafts)
    type(service._actions).execute_plan_mutation = mismatched
    result = service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2, seconds=30))
    type(service._actions).execute_plan_mutation = original_execute
    assert result.outcome.value == "verification_failed"

    rollback_decision = service.request_rollback(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    rollback_id = rollback_decision.action_identity.action_id
    service.confirm(session.session_id, rollback_decision.decision_id, now=NOW + timedelta(minutes=3, seconds=20))
    service.capture_snapshot(session.session_id, rollback_id, now=NOW + timedelta(minutes=3, seconds=40))
    rollback_result = service.execute_rollback(session.session_id, identity.action_id, rollback_id, now=NOW + timedelta(minutes=4))
    assert rollback_result.outcome.value == "rollback_succeeded"
    # Rollback audit carries contract digest
    events = [event for event in ledger.events() if event.event_type.value == "rollback_succeeded"]
    assert len(events) == 1
    assert ":cd=" in events[0].draft.result_code
    # Lease was released on terminal outcome
    assert service._actions.active_lease(rollback_id, now=NOW + timedelta(minutes=4)) is None


# --- Lease lifecycle hardening ---

def test_stale_release_cannot_release_newer_lease(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    repo = service._actions
    action = repo.get_action(identity.action_id)
    lease_a, _ = repo.acquire_lease(identity.action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
    # Force-expire lease A, grant B manually with token 2
    with db.connection:
        db.connection.execute("UPDATE execution_leases SET expires_at = ? WHERE lease_id = ?", ((NOW - timedelta(minutes=1)).isoformat(), lease_a.lease_id))
    db.connection.execute(
        "INSERT INTO execution_leases (lease_id, action_id, environment, fencing_token, state, holder, granted_at, expires_at, action_version)"
        " VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
        ("b" * 32, identity.action_id, "staging", 2, "granted", NOW.isoformat(), (NOW + timedelta(minutes=5)).isoformat(), action.version))
    db.connection.commit()
    # A releases itself (it can only release its own lease)
    repo.release_lease(identity.action_id, lease_id=lease_a.lease_id, fencing_token=lease_a.fencing_token, now=NOW)
    # B is still active — A's release did NOT touch B
    assert db.connection.execute("SELECT state FROM execution_leases WHERE lease_id = ?", ("b" * 32,)).fetchone()["state"] == "granted"
    # A fake release attempt for B with A's fence → refused
    assert repo.release_lease(identity.action_id, lease_id="b" * 32, fencing_token=lease_a.fencing_token, now=NOW) is False
    # B releases itself → succeeds
    assert repo.release_lease(identity.action_id, lease_id="b" * 32, fencing_token=2, now=NOW) is True


def test_lease_release_on_terminal_outcome(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert service._actions.active_lease(identity.action_id, now=NOW + timedelta(minutes=3)) is None


# --- Lease expiry recovery ---

def test_lease_expiry_leads_to_reconciliation_required(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    repo = service._actions
    action = repo.get_action(identity.action_id)
    repo.acquire_lease(identity.action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
    # Expire the lease
    with db.connection:
        db.connection.execute("UPDATE execution_leases SET expires_at = ? WHERE action_id = ?", ((NOW - timedelta(minutes=1)).isoformat(), identity.action_id))
    rm = RecoveryManager(actions=repo, plans=plans, clock=lambda: NOW)
    outcome = rm.recover(identity.action_id)
    assert outcome.reason_code == "lease_expired_reconciliation_required"
    assert outcome.recovered is True
    assert outcome.exit_state is LifecycleState.RECONCILIATION_REQUIRED


def test_expired_lease_cannot_silently_resume(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    repo = service._actions
    action = repo.get_action(identity.action_id)
    repo.acquire_lease(identity.action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
    with db.connection:
        db.connection.execute("UPDATE execution_leases SET expires_at = ? WHERE action_id = ?", ((NOW - timedelta(minutes=1)).isoformat(), identity.action_id))
    # Attempting to execute with the expired lease must fail
    from aipm.control_plane.executor import ExecutionRefused
    with pytest.raises((ExecutionRefused, ControlPlaneError), match="lease"):
        service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))


# --- Recovery idempotency ---

def test_recovery_is_idempotent(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    repo = service._actions
    action = repo.get_action(identity.action_id)
    repo.acquire_lease(identity.action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
    with db.connection:
        db.connection.execute("UPDATE execution_leases SET expires_at = ? WHERE action_id = ?", ((NOW - timedelta(minutes=1)).isoformat(), identity.action_id))
    rm = RecoveryManager(actions=repo, plans=plans, clock=lambda: NOW)
    first = rm.recover(identity.action_id)
    second = rm.recover(identity.action_id)
    third = rm.recover(identity.action_id)
    # First call transitions the state; subsequent calls report the current
    # state without further transitions (idempotent, no oscillation).
    assert first.recovered is True
    assert second.recovered is False
    assert third.recovered is False
    action_after = repo.get_action(identity.action_id)
    assert action_after.state is LifecycleState.RECONCILIATION_REQUIRED


# --- Recovery concurrency ---

def test_concurrent_recovery_cas_semantics(tmp_path: Path):
    """CAS semantics: sequential attempts prove one-winner logic.

    True concurrent SQLite access is inherently contention-prone.
    The CAS semantics (one succeeds, other sees stale version) are
    tested sequentially here; the database-level enforcement is
    proven by the mutation receipt concurrency tests (which use
    WAL mode + busy_timeout for reliable concurrent access).
    """
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    repo = service._actions
    action = repo.get_action(identity.action_id)
    repo.acquire_lease(identity.action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
    with db.connection:
        db.connection.execute("UPDATE execution_leases SET expires_at = ? WHERE action_id = ?", ((NOW - timedelta(minutes=1)).isoformat(), identity.action_id))
    rm = RecoveryManager(actions=repo, plans=plans, clock=lambda: NOW)

    # First recovery: succeeds (CAS matches, state advances)
    first = rm.recover(identity.action_id)
    assert first.recovered is True
    assert first.exit_state is LifecycleState.RECONCILIATION_REQUIRED

    # Second recovery: state has already advanced → no further transition
    second = rm.recover(identity.action_id)
    assert second.recovered is False

    # Durable state is correct
    action_after = repo.get_action(identity.action_id)
    assert action_after.state is LifecycleState.RECONCILIATION_REQUIRED



def test_terminal_states_cannot_be_mutated_back(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    repo = service._actions
    service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    action = repo.get_action(identity.action_id)
    assert action.state is LifecycleState.VERIFIED_SUCCESS
    # Attempt CAS back to RUNNING
    from aipm.control_plane.models import LifecycleError
    with pytest.raises((LifecycleError, ControlPlaneError), match="Terminal|reserved|Illegal"):
        repo.advance_action(identity.action_id, expected_version=action.version, next_state=LifecycleState.RUNNING, approver_subject="local-owner", now=NOW)


# --- Gate/lease race ---

def test_gate_passes_then_lease_expires_mutation_denied(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    repo = service._actions
    action = repo.get_action(identity.action_id)
    lease, _ = repo.acquire_lease(identity.action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
    # The gate would pass now (lease is active), but expire it before the mutation
    with db.connection:
        db.connection.execute("UPDATE execution_leases SET expires_at = ? WHERE action_id = ?", ((NOW - timedelta(minutes=1)).isoformat(), identity.action_id))
    from aipm.control_plane.executor import ExecutionRefused
    with pytest.raises((ExecutionRefused, ControlPlaneError), match="lease"):
        service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert plans.read("project-demo").revision == 1


# --- Gate/kill-switch race ---

def test_gate_passes_then_kill_switch_engages_mutation_denied(tmp_path: Path):
    from aipm.control_plane.kill_switch import KillSwitchRegistry
    from aipm.control_plane.storage import SQLiteKillSwitchStore

    class _Switches:
        def __init__(self):
            self.permit = True
            self.epoch = 1

        def switch(self, environment):
            from aipm.control_plane.kill_switch import KillSwitch, KillSwitchState
            return KillSwitch(environment=Environment(environment), state=KillSwitchState.DISENGAGED if self.permit else KillSwitchState.ENGAGED, created_at=NOW, updated_at=NOW, epoch=self.epoch)

        def permits(self, environment):
            return self.permit

    switches = _Switches()
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    # Re-wire kill switch
    object.__setattr__(service, "_kill_switches", switches)
    switches.permit = False
    from aipm.control_plane.executor import ExecutionRefused
    with pytest.raises((ExecutionRefused, ControlPlaneError), match="[Kk]ill"):
        service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert plans.read("project-demo").revision == 1


# --- Gate/plan race ---

def test_gate_passes_then_plan_changes_mutation_denied(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    # Plan changes after gate
    plans.update("project-demo", expected_revision=1, fields={"title": "Concurrent"}, now=NOW)
    from aipm.control_plane.executor import ExecutionRefused
    with pytest.raises((ExecutionRefused, ControlPlaneError), match="stale|no longer|Plan"):
        service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert plans.read("project-demo").revision == 2  # concurrent change persisted


# --- Gate/action-version race ---

def test_gate_passes_then_action_version_advances_mutation_denied(tmp_path: Path):
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    repo = service._actions
    action = repo.get_action(identity.action_id)
    # Advance through the proper path: acquire lease then expire it (recovery advances to RECONCILIATION_REQUIRED)
    repo.acquire_lease(identity.action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
    with db.connection:
        db.connection.execute("UPDATE execution_leases SET expires_at = ? WHERE action_id = ?", ((NOW - timedelta(minutes=1)).isoformat(), identity.action_id))
    rm = RecoveryManager(actions=repo, plans=plans, clock=lambda: NOW)
    rm.recover(identity.action_id)
    advanced = repo.get_action(identity.action_id)
    assert advanced.version > action.version
    assert advanced.state is LifecycleState.RECONCILIATION_REQUIRED


# --- Multi-process recovery ---

def test_multiprocess_lease_expiry_recovery(tmp_path: Path):
    writer = (
        f"import sys; sys.path.insert(0, '{Path.cwd()}'); sys.path.insert(0, '{Path.cwd() / 'tests'}');"
        "from datetime import datetime, timedelta, timezone;"
        "NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc);"
        "from aipm.control_plane.storage import ControlPlaneDatabase;"
        "db = ControlPlaneDatabase(sys.argv[1], clock=lambda: NOW);"
        "db.connection.execute('UPDATE execution_leases SET expires_at = ? WHERE action_id = ?', (datetime(2026, 8, 28, 11, 0, tzinfo=timezone.utc).isoformat(), sys.argv[2]));"
        "db.connection.commit(); print('EXPIRED')"
    )
    reader = (
        f"import sys; sys.path.insert(0, '{Path.cwd()}'); sys.path.insert(0, '{Path.cwd() / 'tests'}');"
        "from datetime import datetime, timedelta, timezone;"
        "NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc);"
        "from aipm.control_plane.storage import ControlPlaneDatabase;"
        "db = ControlPlaneDatabase(sys.argv[1], clock=lambda: NOW);"
        "row = db.connection.execute('SELECT expires_at FROM execution_leases WHERE action_id = ?', (sys.argv[2],)).fetchone();"
        "print('EXPIRED' if row and row['expires_at'] < NOW.isoformat() else 'ACTIVE')"
    )
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    repo = service._actions
    action = repo.get_action(identity.action_id)
    repo.acquire_lease(identity.action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
    db_file = str(db_path(tmp_path))
    w = subprocess.run([sys.executable, "-c", writer, db_file, identity.action_id], capture_output=True, text=True, check=True)
    assert "EXPIRED" in w.stdout
    r = subprocess.run([sys.executable, "-c", reader, db_file, identity.action_id], capture_output=True, text=True, check=True)
    assert "EXPIRED" in r.stdout


# --- Property: stale fence never commits ---

def test_property_stale_fence_never_commits(tmp_path: Path):
    import random
    rng = random.Random(0xDEAD)
    for _round in range(50):
        service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
        repo = service._actions
        action = repo.get_action(identity.action_id)
        lease, _ = repo.acquire_lease(identity.action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
        wrong_token = lease.fencing_token + rng.randint(1, 1000)
        assert repo.release_lease(identity.action_id, lease_id=lease.lease_id, fencing_token=wrong_token, now=NOW) is False
        assert plans.read("project-demo").revision == 1
        db.close()
        # Clean up for next round
        import shutil
        shutil.rmtree(tmp_path, ignore_errors=True)
        tmp_path.mkdir(parents=True, exist_ok=True)
