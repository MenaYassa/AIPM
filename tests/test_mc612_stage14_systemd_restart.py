"""Shot 14 (first real external capability: systemd restart) tests.

All tests use a fake/mocked subprocess runner. No real systemctl is invoked.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.control_plane.audit import SQLiteAuditLedger
from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.capabilities_registry import DEFAULT_CAPABILITY_REGISTRY, CapabilityId, CapabilityPolicyError, CapabilityRegistry
from aipm.control_plane.executor import EXECUTION_CONTRACT_VERSION, ExecutionContract, ExecutionRefused, ExecutorCapability
from aipm.control_plane.identity import AuthenticationMethod, OwnerPrincipal, PrincipalVerification
from aipm.control_plane.models import ActionRequest, LifecycleState, OperationKind
from aipm.control_plane.owner_auth import Argon2idVerifier, OwnerAuthenticator
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import AuthorizationPolicy
from aipm.control_plane.project_plan import Environment, ProjectPlan
from aipm.control_plane.service import OwnerControlPlaneService
from aipm.control_plane.session import OwnerSessionStore
from aipm.control_plane.storage import ControlPlaneDatabase, SQLiteActionRepository, SQLiteProjectPlanStore
from aipm.control_plane.systemd_provider import (
    SystemdRestartPolicy,
    SystemdRestartProvider,
    SystemdRestartResult,
    SubprocessResult,
    SystemdRestartError,
)
from aipm.control_plane.systemd_executor import SystemdRestartExecutor

VERIFIER = "$argon2id$v=19$m=65536,t=2,p=1$c3RhZ2UzLXNhbHQtMTIzNA$zho28DBNr2G2cGbxzr0Dl6AKwhbd8hEeTkti1pn7TW0"
SECRET = "test-owner-secret"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

POLICY = SystemdRestartPolicy(
    environment="staging", target_id="aipm-telemetry", unit_id="aipm-telemetry",
    canonical_unit_name="aipm-telemetry.service", policy_version="policy-v1",
)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value


class FakeRunner:
    """Fake subprocess runner; records calls without real execution."""

    def __init__(self, *, restart_returncode: int = 0, restart_times_out: bool = False):
        self.calls: list[list[str]] = []
        self.restart_returncode = restart_returncode
        self.restart_times_out = restart_times_out

    def __call__(self, argv, *, timeout=30):
        self.calls.append(list(argv))
        if argv[1] == "show":
            return SubprocessResult(0, "LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\nMainPID=1234\nFragmentPath=/etc/systemd/system/aipm-telemetry.service\n", "", False)
        if argv[1] == "restart":
            if self.restart_times_out:
                return SubprocessResult(-1, "", "timeout", True)
            return SubprocessResult(self.restart_returncode, "", "" if self.restart_returncode == 0 else "fake error", False)
        return SubprocessResult(1, "", f"unknown argv: {argv}", False)


def request(**overrides):
    values = {"operation": OperationKind.UPDATE_PROJECT_PLAN, "target_id": "aipm-telemetry",
              "idempotency_key": "idem-001", "metadata": (("objective", "systemd restart"),), "environment": "staging"}
    values.update(overrides)
    return ActionRequest(**values)


def db_path(tmp_path: Path) -> Path:
    return tmp_path / "control_plane.db"


def _confirmation_id(db, action_id: str) -> str:
    row = db.connection.execute(
        "SELECT c.confirmation_id FROM confirmations c JOIN actions a ON a.decision_id = c.decision_id WHERE a.action_id = ?",
        (action_id,)).fetchone()
    return row["confirmation_id"]


def _login(service):
    return service.login(SECRET)


def build_full(tmp_path: Path, *, restart_returncode: int = 0, restart_times_out: bool = False):
    clock = _Clock(NOW)
    db = ControlPlaneDatabase(db_path(tmp_path), clock=clock)
    ledger = SQLiteAuditLedger(db)
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=clock)
    sessions = OwnerSessionStore(clock=clock)
    policy = AuthorizationPolicy(policy_version="policy-v1", allowed_scopes=frozenset({("aipm-telemetry", "staging")}))
    confirmations = OwnerConfirmationService(clock=clock)
    plans = SQLiteProjectPlanStore(db)
    plans.create(ProjectPlan.create(target_id="aipm-telemetry", environment=Environment.STAGING, title="Telemetry Sampler", objective="Bounded systemd restart target", now=NOW))
    planner = PlanOnlyPlanner(clock=clock, target_allow_list={"aipm-telemetry"})
    actions = SQLiteActionRepository(db, audit=ledger)
    service = OwnerControlPlaneService(
        authenticator=authenticator, sessions=sessions, policy=policy, confirmations=confirmations,
        plans=plans, planner=planner, audit=ledger, actions=actions, clock=clock)
    runner = FakeRunner(restart_returncode=restart_returncode, restart_times_out=restart_times_out)
    provider = SystemdRestartProvider(policies=[POLICY], runner=runner)
    executor = SystemdRestartExecutor(
        actions=actions, plans=plans, confirmations=confirmations, kill_switches=None,
        audit=ledger, snapshots=None, provider=provider, policy=POLICY)
    session = service.login(SECRET)
    return service, db, ledger, plans, clock, session, actions, runner, executor


def _authorize_and_prepare(service, session, *, target_id="aipm-telemetry"):
    decision = service.authorize(session.session_id, request(target_id=target_id))
    identity = decision.action_identity
    assert identity is not None
    service.confirm_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=1))
    return decision, identity


# ---------------------------------------------------------------------------
# Provider: allow-list, argv construction, bounded subprocess
# ---------------------------------------------------------------------------


def test_provider_resolves_allow_listed_unit():
    runner = FakeRunner()
    provider = SystemdRestartProvider(policies=[POLICY], runner=runner)
    policy = provider.resolve_unit("aipm-telemetry", environment="staging")
    assert policy.canonical_unit_name == "aipm-telemetry.service"


def test_provider_refuses_unknown_unit():
    runner = FakeRunner()
    provider = SystemdRestartProvider(policies=[POLICY], runner=runner)
    with pytest.raises(SystemdRestartError, match="not allow-listed"):
        provider.resolve_unit("unknown-unit", environment="staging")


def test_provider_refuses_wrong_environment():
    runner = FakeRunner()
    provider = SystemdRestartProvider(policies=[POLICY], runner=runner)
    with pytest.raises(SystemdRestartError, match="environment"):
        provider.resolve_unit("aipm-telemetry", environment="production")


def test_provider_argv_is_structured_no_shell():
    runner = FakeRunner()
    provider = SystemdRestartProvider(policies=[POLICY], runner=runner)
    provider.restart(POLICY)
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[0] == "/usr/bin/systemctl"
    assert argv[1] == "restart"
    assert argv[2] == "aipm-telemetry.service"
    assert len(argv) == 3  # exactly three args, no extras


def test_provider_observation_is_read_only():
    runner = FakeRunner()
    provider = SystemdRestartProvider(policies=[POLICY], runner=runner)
    snapshot = provider.observe_unit(POLICY)
    assert snapshot.active_state == "active"
    assert snapshot.load_state == "loaded"
    assert snapshot.main_pid == "1234"
    # Observation used "show" not "restart"
    assert runner.calls[0][1] == "show"


def test_provider_refuses_path_traversal_in_unit_name():
    with pytest.raises(SystemdRestartError):
        SystemdRestartPolicy(
            environment="staging", target_id="x", unit_id="x",
            canonical_unit_name="../../../bin/sh", policy_version="v",
        )
    with pytest.raises(SystemdRestartError, match="path separators"):
        SystemdRestartPolicy(
            environment="staging", target_id="x", unit_id="x",
            canonical_unit_name="sub/aipm.service", policy_version="v",
        )


def test_provider_refuses_non_service_unit_name():
    with pytest.raises(SystemdRestartError, match=".service"):
        SystemdRestartPolicy(
            environment="staging", target_id="x", unit_id="x",
            canonical_unit_name="not-a-service", policy_version="v",
        )


# ---------------------------------------------------------------------------
# Executor: contract + gate + race checks
# ---------------------------------------------------------------------------


def _make_contract(action_id: str, action_version: int = 1, *, environment: str = "staging", **overrides):
    values = dict(
        contract_version=EXECUTION_CONTRACT_VERSION, action_id=action_id,
        action_version=action_version, operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
        target_id="aipm-telemetry", environment=environment,
        plan_id="p" * 32, expected_plan_revision=1, expected_plan_digest="d" * 64,
        mutation_fields=(("objective", "systemd restart"),), snapshot_id="s" * 32,
        decision_id="d" * 32, confirmation_id="c" * 32, policy_version="policy-v1",
        verification_version="mc612-verification-v1", kill_switch_epoch=1,
        lease_id="l" * 32, fencing_token=1,
        expires_at=NOW + timedelta(minutes=30), capability_version="1",
    )
    values.update(overrides)
    return ExecutionContract(**values)


def _insert_lease(db, action_id: str, action_version: int, *, token: int = 1):
    import secrets
    lease_id = secrets.token_hex(16)
    db.connection.execute(
        "INSERT INTO execution_leases (lease_id, action_id, environment, fencing_token, state, holder, granted_at, expires_at, action_version)"
        " VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
        (lease_id, action_id, "staging", token, "granted", NOW.isoformat(),
         (NOW + timedelta(minutes=5)).isoformat(), action_version))
    db.connection.commit()
    return lease_id, token


def _real_contract(service, db, identity, *, action_version=None, environment="staging", lease_id=None, fencing_token=1, **overrides):
    """Build a contract from real durable bindings."""
    from dataclasses import replace as dc_replace
    action = service._actions.get_action(identity.action_id)
    decision = service._actions.get_decision(action.decision_id) if action.decision_id else None
    confirmation_id = _confirmation_id(db, identity.action_id)
    plan = service._plans.read("aipm-telemetry")
    base = _make_contract(
        identity.action_id, action_version=action_version or action.version,
        environment=environment,
        plan_id=identity.plan_id,
        expected_plan_revision=plan.revision,
        expected_plan_digest=plan.digest(),
    )
    values = dict(
        decision_id=action.decision_id,
        confirmation_id=confirmation_id,
        policy_version=action.scope.policy_version,
        lease_id=lease_id or "l" * 32,
        fencing_token=fencing_token,
    )
    values.update(overrides)
    return dc_replace(base, **values)


def test_executor_refuses_unknown_action(tmp_path: Path):
    service, db, ledger, plans, clock, _session, actions, runner, executor = build_full(tmp_path)
    contract = _make_contract("f" * 64)
    with pytest.raises(ExecutionRefused, match="action_missing"):
        executor.execute_restart(contract, now=NOW + timedelta(minutes=3))
    db.close()


def test_executor_refuses_stale_version(tmp_path: Path):
    service, db, ledger, plans, clock, _session, actions, runner, executor = build_full(tmp_path)
    session = _login(service)
    decision, identity = _authorize_and_prepare(service, session)
    action = actions.get_action(identity.action_id)
    contract = _make_contract(identity.action_id, action_version=action.version + 100)
    with pytest.raises(ExecutionRefused, match="version"):
        executor.execute_restart(contract, now=NOW + timedelta(minutes=3))
    db.close()


def test_executor_refuses_production(tmp_path: Path):
    service, db, ledger, plans, clock, _session, actions, runner, executor = build_full(tmp_path)
    session = _login(service)
    decision, identity = _authorize_and_prepare(service, session)
    action = actions.get_action(identity.action_id)
    contract = _make_contract(identity.action_id, action_version=action.version, environment="production")
    with pytest.raises(ExecutionRefused, match="environment|capability"):
        executor.execute_restart(contract, now=NOW + timedelta(minutes=3))
    db.close()


# ---------------------------------------------------------------------------
# Kill-switch race (with fake interceptor, no real service)
# ---------------------------------------------------------------------------


def test_kill_switch_race_prevents_restart(tmp_path: Path):
    from aipm.control_plane.kill_switch import KillSwitchRegistry

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
    service, db, ledger, plans, clock, _session, actions, runner, executor = build_full(tmp_path)
    session = _login(service)
    decision, identity = _authorize_and_prepare(service, session)
    object.__setattr__(executor, "_kill_switches", switches)
    action = actions.get_action(identity.action_id)
    lease_id, token = _insert_lease(db, identity.action_id, action.version)
    contract = _real_contract(service, db, identity, action_version=action.version, lease_id=lease_id, fencing_token=token)
    # Now engage the kill switch
    switches.permit = False
    with pytest.raises(ExecutionRefused, match="kill_switch_engaged"):
        executor.execute_restart(contract, now=NOW + timedelta(minutes=3))
    # systemctl was NOT invoked
    restart_calls = [call for call in runner.calls if len(call) > 1 and call[1] == "restart"]
    assert len(restart_calls) == 0
    db.close()


# ---------------------------------------------------------------------------
# Lease race
# ---------------------------------------------------------------------------


def test_lease_race_prevents_restart(tmp_path: Path):
    service, db, ledger, plans, clock, _session, actions, runner, executor = build_full(tmp_path)
    session = _login(service)
    decision, identity = _authorize_and_prepare(service, session)
    # Acquire and then expire the lease
    repo = actions
    action = repo.get_action(identity.action_id)
    # Manually insert an expired lease (no SNAPSHOT_CAPTURED for systemd)
    with db.connection:
        db.connection.execute(
            "INSERT INTO execution_leases (lease_id, action_id, environment, fencing_token, state, holder, granted_at, expires_at, action_version)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            ("l" * 32, identity.action_id, "staging", 1, "granted", NOW.isoformat(), (NOW - timedelta(minutes=1)).isoformat(), action.version))
    contract = _make_contract(identity.action_id, action_version=action.version)
    with pytest.raises(ExecutionRefused, match="lease|confirmation"):
        executor.execute_restart(contract, now=NOW + timedelta(minutes=3))
    restart_calls = [call for call in runner.calls if len(call) > 1 and call[1] == "restart"]
    assert len(restart_calls) == 0
    db.close()


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------


def test_successful_restart(tmp_path: Path):
    service, db, ledger, plans, clock, _session, actions, runner, executor = build_full(tmp_path)
    session = _login(service)
    decision, identity = _authorize_and_prepare(service, session)
    action = actions.get_action(identity.action_id)
    lease_id, token = _insert_lease(db, identity.action_id, action.version)
    contract = _real_contract(service, db, identity, action_version=action.version, lease_id=lease_id, fencing_token=token)
    result = executor.execute_restart(contract, now=NOW + timedelta(minutes=3))
    assert result.outcome == "succeeded"
    assert result.provider_code == "restart_ok"
    # The restart was invoked exactly once
    restart_calls = [call for call in runner.calls if len(call) > 1 and call[1] == "restart"]
    assert len(restart_calls) == 1
    db.close()


def test_failed_restart(tmp_path: Path):
    service, db, ledger, plans, clock, _session, actions, runner, executor = build_full(tmp_path, restart_returncode=1)
    session = _login(service)
    decision, identity = _authorize_and_prepare(service, session)
    action = actions.get_action(identity.action_id)
    lease_id, token = _insert_lease(db, identity.action_id, action.version)
    contract = _real_contract(service, db, identity, action_version=action.version, lease_id=lease_id, fencing_token=token)
    result = executor.execute_restart(contract, now=NOW + timedelta(minutes=3))
    assert result.outcome == "failed"
    assert result.provider_code == "exit_1"
    db.close()


def test_unknown_outcome_on_timeout(tmp_path: Path):
    service, db, ledger, plans, clock, _session, actions, runner, executor = build_full(tmp_path, restart_times_out=True)
    session = _login(service)
    decision, identity = _authorize_and_prepare(service, session)
    action = actions.get_action(identity.action_id)
    lease_id, token = _insert_lease(db, identity.action_id, action.version)
    contract = _real_contract(service, db, identity, action_version=action.version, lease_id=lease_id, fencing_token=token)
    result = executor.execute_restart(contract, now=NOW + timedelta(minutes=3))
    assert result.outcome == "unknown_outcome"
    assert result.provider_code == "timeout"
    db.close()


# ---------------------------------------------------------------------------
# Capability registry integration
# ---------------------------------------------------------------------------


def test_systemd_capability_is_disabled_by_default():
    with pytest.raises(CapabilityPolicyError):
        DEFAULT_CAPABILITY_REGISTRY.require_executable(CapabilityId.RESTART_ALLOWLISTED_SYSTEMD_UNIT, environment="staging")


def test_systemd_capability_can_be_enabled_for_staging_only():
    from aipm.control_plane.capabilities_registry import (
        CapabilityDefinition, CapabilityPolicyError, PrivilegeClass, RiskClass, _definition,
    )
    enabled_def = _definition(
        CapabilityId.RESTART_ALLOWLISTED_SYSTEMD_UNIT,
        version="1", environments=("staging",), target_type="systemd_unit",
        reversible=False, snapshot="mc612-systemd-snapshot-v1",
        verification="systemd-is-active", reconciliation="observation-only",
        risk=RiskClass.HIGH, privilege=PrivilegeClass.SERVICE_ACCOUNT, enabled=True,
    )
    enabled_registry = CapabilityRegistry(capabilities={
        CapabilityId.RESTART_ALLOWLISTED_SYSTEMD_UNIT: enabled_def,
    })
    definition = enabled_registry.require_executable(CapabilityId.RESTART_ALLOWLISTED_SYSTEMD_UNIT, environment="staging")
    assert definition.enabled is True
    assert definition.reversible is False
    with pytest.raises(CapabilityPolicyError, match="environment"):
        enabled_registry.require_executable(CapabilityId.RESTART_ALLOWLISTED_SYSTEMD_UNIT, environment="production")


# ---------------------------------------------------------------------------
# Source security scan
# ---------------------------------------------------------------------------


def test_systemd_provider_source_is_bounded():
    source = Path("src/aipm/control_plane/systemd_provider.py").read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "UpdateEngine" not in source
    assert "GitProvider" not in source
    assert "DockerProvider" not in source
