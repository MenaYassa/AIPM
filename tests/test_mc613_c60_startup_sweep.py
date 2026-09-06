"""MC-6.13 C6.0: startup reconciliation sweep over non-terminal actions.

Pins the contract that the startup sweep:
- enumerates non-terminal actions only (terminal states are never listed),
- routes each through the canonical RecoveryManager (no new transitions),
- applies only the existing safe transition: expired lease -> CAS advance to
  RECONCILIATION_REQUIRED,
- never resumes runtime work (RUNNING/UNKNOWN_OUTCOME observation only),
- is idempotent, bounded, deterministic, and safe under concurrency (a CAS
  race is an isolated bounded error, never a double-apply),
- imports/calls nothing from forbidden runtime/authority boundaries.
"""
from __future__ import annotations

import ast
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.control_plane.action_state import InMemoryActionRepository
from aipm.control_plane.audit import SQLiteAuditLedger
from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.identity import AuthenticationMethod, OwnerPrincipal, PrincipalVerification
from aipm.control_plane.owner_auth import Argon2idVerifier, OwnerAuthenticator
from aipm.control_plane.models import (
    ActionRequest,
    ControlPlaneError,
    LifecycleState,
    OperationKind,
    PlanningErrorCode,
)
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import AuthorizationPolicy
from aipm.control_plane.project_plan import Environment, ProjectPlan
from aipm.control_plane.recovery import RecoveryManager
from aipm.control_plane.recovery_sweep import (
    DEFAULT_SWEEP_LIMIT,
    RecoverySweepError,
    reconcile_non_terminal_actions,
)
from aipm.control_plane.service import OwnerControlPlaneService
from aipm.control_plane.session import OwnerSessionStore
from aipm.control_plane.storage import (
    ControlPlaneDatabase,
    SQLiteActionRepository,
    SQLiteProjectPlanStore,
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
    values = {
        "operation": OperationKind.UPDATE_PROJECT_PLAN,
        "target_id": "project-demo",
        "idempotency_key": "idem-001",
        "metadata": (("title", "New title"),),
        "environment": "staging",
    }
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


def expire_lease(db, action_id: str) -> None:
    with db.connection:
        db.connection.execute(
            "UPDATE execution_leases SET expires_at = ? WHERE action_id = ?",
            ((NOW - timedelta(minutes=1)).isoformat(), action_id),
        )
    db.connection.commit()


def grant_leased_state(service, db, identity, *, then_expire: bool):
    repo = service._actions
    action = repo.get_action(identity.action_id)
    lease, _ = repo.acquire_lease(identity.action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
    if then_expire:
        expire_lease(db, identity.action_id)
    return lease


# ---------------------------------------------------------------------------
# Crash-scenario matrix (each scenario = durable state left by an interrupted run)
# ---------------------------------------------------------------------------


def test_sweep_scenario_pre_lease_stale_states(tmp_path: Path):
    """REQUESTED/PLANNED/CONFIRMATION_REQUIRED -> observation only (pre-lease stale)."""
    # Build a CONFIRMATION_REQUIRED action via the real service path, but stop
    # before snapshot capture: the durable state is pre-lease.
    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    before = repo_state(db, identity.action_id)
    assert before["state"] == "confirmation_required"

    result = reconcile_non_terminal_actions(actions=service._actions, plans=plans, clock=clock)
    assert result.scanned == 1
    assert result.errors == ()
    (outcome,) = result.outcomes
    assert outcome.reason_code == "pre_lease_stale"
    assert outcome.recovered is False
    assert outcome.exit_state is None
    assert result.advanced_action_ids == ()
    # No state change at all
    after = repo_state(db, identity.action_id)
    assert after == before


def test_sweep_scenario_snapshot_captured_ready_for_execution(tmp_path: Path):
    """Crash after snapshot capture, before lease: SNAPSHOT_CAPTURED -> ready_for_execution."""
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    before = repo_state(db, identity.action_id)
    assert before["state"] == "snapshot_captured"

    result = reconcile_non_terminal_actions(actions=service._actions, plans=plans, clock=clock)
    (outcome,) = result.outcomes
    assert outcome.reason_code == "ready_for_execution"
    assert outcome.recovered is False
    assert result.advanced_action_ids == ()
    assert repo_state(db, identity.action_id) == before


def test_sweep_scenario_leased_with_active_lease(tmp_path: Path):
    """Crash after lease grant while lease is still valid: LEASED -> ready (no mutation)."""
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    grant_leased_state(service, db, identity, then_expire=False)
    before = repo_state(db, identity.action_id)
    assert before["state"] == "leased"

    result = reconcile_non_terminal_actions(actions=service._actions, plans=plans, clock=clock)
    (outcome,) = result.outcomes
    assert outcome.reason_code == "lease_active_ready"
    assert outcome.recovered is False
    assert result.advanced_action_ids == ()
    assert repo_state(db, identity.action_id) == before


def test_sweep_scenario_expired_lease_advances_to_reconciliation(tmp_path: Path):
    """Crash after lease grant, lease expired: the ONLY mutating transition (CAS advance)."""
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    grant_leased_state(service, db, identity, then_expire=True)
    assert repo_state(db, identity.action_id)["state"] == "leased"
    leased_version = repo_state(db, identity.action_id)["version"]

    result = reconcile_non_terminal_actions(actions=service._actions, plans=plans, clock=clock)
    assert result.scanned == 1
    assert result.errors == ()
    (outcome,) = result.outcomes
    assert outcome.reason_code == "lease_expired_reconciliation_required"
    assert outcome.recovered is True
    assert outcome.exit_state is LifecycleState.RECONCILIATION_REQUIRED
    assert result.advanced_action_ids == (identity.action_id,)
    after = repo_state(db, identity.action_id)
    assert after["state"] == "reconciliation_required"
    assert after["version"] > leased_version


def test_sweep_scenario_running_never_resumed(tmp_path: Path):
    """Crash mid-execution: RUNNING is observation only; the sweep never resumes it."""
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    grant_leased_state(service, db, identity, then_expire=False)
    # Advance LEASED -> RUNNING via the real composite (consumes confirmation).
    confirmation_id = _confirmation_id(db, identity.action_id)
    service._actions.begin_execution(
        identity.action_id,
        expected_version=before_version(db, identity.action_id, "leased"),
        confirmation_id=confirmation_id,
        now=NOW + timedelta(minutes=4),
    )
    before = repo_state(db, identity.action_id)
    assert before["state"] == "running"

    result = reconcile_non_terminal_actions(actions=service._actions, plans=plans, clock=clock)
    (outcome,) = result.outcomes
    assert outcome.recovered is False
    assert outcome.outcome is not None and outcome.outcome.value == "unknown_outcome"
    assert outcome.reason_code in {"reconciliation_required", "outcome_marker_missing"}
    assert result.advanced_action_ids == ()
    assert repo_state(db, identity.action_id) == before
    # Runtime work was NOT resumed: plan revision unchanged.
    assert plans.read("project-demo").revision == 1


def test_sweep_scenario_unknown_outcome_observation_only(tmp_path: Path):
    """RUNNING + UNKNOWN_OUTCOME marker: reconcile-by-observation classification only."""
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    grant_leased_state(service, db, identity, then_expire=False)
    confirmation_id = _confirmation_id(db, identity.action_id)
    advanced = service._actions.begin_execution(
        identity.action_id,
        expected_version=before_version(db, identity.action_id, "leased"),
        confirmation_id=confirmation_id,
        now=NOW + timedelta(minutes=4),
    )
    service._actions.mark_outcome(
        identity.action_id,
        expected_version=advanced.version,
        outcome="unknown_outcome",
        now=NOW + timedelta(minutes=4, seconds=10),
    )
    before = repo_state(db, identity.action_id)

    result = reconcile_non_terminal_actions(actions=service._actions, plans=plans, clock=clock)
    (outcome,) = result.outcomes
    assert outcome.reason_code == "reconciliation_required"
    assert outcome.outcome is not None and outcome.outcome.value == "unknown_outcome"
    assert outcome.recovered is False
    assert result.advanced_action_ids == ()
    assert repo_state(db, identity.action_id) == before
    assert plans.read("project-demo").revision == 1


def test_sweep_scenario_executed_pending_verification(tmp_path: Path):
    """Crash between mutation and verification: EXECUTED_PENDING_VERIFICATION -> verification_resumable."""
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    grant_leased_state(service, db, identity, then_expire=False)
    confirmation_id = _confirmation_id(db, identity.action_id)
    advanced = service._actions.begin_execution(
        identity.action_id,
        expected_version=before_version(db, identity.action_id, "leased"),
        confirmation_id=confirmation_id,
        now=NOW + timedelta(minutes=4),
    )
    service._actions.execute_plan_mutation(
        identity.action_id,
        expected_version=advanced.version,
        expected_revision=1,
        mutation_fields={"title": "New title"},
        now=NOW + timedelta(minutes=4, seconds=30),
    )
    before = repo_state(db, identity.action_id)
    assert before["state"] == "executed_pending_verification"

    result = reconcile_non_terminal_actions(actions=service._actions, plans=plans, clock=clock)
    (outcome,) = result.outcomes
    assert outcome.reason_code == "verification_resumable"
    assert outcome.recovered is False
    assert result.advanced_action_ids == ()
    assert repo_state(db, identity.action_id) == before


def test_sweep_scenario_terminal_states_excluded(tmp_path: Path):
    """Terminal actions are never enumerated; sweep over terminal-only DB scans nothing."""
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    result = service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert result.lifecycle_state is LifecycleState.VERIFIED_SUCCESS

    sweep = reconcile_non_terminal_actions(actions=service._actions, plans=plans, clock=clock)
    assert sweep.scanned == 0
    assert sweep.outcomes == ()
    assert sweep.advanced_action_ids == ()


def test_sweep_empty_store_is_noop(tmp_path: Path):
    service, db, ledger, plans, clock = build_service(tmp_path)
    result = reconcile_non_terminal_actions(actions=service._actions, plans=plans, clock=clock)
    assert result.scanned == 0
    assert result.outcomes == ()
    assert result.errors == ()
    assert result.advanced_action_ids == ()


# ---------------------------------------------------------------------------
# Idempotency / determinism / bounds
# ---------------------------------------------------------------------------


def test_sweep_is_idempotent_over_unchanged_state(tmp_path: Path):
    """Two sweeps back to back: same observations, no double-apply."""
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    grant_leased_state(service, db, identity, then_expire=True)

    first = reconcile_non_terminal_actions(actions=service._actions, plans=plans, clock=clock)
    assert first.advanced_action_ids == (identity.action_id,)
    second = reconcile_non_terminal_actions(actions=service._actions, plans=plans, clock=clock)
    assert second.advanced_action_ids == ()
    assert [o.safe_dict() for o in first.outcomes] != [] and [o.safe_dict() for o in second.outcomes] != []
    assert second.outcomes[0].entry_state is LifecycleState.RECONCILIATION_REQUIRED
    assert second.outcomes[0].reason_code == "reconciliation_required"
    assert second.outcomes[0].recovered is False
    # Durable state advanced exactly once.
    assert repo_state(db, identity.action_id)["state"] == "reconciliation_required"


def test_sweep_result_is_bounded_by_limit(tmp_path: Path):
    """limit=N enumerates at most N non-terminal actions, deterministically ordered."""
    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    ids = []
    for i in range(5):
        decision = service.authorize(session.session_id, request(idempotency_key=f"idem-{i:03d}"))
        ids.append(decision.action_identity.action_id)
    repo = service._actions
    all_ids = repo.non_terminal_action_ids(limit=100)
    assert len(all_ids) == 5

    bounded = reconcile_non_terminal_actions(actions=repo, plans=plans, clock=clock, limit=3)
    assert bounded.scanned == 3
    assert len(bounded.outcomes) == 3
    # Deterministic order: (created_at, action_id) prefix.
    expected = sorted(ids)[:3] if len(set(ids)) == 5 else None
    listed = repo.non_terminal_action_ids(limit=3)
    assert listed == all_ids[:3]
    # stable across calls
    assert repo.non_terminal_action_ids(limit=100) == all_ids


def test_sweep_default_limit_constant():
    assert DEFAULT_SWEEP_LIMIT == 1000


def test_sweep_rejects_invalid_inputs(tmp_path: Path):
    service, db, ledger, plans, clock = build_service(tmp_path)
    with pytest.raises(RecoverySweepError):
        reconcile_non_terminal_actions(actions=service._actions, plans=plans, clock=clock, limit=0)
    with pytest.raises(RecoverySweepError):
        reconcile_non_terminal_actions(actions=service._actions, plans=plans, clock=clock, limit=-5)
    # Repository without the enumeration capability is refused outright.
    with pytest.raises(RecoverySweepError):
        reconcile_non_terminal_actions(actions=object(), plans=plans, clock=clock)


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_sweep_isolates_per_action_failure(tmp_path: Path):
    """A poisoned action fails in isolation; the rest of the sweep completes."""
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    healthy_id = identity.action_id
    # Second action whose row will vanish after enumeration (get_action -> None).
    decision2 = service.authorize(session.session_id, request(idempotency_key="idem-poison"))
    poisoned_id = decision2.action_identity.action_id

    repo = service._actions
    enumerated = repo.non_terminal_action_ids(limit=100)
    assert poisoned_id in enumerated and healthy_id in enumerated

    class _VanishingRepo:
        """Enumerates both, but get_action fails for the poisoned id."""

        def __init__(self, inner) -> None:
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def get_action(self, action_id):
            if action_id == poisoned_id:
                raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Poisoned row")
            return self._inner.get_action(action_id)

    result = reconcile_non_terminal_actions(actions=_VanishingRepo(repo), plans=plans, clock=clock)
    assert result.scanned == 2
    reasons = {e.action_id: e.reason for e in result.errors}
    assert set(reasons) == {poisoned_id}
    assert "Poisoned row" in reasons[poisoned_id]
    assert len(result.outcomes) == 1
    assert result.outcomes[0].action_id == healthy_id
    assert result.outcomes[0].reason_code == "ready_for_execution"


def test_sweep_enumeration_failure_fails_closed(tmp_path: Path):
    """Enumeration failure propagates (fail closed); nothing is recovered."""
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)

    class _BrokenEnumeration:
        def __getattr__(self, name):
            return getattr(service._actions, name)

        def non_terminal_action_ids(self, *, limit):
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Enumeration unavailable")

    with pytest.raises(ControlPlaneError):
        reconcile_non_terminal_actions(actions=_BrokenEnumeration(), plans=plans, clock=clock)
    # Durable state untouched by the failed sweep.
    assert repo_state(db, identity.action_id)["state"] == "snapshot_captured"


# ---------------------------------------------------------------------------
# CAS race under concurrency
# ---------------------------------------------------------------------------


def test_sweep_cas_race_single_winner(tmp_path: Path):
    """Two sweeps race on one expired lease: exactly one winner, loser sees isolated error."""
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    grant_leased_state(service, db, identity, then_expire=True)

    # Stale-read proxy: both sweeps read the pre-advance version (v5) but the
    # real advance_action performs the durable CAS, so exactly one can win.
    class _StaleReadRepo:
        def __init__(self, inner, version_snapshot) -> None:
            self._inner = inner
            self._version_snapshot = version_snapshot

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def get_action(self, action_id):
            from dataclasses import replace

            current = self._inner.get_action(action_id)
            return replace(current, version=self._version_snapshot)

    stale_version = before_version(db, identity.action_id, "leased")
    db_file = str(db_path(tmp_path))
    results: list = []
    failures: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def runner():
        try:
            # Each sweep owns an independent connection to the same durable
            # file: the database, not shared python state, serializes them.
            thread_db = ControlPlaneDatabase(db_file, clock=clock)
            thread_repo = SQLiteActionRepository(thread_db)
            barrier.wait()
            outcome = reconcile_non_terminal_actions(actions=_StaleReadRepo(thread_repo, stale_version), plans=plans, clock=clock)
            with lock:
                results.append(outcome)
        except Exception as exc:  # surfaced below; never silently swallowed
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=runner) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert failures == [], f"sweep threads raised: {failures!r}"
    assert len(results) == 2
    winners = [r for r in results if r.advanced_action_ids == (identity.action_id,)]
    losers = [r for r in results if r.advanced_action_ids == ()]
    assert len(winners) == 1
    assert len(losers) == 1
    (loser,) = losers
    # A loser is safe in exactly one of two modes:
    # (a) it read the pre-commit LEASED row and its CAS/transition attempt was
    #     rejected as a typed, isolated conflict; or
    # (b) it read the post-commit RECONCILIATION_REQUIRED row and observed it
    #     without error (no second advance attempted, none possible).
    # Either way there is never a double-apply.
    if loser.errors:
        assert len(loser.errors) == 1
        assert loser.errors[0].action_id == identity.action_id
        assert ("Stale action version" in loser.errors[0].reason) or (
            "Illegal lifecycle transition" in loser.errors[0].reason
        )
    else:
        assert len(loser.outcomes) == 1
        assert loser.outcomes[0].entry_state is LifecycleState.RECONCILIATION_REQUIRED
        assert loser.outcomes[0].recovered is False
    # Durable truth: advanced exactly once, no corruption.
    after = repo_state(db, identity.action_id)
    assert after["state"] == "reconciliation_required"
    # A post-race sweep is clean and observational.
    final = reconcile_non_terminal_actions(actions=service._actions, plans=plans, clock=clock)
    assert final.errors == ()
    assert final.advanced_action_ids == ()
    assert final.outcomes[0].reason_code == "reconciliation_required"


