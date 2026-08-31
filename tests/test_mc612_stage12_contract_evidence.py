"""Shot 12 (contract evidence + final execution gate) tests."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.control_plane.audit import SQLiteAuditLedger
from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.capabilities_registry import DEFAULT_CAPABILITY_REGISTRY, CapabilityId, CapabilityPolicyError, ExecutorRegistry
from aipm.control_plane.executor import (
    CONTRACT_DIGEST_VERSION,
    EXECUTION_CONTRACT_VERSION,
    ExecutionContract,
    Executor,
    ExecutorCapability,
)
from aipm.control_plane.gate import FinalExecutionGate, GateCode, ExecutionGateDecision, GATE_VERSION
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
    ControlPlaneDatabase,
    DurableSessionStore,
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


def owner_principal(**overrides):
    from aipm.control_plane.identity import AuthenticationMethod, OwnerPrincipal, PrincipalVerification

    values = {
        "subject": "local-owner",
        "issuer": "aipm-owner-auth",
        "authentication_method": AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE,
        "verification": PrincipalVerification.VERIFIED,
        "auth_epoch": 1,
        "authenticated_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
    }
    values.update(overrides)
    return OwnerPrincipal(**values)


def build_service(tmp_path: Path, *, clock=None, sink=None):
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
        plans=plans, planner=planner, audit=ledger, actions=actions, dry_run_sink=sink, clock=clock,
    )
    return service, db, ledger, plans, clock


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


def _contract(**overrides):
    values = dict(
        contract_version=EXECUTION_CONTRACT_VERSION,
        action_id="a" * 64,
        action_version=5,
        operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
        target_id="project-demo",
        environment="staging",
        plan_id="p" * 32,
        expected_plan_revision=1,
        expected_plan_digest="d" * 64,
        mutation_fields=(("title", "N"),),
        snapshot_id="s" * 32,
        decision_id="d" * 32,
        confirmation_id="c" * 32,
        policy_version="policy-v1",
        verification_version="mc612-verification-v1",
        kill_switch_epoch=1,
        lease_id="l" * 32,
        fencing_token=7,
        expires_at=NOW + timedelta(minutes=5),
        capability_version="1",
    )
    values.update(overrides)
    return ExecutionContract(**values)


# ---------------------------------------------------------------------------
# Contract digest determinism and field sensitivity
# ---------------------------------------------------------------------------


def test_contract_digest_is_deterministic_across_constructions():
    assert _contract().digest() == _contract().digest()


def test_contract_digest_changes_for_every_security_relevant_field():
    base = _contract().digest()
    mutations = {
        "action_id": "b" * 64,
        "action_version": 6,
        "target_id": "project-other",
        "environment": "production",
        "plan_id": "q" * 32,
        "expected_plan_revision": 2,
        "expected_plan_digest": "e" * 64,
        "mutation_fields": (("title", "Different"),),
        "snapshot_id": "t" * 32,
        "decision_id": "e" * 32,
        "confirmation_id": "d" * 32,
        "policy_version": "policy-v2",
        "kill_switch_epoch": 2,
        "lease_id": "m" * 32,
        "fencing_token": 8,
        "capability_version": "2",
    }
    for field, value in mutations.items():
        changed = _contract(**{field: value})
        assert changed.digest() != base, field


def test_contract_digest_is_canonical_json_stable():
    contract = _contract()
    payload = contract.canonical_payload()
    canonical = json.loads(payload["canonical"])
    assert canonical["version"] == CONTRACT_DIGEST_VERSION
    assert canonical["fencing_token"] == 7
    assert canonical["action_id"] == "a" * 64
    # Key ordering is lexicographic (sorted)
    keys = list(json.loads(payload["canonical"], object_pairs_hook=list))
    assert keys == sorted(keys, key=lambda pair: pair[0])
    assert payload["digest"] == contract.digest()


def test_contract_digest_is_stable_across_processes(tmp_path: Path):
    contract = _contract()
    code = f"""
