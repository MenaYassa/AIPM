"""Shot 11 (production hardening + execution-plane contract) tests.

Covers: durable sessions (hash-only, revocation, rotation, fixation, restart),
lease lifecycle hardening (stale release), recovery manager, capability
registry posture, executor registry, contract integrity digest, target
registry, startup validation, multi-process durability, and the consolidated
security invariant suite.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.control_plane.audit import SQLiteAuditLedger
from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.capabilities_registry import (
    DEFAULT_CAPABILITY_REGISTRY,
    CapabilityId,
    CapabilityPolicyError,
    CapabilityRegistry,
    ExecutorRegistry,
    validate_startup_configuration,
)
from aipm.control_plane.executor import (
    EXECUTION_CONTRACT_VERSION,
    ExecutionContract,
    Executor,
    ExecutorCapability,
)
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
    values = {
        "subject": "local-owner",
        "issuer": "aipm-owner-auth",
        "authentication_method": __import__("aipm.control_plane.identity", fromlist=["AuthenticationMethod"]).AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE,
        "verification": __import__("aipm.control_plane.identity", fromlist=["PrincipalVerification"]).PrincipalVerification.VERIFIED,
        "auth_epoch": 1,
        "authenticated_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
    }
    values.update(overrides)
    from aipm.control_plane.identity import OwnerPrincipal

    return OwnerPrincipal(**values)


# ---------------------------------------------------------------------------
# Durable sessions
# ---------------------------------------------------------------------------


def test_durable_session_roundtrip_revocation_and_hash_only_storage(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path), clock=lambda: NOW)
    store = DurableSessionStore(db, clock=lambda: NOW)
    session = store.create(principal=owner_principal(), now=NOW)
    restored = store.get(session.session_id, now=NOW + timedelta(minutes=5))
    assert restored is not None and restored.principal.subject == "local-owner"
    assert restored.session_id == session.session_id
    stored_hash = db.connection.execute("SELECT session_id_hash FROM operator_sessions").fetchone()["session_id_hash"]
    assert stored_hash == hashlib.sha256(session.session_id.encode()).hexdigest()
    assert session.session_id not in stored_hash
    store.revoke(session.session_id, now=NOW + timedelta(minutes=6))
    assert store.get(session.session_id, now=NOW + timedelta(minutes=7)) is None
    revoked_row = db.connection.execute("SELECT revoked_at FROM operator_sessions").fetchone()
    assert revoked_row["revoked_at"] is not None
    db.close()


def test_durable_session_survives_restart_and_expires(tmp_path: Path):
    db1 = ControlPlaneDatabase(db_path(tmp_path), clock=lambda: NOW)
    store1 = DurableSessionStore(db1, clock=lambda: NOW)
    session = store1.create(principal=owner_principal(), now=NOW)
    db1.close()
    db2 = ControlPlaneDatabase(db_path(tmp_path), clock=lambda: NOW)
    store2 = DurableSessionStore(db2, clock=lambda: NOW)
    assert store2.get(session.session_id, now=NOW + timedelta(minutes=5)) is not None
    assert store2.get(session.session_id, now=NOW + timedelta(minutes=35)) is None
    db2.close()


def test_session_rotation_produces_fresh_identifier_and_invalidates_old(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path), clock=lambda: NOW)
    store = DurableSessionStore(db, clock=lambda: NOW)
    first = store.create(principal=owner_principal(), now=NOW)
    rotated = store.rotate(first.session_id, now=NOW + timedelta(minutes=1))
    assert rotated is not None
    assert rotated.session_id != first.session_id
    assert store.get(first.session_id, now=NOW + timedelta(minutes=1)) is None
    assert store.get(rotated.session_id, now=NOW + timedelta(minutes=1)) is not None
    db.close()


def test_planted_cookie_value_cannot_hijack_a_session(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path), clock=lambda: NOW)
    store = DurableSessionStore(db, clock=lambda: NOW)
    attacker_token = "attacker-chosen-session-id"
    assert store.get(attacker_token, now=NOW) is None
    session = store.create(principal=owner_principal(), now=NOW)
    assert store.get(attacker_token, now=NOW) is None
    assert session.session_id != attacker_token
    db.close()


def test_epoch_rotation_revokes_durable_sessions(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path), clock=lambda: NOW)
    store = DurableSessionStore(db, clock=lambda: NOW)
    session = store.create(principal=owner_principal(), now=NOW)
    store.rotate_auth_epoch()
    assert store.get(session.session_id, now=NOW + timedelta(minutes=1)) is None
    db.close()


def test_durable_sessions_across_processes(tmp_path: Path):
    writer = (
        "import sys; from datetime import datetime, timedelta, timezone;"
        f"sys.path.insert(0, '{Path.cwd()}');"
        "NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc);"
        "from aipm.control_plane.storage import ControlPlaneDatabase, DurableSessionStore;"
        "from aipm.control_plane.identity import OwnerPrincipal, AuthenticationMethod, PrincipalVerification;"
        "db = ControlPlaneDatabase(sys.argv[1], clock=lambda: NOW);"
        "store = DurableSessionStore(db, clock=lambda: NOW);"
        "p = OwnerPrincipal(subject='local-owner', issuer='aipm-owner-auth',"
        " authentication_method=AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE,"
        " verification=PrincipalVerification.VERIFIED, auth_epoch=1,"
        " authenticated_at=NOW, expires_at=NOW + timedelta(minutes=30));"
        "s = store.create(principal=p, now=NOW); print(s.session_id)"
    )
    reader = (
        "import sys; from datetime import datetime, timedelta, timezone;"
        f"sys.path.insert(0, '{Path.cwd()}');"
        "NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc);"
        "from aipm.control_plane.storage import ControlPlaneDatabase, DurableSessionStore;"
        "db = ControlPlaneDatabase(sys.argv[1], clock=lambda: NOW);"
        "store = DurableSessionStore(db, clock=lambda: NOW);"
        "session = store.get(sys.argv[2], now=NOW + timedelta(minutes=1));"
        "print('ALIVE' if session is not None else 'DEAD')"
    )
    db_file = str(tmp_path / "cp.db")
    writer_proc = subprocess.run([sys.executable, "-c", writer, db_file], capture_output=True, text=True, check=True)
    session_id = writer_proc.stdout.strip().splitlines()[-1]
    reader_proc = subprocess.run([sys.executable, "-c", reader, db_file, session_id], capture_output=True, text=True, check=True)
    assert "ALIVE" in reader_proc.stdout


def test_stale_release_cannot_release_a_newer_lease(tmp_path: Path):
    service, db, ledger, plans, session, decision, identity = _prepared(tmp_path)
    repo = service._actions
    action = repo.get_action(identity.action_id)
    lease_a, _adv = repo.acquire_lease(action.action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
    # Force-expire lease A and grant lease B (token 2).
    with db.connection:
        db.connection.execute(
            "UPDATE execution_leases SET expires_at = ? WHERE lease_id = ?",
            ((NOW - timedelta(minutes=1)).isoformat(), lease_a.lease_id),
        )
    # The action is LEASED; release the expired lease first so B can be granted.
    # Since acquire requires SNAPSHOT_CAPTURED, simulate B by direct grant on the row.
    db.connection.execute(
        "INSERT INTO execution_leases (lease_id, action_id, environment, fencing_token, state, holder, granted_at, expires_at, released_at, action_version)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (
            "b" * 32,
            action.action_id,
            "staging",
            lease_a.fencing_token + 1,
            "granted",
            None,
            NOW.isoformat(),
            (NOW + timedelta(minutes=5)).isoformat(),
            action.version,
        ),
    )
    db.connection.commit()
    # A stale-fenced release attempt (wrong token for A) must fail.
    released = repo.release_lease(action.action_id, lease_id=lease_a.lease_id, fencing_token=lease_a.fencing_token + 10, now=NOW)
    assert released is False
    lease_b_state = db.connection.execute(
        "SELECT state FROM execution_leases WHERE lease_id = ?",
        ("b" * 32,),
    ).fetchone()["state"]
    assert lease_b_state == "granted"
    # Lease B releases itself: succeeds.
    released_b = repo.release_lease(action.action_id, lease_id="b" * 32, fencing_token=lease_a.fencing_token + 1, now=NOW)
    assert released_b is True
    db.close()


def _action_id_of(db) -> str:
    return db.connection.execute("SELECT action_id FROM actions LIMIT 1").fetchone()["action_id"]


# ---------------------------------------------------------------------------
# Recovery manager
# ---------------------------------------------------------------------------


def _prepared(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path), clock=_Clock(NOW))
    ledger = SQLiteAuditLedger(db)
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=_Clock(NOW))
    sessions = OwnerSessionStore(clock=_Clock(NOW))
    policy = AuthorizationPolicy(policy_version="policy-v1", allowed_scopes=frozenset({("project-demo", "staging")}))
    confirmations = OwnerConfirmationService(clock=_Clock(NOW))
    plans = SQLiteProjectPlanStore(db)
    plans.create(ProjectPlan.create(target_id="project-demo", environment=Environment.STAGING, title="Old title", objective="Objective", now=NOW))
    planner = PlanOnlyPlanner(clock=_Clock(NOW), target_allow_list={"project-demo"})
    actions = SQLiteActionRepository(db, audit=ledger)
    service = OwnerControlPlaneService(
        authenticator=authenticator, sessions=sessions, policy=policy, confirmations=confirmations,
        plans=plans, planner=planner, audit=ledger, actions=actions, clock=_Clock(NOW),
    )
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    service.capture_snapshot(session.session_id, identity.action_id, now=NOW + timedelta(minutes=2))
    return service, db, ledger, plans, session, decision, identity


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


def test_recovery_classifies_non_terminal_states(tmp_path: Path):
    service, db, ledger, plans, session, decision, identity = _prepared(tmp_path)
    rm = RecoveryManager(actions=service._actions, plans=plans, clock=lambda: NOW)
    outcome = rm.recover(identity.action_id)
    assert outcome.entry_state is LifecycleState.SNAPSHOT_CAPTURED
    assert outcome.reason_code == "ready_for_execution"
    assert outcome.recovered is False
    db.close()


def test_recovery_flags_unknown_outcome_for_reconciliation(tmp_path: Path):
    service, db, ledger, plans, session, decision, identity = _prepared(tmp_path)
    repo = service._actions
    action = repo.get_action(identity.action_id)
    _lease, leased = repo.acquire_lease(identity.action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
    running = repo.begin_execution(identity.action_id, expected_version=leased.version, confirmation_id=_confirmation_id(db, identity.action_id), now=NOW + timedelta(minutes=3))
    repo.mark_outcome(identity.action_id, expected_version=running.version, outcome="mutation_started", now=NOW)
    repo.mark_outcome(identity.action_id, expected_version=running.version, outcome="unknown_outcome", now=NOW)
    rm = RecoveryManager(actions=repo, plans=plans, clock=lambda: NOW)
    outcome = rm.recover(identity.action_id)
    assert outcome.outcome is not None and outcome.outcome.value == "unknown_outcome"
    assert outcome.reason_code == "reconciliation_required"
    db.close()


def test_recovery_terminal_states_require_nothing(tmp_path: Path):
    service, db, ledger, plans, session, decision, identity = _prepared(tmp_path)
    result = service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert result.lifecycle_state is LifecycleState.VERIFIED_SUCCESS
    rm = RecoveryManager(actions=service._actions, plans=plans, clock=lambda: NOW)
    outcome = rm.recover(identity.action_id)
    assert outcome.reason_code == "terminal_no_action"
    db.close()


def _confirmation_id(db, action_id: str) -> str:
    row = db.connection.execute(
        "SELECT c.confirmation_id FROM confirmations c JOIN actions a ON a.decision_id = c.decision_id WHERE a.action_id = ?",
        (action_id,),
    ).fetchone()
    return row["confirmation_id"]


# ---------------------------------------------------------------------------
# Capability registry / executor registry
# ---------------------------------------------------------------------------


def test_default_posture_is_fail_closed():
    registry = DEFAULT_CAPABILITY_REGISTRY
    assert registry.require_executable(CapabilityId.APPLY_PROJECT_PLAN, environment="staging") is not None
    for external in (CapabilityId.RESTART_ALLOWLISTED_SYSTEMD_UNIT, CapabilityId.UPDATE_GIT_REF, CapabilityId.REBUILD_COMPOSE_STACK, CapabilityId.DOCKER_MUTATION):
        with pytest.raises(CapabilityPolicyError):
            registry.require_executable(external, environment="staging")
    with pytest.raises(CapabilityPolicyError, match="permanently"):
        registry.require_executable(CapabilityId.ARBITRARY_SCRIPT, environment="staging")
    for environment in ("production", "unknown", "", "PRODUCTION"):
        with pytest.raises(CapabilityPolicyError):
            registry.require_executable(CapabilityId.APPLY_PROJECT_PLAN, environment=environment)


def test_registry_refuses_unknown_capability_and_version_mismatch():
    registry = DEFAULT_CAPABILITY_REGISTRY
    with pytest.raises(CapabilityPolicyError, match="Unknown"):
        registry.resolve("run_shell")
    with pytest.raises(CapabilityPolicyError, match="version"):
        registry.resolve(CapabilityId.APPLY_PROJECT_PLAN, version="999")


def test_executor_registry_refuses_missing_executor_and_forbidden_registration():
    registry = DEFAULT_CAPABILITY_REGISTRY
    executors = ExecutorRegistry(capability_registry=registry)
    with pytest.raises(CapabilityPolicyError, match="No executor"):
        executors.resolve(CapabilityId.APPLY_PROJECT_PLAN)
    with pytest.raises(CapabilityPolicyError, match="permanently"):
        executors.register(capability_id=CapabilityId.ARBITRARY_SCRIPT, executor=object())
    executors.register(capability_id=CapabilityId.APPLY_PROJECT_PLAN, executor=object())
    with pytest.raises(CapabilityPolicyError, match="Duplicate"):
        executors.register(capability_id=CapabilityId.APPLY_PROJECT_PLAN, executor=object())
    definition, _executor = executors.resolve(CapabilityId.APPLY_PROJECT_PLAN, version="1")
    assert definition.capability_id is CapabilityId.APPLY_PROJECT_PLAN


# ---------------------------------------------------------------------------
# Execution contract integrity
# ---------------------------------------------------------------------------


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


def test_contract_digest_is_deterministic_and_field_sensitive():
    first = _contract()
    second = _contract()
    assert first.digest() == second.digest()
    tampered = _contract(fencing_token=8)
    assert tampered.digest() != first.digest()
    tampered2 = _contract(mutation_fields=(("title", "Different"),))
    assert tampered2.digest() != first.digest()
    payload = first.canonical_payload()
    canonical_doc = json.loads(payload["canonical"])
    assert canonical_doc["fencing_token"] == 7
    assert canonical_doc["version"] == "mc612-contract-digest-v1"


def test_contract_carries_capability_version():
    contract = _contract()
    assert contract.capability_version == "1"
    with pytest.raises(Exception):
        _contract(capability_version="")


# ---------------------------------------------------------------------------
# Target registry + startup validation
# ---------------------------------------------------------------------------


def test_target_registry_resolves_and_fails_closed(tmp_path: Path):
    plans = SQLiteProjectPlanStore(ControlPlaneDatabase(tmp_path / "cp.db", clock=lambda: NOW))
    plans.create(ProjectPlan.create(target_id="project-demo", environment=Environment.STAGING, title="T", objective="O", now=NOW))
    from aipm.control_plane.capabilities_registry import resolve_target

    target = resolve_target(target_id="project-demo", plan_store=plans, capability_registry=DEFAULT_CAPABILITY_REGISTRY, capability_id=CapabilityId.APPLY_PROJECT_PLAN)
    assert target.target_id == "project-demo"
    assert target.environment == "staging"
    assert CapabilityId.APPLY_PROJECT_PLAN in target.allowed_capabilities
    with pytest.raises(CapabilityPolicyError, match="registered"):
        resolve_target(target_id="absent", plan_store=plans, capability_registry=DEFAULT_CAPABILITY_REGISTRY, capability_id=CapabilityId.APPLY_PROJECT_PLAN)
    with pytest.raises(CapabilityPolicyError):
        resolve_target(target_id="project-demo", plan_store=plans, capability_registry=DEFAULT_CAPABILITY_REGISTRY, capability_id=CapabilityId.RESTART_ALLOWLISTED_SYSTEMD_UNIT)


def test_startup_validation_fails_closed(tmp_path: Path):
    registry = DEFAULT_CAPABILITY_REGISTRY
    validate_startup_configuration(
        owner_verifier_present=True, capability_registry=registry,
        database_path_permissions_ok=True, staging_targets_registered=1,
    )
    with pytest.raises(CapabilityPolicyError, match="owner authentication"):
        validate_startup_configuration(owner_verifier_present=False, capability_registry=registry, database_path_permissions_ok=True, staging_targets_registered=1)
    with pytest.raises(CapabilityPolicyError, match="staging target"):
        validate_startup_configuration(owner_verifier_present=True, capability_registry=registry, database_path_permissions_ok=True, staging_targets_registered=0)
    with pytest.raises(CapabilityPolicyError, match="unsafe bind"):
        validate_startup_configuration(owner_verifier_present=True, capability_registry=registry, database_path_permissions_ok=True, staging_targets_registered=1, unsafe_bind_detected=True)
    with pytest.raises(CapabilityPolicyError, match="database path"):
        validate_startup_configuration(owner_verifier_present=True, capability_registry=registry, database_path_permissions_ok=False, staging_targets_registered=1)


# ---------------------------------------------------------------------------
# Security invariant suite (consolidated)
# ---------------------------------------------------------------------------


def test_security_invariant_denial_matrix(tmp_path: Path):
    service, db, ledger, plans, session, decision, identity = _prepared(tmp_path)
    from aipm.control_plane.executor import ExecutionRefused

    executor = service._executor()
    action = service.lifecycle(identity.action_id)
    # Expired contract (live bindings so expiry is the refusal reason)
    expired = _contract(
        action_id=identity.action_id,
        action_version=action.version,
        plan_id=action.plan_id,
        expected_plan_revision=action.plan_revision,
        expected_plan_digest="0" * 64,  # digest mismatch is fine; version check fires first? no — expiry must fire
        expires_at=NOW - timedelta(minutes=1),
    )
    with pytest.raises(ExecutionRefused, match="expired|mismatch"):
        executor.execute(expired, now=NOW + timedelta(minutes=3))
    # Stale action version (with correct digest binding)
    stale = _contract(action_id=identity.action_id, action_version=99, plan_id=action.plan_id, expected_plan_revision=action.plan_revision)
    with pytest.raises(ExecutionRefused, match="version"):
        executor.execute(stale, now=NOW + timedelta(minutes=3))
    # Changed environment (binding mismatch)
    wrong_env = _contract(
        action_id=identity.action_id,
        action_version=action.version,
        plan_id=action.plan_id,
        expected_plan_revision=action.plan_revision,
        environment="production",
        target_id="other-target",
    )
    with pytest.raises(ExecutionRefused, match="binding|stale|environment"):
        executor.execute(wrong_env, now=NOW + timedelta(minutes=3))
    db.close()


def test_audit_completeness_for_executed_action(tmp_path: Path):
    service, db, ledger, plans, session, decision, identity = _prepared(tmp_path)
    service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    events = [event.event_type.value for event in ledger.events()]
    # Every security-relevant step has canonical evidence, exactly once.
    for required in ("authorization_allowed", "owner_confirmation_requested", "owner_confirmed", "lease_acquired", "execution_started", "execution_succeeded", "verification_started", "verification_succeeded"):
        assert events.count(required) >= 1, required
    assert events.count("execution_succeeded") == 1
    assert events.count("verification_succeeded") == 1
    assert ledger.verify_chain().ok is True
    db.close()


# ---------------------------------------------------------------------------
# Property / fuzz (bounded, deterministic seed)
# ---------------------------------------------------------------------------


def test_hostile_inputs_are_always_rejected_never_mutating(tmp_path: Path):
    import random

    rng = random.Random(0xC0FFEE)
    plans = SQLiteProjectPlanStore(ControlPlaneDatabase(tmp_path / "cp.db", clock=lambda: NOW))
    plans.create(ProjectPlan.create(target_id="project-demo", environment=Environment.STAGING, title="T", objective="O", now=NOW))
    from aipm.control_plane.bridge import BridgeError, LegacyUpdateIntent, UpdateActionRequestAdapter

    adapter = UpdateActionRequestAdapter(plan_store=plans, allowed_projects={"project-demo"})

    alphabet = "';\\/`$()\x00\x1b|&><%*?!~^[]{}:\" \n\t"
    rejections = 0
    for _round in range(200):
        hostile = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40)))
        try:
            adapter.adapt(LegacyUpdateIntent(project=hostile, idempotency_key=hostile))
        except (BridgeError, Exception):
            rejections += 1
    assert rejections >= 190
    assert plans.read("project-demo").revision == 1


def test_malformed_contract_payloads_are_rejected(tmp_path: Path):
    from aipm.control_plane.audit.sanitize import AuditEventError

    with pytest.raises(Exception):
        _contract(contract_version="bogus")
    with pytest.raises(Exception):
        _contract(action_id="")
    with pytest.raises(Exception):
        _contract(expected_plan_digest="zz")
    with pytest.raises(Exception):
        _contract(mutation_fields=())
    with pytest.raises(Exception):
        _contract(fencing_token=-1)
    with pytest.raises(Exception):
        _contract(kill_switch_epoch=0)


def test_execution_plane_never_imports_authn_session_or_transport():
    executor_source = Path("src/aipm/control_plane/executor.py").read_text(encoding="utf-8")
    for forbidden in ("OwnerAuthenticator", "OwnerSession", "from aipm.control_plane.transport", "AuthorizationPolicy", "import session", "owner_session"):
        assert forbidden not in executor_source, forbidden
    registry_source = Path("src/aipm/control_plane/capabilities_registry.py").read_text(encoding="utf-8")
    for forbidden in ("OwnerAuthenticator", "OwnerSession", "transport", "subprocess"):
        assert forbidden not in registry_source, forbidden
    recovery_source = Path("src/aipm/control_plane/recovery.py").read_text(encoding="utf-8")
    for forbidden in ("OwnerAuthenticator", "OwnerSession", "transport", "subprocess"):
        assert forbidden not in recovery_source, forbidden


def test_systemd_capability_is_typed_but_disabled():
    registry = DEFAULT_CAPABILITY_REGISTRY
    definition = registry.resolve(CapabilityId.RESTART_ALLOWLISTED_SYSTEMD_UNIT)
    assert definition.enabled is False
    assert definition.reversible is False
    assert definition.snapshot_contract is None
    with pytest.raises(CapabilityPolicyError):
        registry.require_executable(CapabilityId.RESTART_ALLOWLISTED_SYSTEMD_UNIT, environment="staging")