# ---------------------------------------------------------------------------
# Real-SQLite cross-process crash recovery
# ---------------------------------------------------------------------------


def test_sweep_cross_process_after_crash(tmp_path: Path):
    """Process A creates durable LEASED state and exits; process B (fresh stores,
    no shared memory) runs the sweep and applies the expired-lease transition."""
    service, db, ledger, plans, clock, session, decision, identity = prepared(tmp_path)
    grant_leased_state(service, db, identity, then_expire=True)
    db_file = str(db_path(tmp_path))

    sweeper = (
        f"import sys; sys.path.insert(0, '{Path.cwd()}');"
        "from datetime import datetime, timezone;"
        "NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc);"
        "from aipm.control_plane.storage import ControlPlaneDatabase, SQLiteActionRepository, SQLiteProjectPlanStore;"
        "from aipm.control_plane.recovery_sweep import reconcile_non_terminal_actions;"
        "db = ControlPlaneDatabase(sys.argv[1], clock=lambda: NOW);"
        "actions = SQLiteActionRepository(db);"
        "plans = SQLiteProjectPlanStore(db);"
        "result = reconcile_non_terminal_actions(actions=actions, plans=plans, clock=lambda: NOW);"
        "print('ADVANCED=' + ','.join(result.advanced_action_ids));"
        "print('SCANNED=' + str(result.scanned));"
        "print('ERRORS=' + str(len(result.errors)));"
        "row = db.connection.execute('SELECT lifecycle_state FROM actions WHERE action_id = ?', (sys.argv[2],)).fetchone();"
        "print('STATE=' + row['lifecycle_state'])"
    )
    proc = subprocess.run(
        [sys.executable, "-c", sweeper, db_file, identity.action_id],
        capture_output=True, text=True, check=True, timeout=120,
    )
    assert "SCANNED=1" in proc.stdout
    assert "ERRORS=0" in proc.stdout
    assert f"ADVANCED={identity.action_id}" in proc.stdout
    assert "STATE=reconciliation_required" in proc.stdout