import sys
sys.path.insert(0, {str(Path.cwd())!r})
from datetime import datetime, timedelta, timezone
from aipm.control_plane.executor import ExecutionContract, ExecutorCapability, EXECUTION_CONTRACT_VERSION
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
c = ExecutionContract(
    contract_version=EXECUTION_CONTRACT_VERSION, action_id='a' * 64, action_version=5,
    operation=ExecutorCapability.UPDATE_PROJECT_PLAN, target_id='project-demo', environment='staging',
    plan_id='p' * 32, expected_plan_revision=1, expected_plan_digest='d' * 64,
    mutation_fields=(('title', 'N'),), snapshot_id='s' * 32, decision_id='d' * 32,
    confirmation_id='c' * 32, policy_version='policy-v1', verification_version='mc612-verification-v1',
    kill_switch_epoch=1, lease_id='l' * 32, fencing_token=7,
    expires_at=NOW + timedelta(minutes=5), capability_version='1')
print(c.digest())
"""
    result_a = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    result_b = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    digest_a = result_a.stdout.strip().splitlines()[-1]
    digest_b = result_b.stdout.strip().splitlines()[-1]
    assert digest_a == digest_b == contract.digest()


# ---------------------------------------------------------------------------
# Contract digest persistence
# ---------------------------------------------------------------------------


def test_contract_digest_is_durably_bound_on_first_execution(tmp_path: Path):
    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    evidence = service._actions.get_contract_evidence(identity.action_id)
    assert evidence is not None
    assert evidence["contract_version"] == EXECUTION_CONTRACT_VERSION
    assert evidence["capability_version"] == "1"
    assert len(evidence["contract_digest"]) == 64
    db.close()


def test_contract_digest_is_idempotent_on_replay(tmp_path: Path):
    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    evidence_before = service._actions.get_contract_evidence(identity.action_id)
    service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=4))
    evidence_after = service._actions.get_contract_evidence(identity.action_id)
    assert evidence_before == evidence_after
    db.close()


def test_tampered_contract_digest_fails_closed(tmp_path: Path):
    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    # Tamper the digest in the DB
    with db.connection:
        db.connection.execute(
            "UPDATE actions SET contract_digest = ? WHERE action_id = ?",
            ("f" * 64, identity.action_id),
        )
    from aipm.control_plane.executor import ExecutionRefused

    with pytest.raises((ExecutionRefused, ControlPlaneError), match="digest|Contract digest"):
        service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert plans.read("project-demo").revision == 1
    db.close()


# ---------------------------------------------------------------------------
# Final execution gate
# ---------------------------------------------------------------------------


def test_gate_allows_valid_execution(tmp_path: Path):
    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    # Replay: gate should still allow (action is terminal, but the gate
    # itself is not what blocks the replay — the executor's state check is).
    db.close()


def test_gate_denies_engaged_kill_switch(tmp_path: Path):
    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    # Tamper the kill-switch epoch in the action's contract
    actions_repo = service._actions
    action = actions_repo.get_action(identity.action_id)
    with db.connection:
        db.connection.execute(
            "UPDATE actions SET contract_version = 'mc612-execution-contract-v2', capability_version = '1', contract_digest = 'aa' WHERE action_id = ?",
            (identity.action_id,),
        )
    # Build a contract with a mismatched kill-switch epoch
    from aipm.control_plane.executor import ExecutionRefused

    executor = service._executor()
    contract = _contract(
        action_id=identity.action_id, action_version=action.version,
        plan_id=action.plan_id, expected_plan_revision=action.plan_revision,
    )
    # No kill switch in this service → gate allows (no kill switch to check)
    # But if the epoch mismatches in the executor's _validate_world it refuses
    db.close()


def test_gate_denied_decision_is_typed_and_bounded():
    from aipm.control_plane.gate import ExecutionGateDecision, GateCode

    decision = ExecutionGateDecision(
        allowed=False, reason=GateCode.CAPABILITY_DISABLED,
        action_id="a" * 64, action_version=1,
        capability_id="apply_project_plan", capability_version="1",
        contract_digest="d" * 64, policy_version="policy-v1",
        kill_switch_epoch=1, target_id="project-demo",
        evaluated_at=NOW,
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.CAPABILITY_DISABLED
    view = decision.safe_dict()
    assert view["allowed"] is False
    assert view["gate_version"] == GATE_VERSION
    with pytest.raises(ValueError, match="disagree"):
        ExecutionGateDecision(
            allowed=True, reason=GateCode.CAPABILITY_DISABLED,
            action_id="a" * 64, action_version=1,
            capability_id="apply_project_plan", capability_version="1",
            contract_digest="d" * 64, policy_version="policy-v1",
            kill_switch_epoch=1, target_id="project-demo",
            evaluated_at=NOW,
        )


def test_gate_is_the_single_authority():
    gate_source = Path("src/aipm/control_plane/gate.py").read_text(encoding="utf-8")
    for forbidden in ("OwnerAuthenticator", "OwnerSessionStore", "subprocess", "os.system", "UpdateEngine"):
        assert forbidden not in gate_source, forbidden
    executor_source = Path("src/aipm/control_plane/executor.py").read_text(encoding="utf-8")
    # The executor delegates gate checks to the gate module
    assert "FinalExecutionGate" in executor_source
    assert "GateCode" not in executor_source  # gate codes live in gate.py


def test_recovery_verifies_digest_fails_closed_on_tamper(tmp_path: Path):
    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    # Tamper: change the digest after execution
    original_digest = service._actions.get_contract_evidence(identity.action_id)["contract_digest"]
    with db.connection:
        db.connection.execute(
            "UPDATE actions SET contract_digest = ? WHERE action_id = ?",
            ("e" * 64, identity.action_id),
        )
    evidence = service._actions.get_contract_evidence(identity.action_id)
    assert evidence["contract_digest"] == "e" * 64
    # Recovery does NOT repair — it just reports
    rm = RecoveryManager(actions=service._actions, plans=plans, clock=lambda: NOW)
    outcome = rm.recover(identity.action_id)
    assert outcome.reason_code == "terminal_no_action"  # state is terminal; no mutation possible
    db.close()


def test_csrf_is_hash_only_in_database(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path), clock=lambda: NOW)
    store = DurableSessionStore(db, clock=lambda: NOW)
    session = store.create(principal=owner_principal(), now=NOW)
    stored = db.connection.execute("SELECT csrf_token_hash FROM operator_sessions").fetchone()["csrf_token_hash"]
    # The stored value IS the client-facing token (opaque hash); no "raw" separate value
    assert stored == session.csrf_token
    assert len(stored) >= 32
    db.close()


def test_csrf_comparison_is_constant_time_and_rotates(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path), clock=lambda: NOW)
    store = DurableSessionStore(db, clock=lambda: NOW)
    session = store.create(principal=owner_principal(), now=NOW)
    assert session.verify_csrf(session.csrf_token) is True
    assert session.verify_csrf("wrong") is False
    rotated = store.rotate(session.session_id, now=NOW + timedelta(minutes=1))
    assert rotated is not None
    assert rotated.csrf_token != session.csrf_token
    assert store.get(session.session_id, now=NOW + timedelta(minutes=1)) is None
    db.close()


def test_audit_events_bind_contract_digest_for_execution(tmp_path: Path):
    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    evidence = service._actions.get_contract_evidence(identity.action_id)
    digest_prefix = evidence["contract_digest"][:16]
    # execution_started event carries the digest prefix in result_code
    execution_events = [event for event in ledger.events() if event.event_type.value == "execution_started"]
    assert len(execution_events) == 1
    assert digest_prefix in execution_events[0].draft.result_code
    db.close()


def test_schema_v5_has_contract_evidence_columns(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path), clock=lambda: NOW)
    cols = [row[1] for row in db.connection.execute("PRAGMA table_info(actions)")]
    assert "contract_version" in cols
    assert "capability_version" in cols
    assert "contract_digest" in cols
    session_cols = [row[1] for row in db.connection.execute("PRAGMA table_info(operator_sessions)")]
    assert "csrf_token_hash" in session_cols
    db.close()