# ---------------------------------------------------------------------------
# In-memory repository parity
# ---------------------------------------------------------------------------


def test_in_memory_repo_non_terminal_enumeration_matches_sqlite_semantics():
    """The in-memory double exposes the same bounded, deterministic, terminal-free
    enumeration contract as the durable store."""
    from tests.test_mc612_action_state import decision_and_lifecycle

    repo = InMemoryActionRepository()
    ids = []
    for i in range(3):
        decision, pending = decision_and_lifecycle(idempotency_key=f"idem-{i:03d}")
        repo.register_action(decision, pending)
        ids.append(decision.action_identity.action_id)
    assert len(set(ids)) == 3

    listed = repo.non_terminal_action_ids(limit=10)
    assert len(listed) == 3
    assert set(listed) == set(ids)
    # Bounded + deterministic
    assert repo.non_terminal_action_ids(limit=2) == listed[:2]
    assert repo.non_terminal_action_ids(limit=10) == listed
    with pytest.raises(ControlPlaneError):
        repo.non_terminal_action_ids(limit=0)


def test_in_memory_repo_active_lease_honours_now_kwarg():
    """Regression: RecoveryManager passes now= to active_lease; the in-memory
    double must accept it (latent TypeError on the LEASED recovery path)."""
    from dataclasses import replace

    from aipm.control_plane.storage.sqlite_store import ExecutionLease, DEFAULT_LEASE_TTL

    repo = InMemoryActionRepository()
    decision, pending = decision_and_lifecycle_for_lease()
    repo.register_action(decision, pending)
    action_id = decision.action_identity.action_id
    repo._leases = getattr(repo, "_leases", {})
    repo._leases[action_id] = ExecutionLease(
        lease_id="a" * 32,
        action_id=action_id,
        environment="staging",
        fencing_token=1,
        state="granted",
        granted_at=NOW,
        expires_at=NOW + DEFAULT_LEASE_TTL,
        action_version=pending.version,
    )
    # now= accepted and honoured for expiry evaluation.
    assert repo.active_lease(action_id, now=NOW + timedelta(minutes=1)) is not None
    assert repo.active_lease(action_id, now=NOW + DEFAULT_LEASE_TTL + timedelta(seconds=1)) is None
    # Default (wall-clock) path: fixture lease is long expired against the
    # real clock, proving the default path evaluates expiry honestly.
    assert repo.active_lease(action_id) is None


def decision_and_lifecycle_for_lease():
    """CONFIRMATION_REQUIRED action (pre-lease states are enough for active_lease)."""
    from tests.test_mc612_action_state import decision_and_lifecycle

    return decision_and_lifecycle()


# ---------------------------------------------------------------------------
# Source-boundary AST tests: the sweep stays observational by construction
# ---------------------------------------------------------------------------

_FORBIDDEN_MODULES = (
    "aipm.services.update.engine",
    "aipm.control_plane.executor",
    "aipm.control_plane.executor_ipc",
    "aipm.control_plane.transport",
    "aipm.control_plane.gate",
    "aipm.control_plane.dashboard",
)
_FORBIDDEN_CALL_NAMES = {
    "acquire_lease", "begin_execution", "execute_plan_mutation", "record_verification_outcome",
    "advance_rollback_state", "release_lease", "mark_reconciled", "consume_confirmation",
    "Popen", "system", "run",
}


def _sweep_module_ast():
    import aipm.control_plane.recovery_sweep as sweep_module

    source = ast.get_source_segment.__self__ if False else None
    path = Path(sweep_module.__file__)
    return ast.parse(path.read_text())


def test_sweep_module_imports_stay_within_recovery_boundary():
    tree = _sweep_module_ast()
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden_hits = sorted(imported & set(_FORBIDDEN_MODULES))
    assert forbidden_hits == []
    # No transport/dashboard/HTTP surface either.
    assert not any(name.startswith(("aiohttp", "flask", "fastapi", "requests", "httpx", "urllib")) for name in imported)


def test_sweep_module_makes_no_forbidden_calls():
    tree = _sweep_module_ast()
    calls = [node.func for node in ast.walk(tree) if isinstance(node, ast.Call)]
    names = set()
    for func in calls:
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
    forbidden = sorted(names & _FORBIDDEN_CALL_NAMES)
    assert forbidden == []


# ---------------------------------------------------------------------------
# Shared helpers (defined last only because modules above use them; Python
# resolves at call time, not import time)
# ---------------------------------------------------------------------------


def repo_state(db, action_id: str) -> dict:
    row = db.connection.execute(
        "SELECT lifecycle_state, version FROM actions WHERE action_id = ?", (action_id,)
    ).fetchone()
    return {"state": row["lifecycle_state"], "version": row["version"]}


def before_version(db, action_id: str, state: str) -> int:
    return repo_state(db, action_id)["version"]


def _confirmation_id(db, action_id: str) -> str:
    return db.connection.execute(
        "SELECT c.confirmation_id FROM confirmations c JOIN actions a ON a.decision_id = c.decision_id WHERE a.action_id = ?",
        (action_id,),
    ).fetchone()["confirmation_id"]
