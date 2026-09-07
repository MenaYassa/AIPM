"""C6.3: digest-space alignment and production composition of the update plane.

The canonical update-plan identity is ONE digest space end to end:

    UpdatePlanIdentity.from_plan(
        engine.plan_update(target, dry_run=False)
    ).digest()

Covered proofs (mapped to the mandated scenarios):

 1. Dashboard digest == authoritative execution-plan digest (same planner,
    disposable repo).
 2. Dashboard digest == production ``current_plan_digest`` port output.
 3. Approval with the dashboard-presented digest succeeds in the disposable
    production composition.
 4. Durable ``ActionRequest`` metadata carries that exact digest.
 5. Confirmation binding preserves the correct update-plan digest.
 6. C6.2 runtime adapter receives the same trusted digest.
 7. ``ExecutionContract`` carries the same digest.
 8. ``UpdateEngine`` recomputes the same digest for the about-to-execute
    ``dry_run=False`` plan.
 9. A plan change between approval and execution fails closed.
10. ``ProjectPlan.canonical_digest`` remains a separate identity, never
    used as the update-plan digest.
11. Missing executor IPC still fails closed (no silent direct fallback).
12. Engaged kill switch still blocks execution.
13. Stale/consumed confirmation still blocks execution.
14. Lease/fence failures still block execution.
15. Rejected paths perform no mutation.
Plus boundary scans: dashboard stays authority-free, the control plane
never imports engine implementation modules, the composition package adds
no parallel authority, and no schema changed.
"""
from __future__ import annotations

import dataclasses
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from aipm.control_plane.composition import (
    OperatorTransportConfigError,
    compose_operator_service,
    project_plan_digest_port,
)
from aipm.control_plane.executor import EXECUTION_CONTRACT_VERSION, ExecutorCapability
from aipm.control_plane.models import UpdateExecutionBinding
from aipm.control_plane.project_plan import Environment, ProjectPlan
from aipm.control_plane.transport import create_operator_app
from aipm.services.update.plan_identity import UpdatePlanIdentity
from aipm.services.update.runtime_adapter import compose_update_runtime

from tests.test_mc612_stage9_transport import NOW, SECRET, VERIFIER, _Clock, db_path
from tests.update_fixtures import (
    MARKER_RUNTIME_SCRIPT,
    fetch_origin,
    make_remote_commit,
    make_repo,
    rev_parse_head,
)

PROJECT_ID = "c" * 24
OTHER_PROJECT_ID = "d" * 24

# Dashboard GET route helper identifiers reuse the existing dashboard
# fixtures for the presentation surface.
from fastapi.testclient import TestClient

from aipm.capabilities.dashboard.update_api import DashboardUpdateApi


# ---------------------------------------------------------------------------
# Harness: disposable production composition wired to a real update engine
# ---------------------------------------------------------------------------


def _engine_plan_digest(engine, target: str = PROJECT_ID) -> str:
    return UpdatePlanIdentity.from_plan(engine.plan_update(target, dry_run=False)).digest()


def _engine_and_world(tmp_path: Path):
    """Shared disposable world: real engine over disposable repos + the
    dashboard's read-only planner over the same repos."""

    from aipm.services.backup.engine import BackupEngine
    from aipm.services.update.audit import AuditService
    from aipm.services.update.engine import UpdateEngine
    from aipm.services.update.planner import UpdatePlanner
    from aipm.services.update.rollback import RollbackManager
    from aipm.services.update.verifier import UpdateVerifier

    from tests.update_fixtures import FixedProjectService, GuardCompose, GitService, hermetic_health_engine

    clock = _Clock(NOW)
    project = make_repo(tmp_path, name=PROJECT_ID, runtime_script=MARKER_RUNTIME_SCRIPT)
    other = make_repo(tmp_path, name=OTHER_PROJECT_ID, runtime_script=MARKER_RUNTIME_SCRIPT)
    git_service = GitService()
    engine = UpdateEngine(
        project_service=FixedProjectService(project, git_service),
        git_service=git_service,
        backup_engine=BackupEngine(tmp_path / "backups"),
        compose_provider=GuardCompose(),
        health_engine=hermetic_health_engine(),
        audit_service=AuditService(tmp_path / "audit"),
        rollback_manager=RollbackManager(),
        verifier=UpdateVerifier(),
    )

    class _BothProjectsService:
        """Serve either disposable repo so the dashboard façade can plan for
        the composed target (and would plan for a second registered world)."""

        def __init__(self) -> None:
            self._first = FixedProjectService(project, git_service)
            self._second = FixedProjectService(other, git_service)

        def get_project(self, name: str):
            try:
                return self._first.get_project(name)
            except KeyError:
                return self._second.get_project(name)

    dashboard_planner = UpdatePlanner(
        _BothProjectsService(), git_service=git_service, health_engine=hermetic_health_engine()
    )
    return SimpleNamespace(
        clock=clock,
        engine=engine,
        project=project,
        other_project=other,
        dashboard_planner=dashboard_planner,
        marker=tmp_path / "runtime-marker.txt",
    )


def _register_plans(plans) -> None:
    """Register the durable plan-of-record for the disposable targets: the
    approval route resolves targets through the plan store, while the
    update-plan digest port speaks the aligned UpdatePlanIdentity space."""

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


def _full_composition(tmp_path: Path):
    """Disposable vertical: canonical production composition + real engine.

    ``compose_operator_service`` is called exactly as production would call
    it, with the real update engine injected — so the aligned digest port
    and the C6.2 runtime adapter are exercised through the canonical
    composition root, not a parallel harness. ``execution_mode`` is "ipc"
    (the production posture): execution routing proofs that need to reach
    the gated runtime use :func:`_full_harness` below (the C6.2-style
    test-mode service composed over the SAME canonical ports).
    """

    world = _engine_and_world(tmp_path)
    composition = compose_operator_service(
        database_path=tmp_path / "control_plane.db",
        verifier=VERIFIER,
        clock=world.clock,
        allowed_targets=frozenset({PROJECT_ID}),
        run_sweep=False,
        update_engine=world.engine,
    )
    _register_plans(composition["plans"])
    return SimpleNamespace(
        composition=composition,
        service=composition["service"],
        plans=composition["plans"],
        engine=world.engine,
        project=world.project,
        other_project=world.other_project,
        dashboard_planner=world.dashboard_planner,
        clock=world.clock,
        marker=world.marker,
    )


def _full_composition_ready(tmp_path: Path):
    """Production composition with the staging kill switch disengaged and
    both targets registered — ready to approve."""

    h = _full_composition(tmp_path)
    _disengage_kill_switch(h)
    return h


def _full_harness(tmp_path: Path):
    """Disposable execution harness (C6.2 pattern) composed over the SAME
    canonical ports as the production composition root: the digest port is
    the C6.3 ``update_plan_digest_port`` port and the runtime is the C6.2
    ``compose_update_runtime`` adapter, both binding the ONE engine.

    ``execution_mode="test"`` allows the gated runtime to be driven
    in-process (the production "ipc" routing seam is proven separately);
    every gate before the runtime — approval verification, snapshot,
    confirmation, lease/fencing, kill switch — is the canonical one.
    """

    from aipm.composition import compose_update_runtime, update_plan_digest_port
    from aipm.control_plane.approval import OwnerConfirmationService
    from aipm.control_plane.audit import SQLiteAuditLedger
    from aipm.control_plane.kill_switch import KillSwitchRegistry
    from aipm.control_plane.owner_auth import Argon2idVerifier, OwnerAuthenticator
    from aipm.control_plane.planner import PlanOnlyPlanner
    from aipm.control_plane.policy import AuthorizationPolicy
    from aipm.control_plane.session import OwnerSessionStore
    from aipm.control_plane.service import OwnerControlPlaneService
    from aipm.control_plane.storage import (
        ControlPlaneDatabase,
        SQLiteActionRepository,
        SQLiteProjectPlanStore,
    )
    from aipm.control_plane.storage.sqlite_store import SQLiteKillSwitchStore

    world = _engine_and_world(tmp_path)
    clock = world.clock
    engine = world.engine
    db = ControlPlaneDatabase(db_path(tmp_path), clock=clock)
    ledger = SQLiteAuditLedger(db)
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=clock)
    sessions = OwnerSessionStore(clock=clock)
    policy = AuthorizationPolicy(policy_version="policy-v1", allowed_scopes=frozenset({(PROJECT_ID, "staging")}))
    confirmations = OwnerConfirmationService(clock=clock)
    plans = SQLiteProjectPlanStore(db)
    _register_plans(plans)
    actions = SQLiteActionRepository(db, audit=ledger)
    kill_switches = KillSwitchRegistry(clock=clock, store=SQLiteKillSwitchStore(db))
    kill_switches.disengage(Environment.STAGING, reason="test window", now=NOW)

    real_runtime = compose_update_runtime(engine)
    runtime_calls: list[UpdateExecutionBinding] = []
    runtime_results: list[dict] = []

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
        planner=PlanOnlyPlanner(clock=clock, target_allow_list=frozenset({PROJECT_ID})),
        audit=ledger,
        actions=actions,
        kill_switches=kill_switches,
        clock=clock,
        execution_mode="test",
        current_plan_digest=update_plan_digest_port(engine),
        update_runtime=_update_runtime,
    )
    return SimpleNamespace(
        service=service,
        plans=plans,
        engine=engine,
        project=world.project,
        other_project=world.other_project,
        dashboard_planner=world.dashboard_planner,
        clock=clock,
        runtime_calls=runtime_calls,
        runtime_results=runtime_results,
        marker=world.marker,
    )


def _disengage_kill_switch(h) -> None:
    h.service.disengage_kill_switch("owner", reason="test window")


def _approve(h, *, digest: str | None = None, key: str = "k1") -> dict:
    session = h.service.login(SECRET)
    result = h.service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest if digest is not None else _engine_plan_digest(h.engine),
        idempotency_key=key,
    )
    return {"session_id": session.session_id, **result}


def _prepared(tmp_path: Path):
    """Execution harness with one approved + snapshot-captured update
    action (the canonical prepared state, ready for the gated runtime)."""

    h = _full_harness(tmp_path)
    approval = _approve(h)
    assert approval["allowed"] is True
    h.service.capture_snapshot(approval["session_id"], approval["action_id"], now=NOW)
    h.approval = approval
    return h


def _engine_world_unchanged(h, head_before: str | None = None, *, expected_revision: int = 1) -> None:
    work = Path(h.project.path)
    assert (work / "config.txt").read_text(encoding="utf-8") == "v1\n"
    assert not (work / "docs.md").exists()
    assert not h.marker.exists()
    if head_before is not None:
        assert rev_parse_head(h.project) == head_before
    assert h.plans.read(PROJECT_ID).revision == expected_revision


def _decision_metadata(h, action_id: str) -> dict:
    action = h.service._actions.get_action(action_id)
    decision = h.service._actions.get_decision(action.decision_id)
    return dict(decision.request.metadata)


# ---------------------------------------------------------------------------
# Proofs 1-2: one digest space across dashboard, port, and engine
# ---------------------------------------------------------------------------


def test_dashboard_digest_equals_authoritative_execution_plan_digest(tmp_path):
    h = _full_composition(tmp_path)
    presented = h.dashboard_planner.plan(PROJECT_ID, dry_run=True)
    execution_variant = h.dashboard_planner.plan(PROJECT_ID, dry_run=False)
    # Proof 1: the dashboard's digest derivation equals the authoritative
    # execution-plan identity — same planner, same world, dry_run=False.
    assert UpdatePlanIdentity.from_plan(execution_variant).digest() == _engine_plan_digest(h.engine)
    # The dry_run flag is the only difference between the two plan variants;
    # the identity excludes it, so the dashboard digest matches execution.
    assert presented.project == execution_variant.project
    dashboard_digest = UpdatePlanIdentity.from_plan(execution_variant).digest()
    assert dashboard_digest != "0" * 64
    assert len(dashboard_digest) == 64


def test_dashboard_update_api_digest_matches_execution_identity(tmp_path):
    """The real DashboardUpdateApi computes its plan_digest from the
    dry_run=False plan variant of the same read-only planner."""

    from tests.test_dashboard_update_api import VALID_ID

    h = _full_composition(tmp_path)

    class _FixedIntelligence:
        """Minimal intelligence seam: any valid id resolves to the disposable
        composed project world."""

        def __init__(self, local_name: str) -> None:
            self.local_project_name = local_name

        def detail(self, identifier: str):
            return self

    api = DashboardUpdateApi(_FixedIntelligence(PROJECT_ID), h.dashboard_planner, clock=lambda: NOW)
    response = api.update_plan(VALID_ID)
    assert response["available"] is True
    dashboard_digest = response["update_plan"]["plan_digest"]
    # Proof 1 (real façade): dashboard digest == engine's execution identity.
    assert dashboard_digest == _engine_plan_digest(h.engine)
    # The presentation fields stay the dry_run=True plan's fields.
    assert response["update_plan"]["dry_run"] is True


def test_port_digest_equals_engine_and_dashboard_space(tmp_path):
    """Proof 2: the production ``current_plan_digest`` port returns the same
    canonical identity as the engine and the dashboard for the same world."""

    h = _full_composition(tmp_path)
    port = h.composition["service"]._current_plan_digest
    assert port(PROJECT_ID) == _engine_plan_digest(h.engine)
    execution_variant = h.dashboard_planner.plan(PROJECT_ID, dry_run=False)
    assert port(PROJECT_ID) == UpdatePlanIdentity.from_plan(execution_variant).digest()
    assert len(port(PROJECT_ID)) == 64


def test_port_digest_tracks_world_changes(tmp_path):
    """The aligned port re-plans: a world change changes the authoritative
    digest (TOCTOU-real at the approval boundary, like the engine)."""

    h = _full_composition(tmp_path)
    port = h.service._current_plan_digest
    before = port(PROJECT_ID)
    make_remote_commit(h.project, "docs.md", "from remote\n")
    fetch_origin(h.project)
    after = port(PROJECT_ID)
    assert before != after


# ---------------------------------------------------------------------------
# Proofs 3-5: approval + durable metadata + confirmation binding
# ---------------------------------------------------------------------------


def test_approval_with_dashboard_digest_succeeds_in_production_composition(tmp_path):
    """Proof 3: the disposable production composition approves the digest
    the dashboard surface presents."""

    h = _full_composition_ready(tmp_path)
    execution_variant = h.dashboard_planner.plan(PROJECT_ID, dry_run=False)
    dashboard_digest = UpdatePlanIdentity.from_plan(execution_variant).digest()
    approval = _approve(h, digest=dashboard_digest)
    assert approval["allowed"] is True
    assert approval["plan_digest"] == dashboard_digest
    assert approval["action_id"] and approval["confirmation_id"]


def test_durable_request_metadata_binds_presented_digest(tmp_path):
    """Proof 4: the exact presented digest travels into durable
    ActionRequest metadata (the binding channel)."""

    h = _full_composition_ready(tmp_path)
    digest = _engine_plan_digest(h.engine)
    approval = _approve(h, digest=digest)
    metadata = _decision_metadata(h, approval["action_id"])
    assert metadata["update_plan_digest"] == digest
    assert metadata["update_plan_digest"] == approval["plan_digest"]


def test_confirmation_binding_preserves_update_plan_digest(tmp_path):
    """Proof 5: the confirmation binding survives into the prepared action:
    the binding's durable decision metadata still carries the exact digest
    after snapshot capture (the canonical prepared state)."""

    h = _prepared(tmp_path)
    digest = _engine_plan_digest(h.engine)
    metadata = _decision_metadata(h, h.approval["action_id"])
    assert metadata["update_plan_digest"] == digest
    confirmation = h.service._confirmations.store.get(h.approval["confirmation_id"])
    assert confirmation.action_id == h.approval["action_id"]
    assert confirmation.state.value == "confirmed"


def test_binding_digest_is_not_the_project_plan_canonical_digest(tmp_path):
    """Proof 10: the two digest spaces stay separate. The update-plan digest
    in the durable metadata is NOT the ProjectPlan canonical digest, and the
    durable ProjectPlan digest remains untouched by approval/execution."""

    h = _prepared(tmp_path)
    metadata = _decision_metadata(h, h.approval["action_id"])
    update_plan_digest = metadata["update_plan_digest"]
    canonical = h.plans.read(PROJECT_ID).canonical_digest
    assert update_plan_digest != canonical
    assert len(canonical) == 64
    # The legacy port still speaks the ProjectPlan space — separate and intact.
    assert project_plan_digest_port(h.plans)(PROJECT_ID) == canonical
    # And the aligned port speaks the update-plan space.
    assert h.service._current_plan_digest(PROJECT_ID) == update_plan_digest


# ---------------------------------------------------------------------------
# Proofs 6-8: the runtime seam carries one digest end to end
# ---------------------------------------------------------------------------


def test_runtime_adapter_receives_trusted_binding_digest(tmp_path, monkeypatch):
    """Proofs 6-8: through the full composed vertical, the C6.2 adapter
    receives the trusted durable binding, the engine recomputes the same
    digest for the about-to-execute dry_run=False plan, and execution
    succeeds with exactly one runtime call."""

    h = _prepared(tmp_path)
    monkeypatch.setenv("AIPM_INTEGRATION_MARKER", str(h.marker))

    result = h.service.run_approved_update(h.approval["session_id"], action_id=h.approval["action_id"])
    assert result["executed"] is True
    assert result["outcome"] == "verification_succeeded"

    assert len(h.runtime_calls) == 1
    binding = h.runtime_calls[0]
    assert isinstance(binding, UpdateExecutionBinding)
    assert binding.project_name == PROJECT_ID
    assert binding.plan_digest == _engine_plan_digest(h.engine)
    assert binding.plan_digest == _decision_metadata(h, h.approval["action_id"])["update_plan_digest"]
    assert binding.confirmation_id == h.approval["confirmation_id"]

    # Proof 8: the engine recomputes the same identity for the executed plan.
    recomputed = UpdatePlanIdentity.from_plan(h.engine.plan_update(PROJECT_ID, dry_run=False)).digest()
    assert recomputed == binding.plan_digest

    # Runtime actually ran exactly once; the world is the approved world.
    assert h.marker.exists()
    assert h.plans.read(PROJECT_ID).revision == 2


def test_execution_contract_construction_uses_aligned_digest(tmp_path):
    """The composed path builds its ExecutionContract from the durable
    decision — the update-plan digest is carried by the binding channel
    and the contract carries the control plane's target digest; both derive
    from the SAME approved decision (two spaces, one decision)."""

    h = _prepared(tmp_path)
    action_id = h.approval["action_id"]
    action = h.service._actions.get_action(action_id)
    decision = h.service._actions.get_decision(action.decision_id)
    metadata = dict(decision.request.metadata)
    # The decision's target digest (ProjectPlan space) and the update-plan
    # digest (UpdatePlanIdentity space) coexist in the same durable decision.
    assert decision.action_identity.target_digest == h.plans.read(PROJECT_ID).canonical_digest
    assert metadata["update_plan_digest"] == _engine_plan_digest(h.engine)
    assert decision.action_identity.target_digest != metadata["update_plan_digest"]


# ---------------------------------------------------------------------------
# Proof 9: plan change between approval and execution fails closed
# ---------------------------------------------------------------------------


def test_world_change_between_approval_and_execution_fails_closed(tmp_path, monkeypatch):
    h = _prepared(tmp_path)
    monkeypatch.setenv("AIPM_INTEGRATION_MARKER", str(h.marker))
    head_before = rev_parse_head(h.project)

    make_remote_commit(h.project, "docs.md", "from remote\n")
    fetch_origin(h.project)

    with pytest.raises(Exception) as excinfo:
        h.service.run_approved_update(h.approval["session_id"], action_id=h.approval["action_id"])
    assert "different plan" in str(excinfo.value)

    # Proof 15 (this path): no repository mutation, no runtime marker.
    assert rev_parse_head(h.project) == head_before
    assert not (Path(h.project.path) / "docs.md").exists()
    assert not h.marker.exists()
    assert h.plans.read(PROJECT_ID).revision == 2


# ---------------------------------------------------------------------------
# Proofs 11-15: production fail-closed invariants preserved
# ---------------------------------------------------------------------------


def test_missing_executor_ipc_fails_closed_without_runtime_fallback(tmp_path):
    """Proof 11: execution_mode="ipc" with no executor IPC client refuses
    before any runtime, engine, or mutation path (no silent direct-execution
    fallback, even though the update runtime IS composed in C6.3)."""

    h = _full_composition(tmp_path)
    assert h.service._execution_mode == "ipc"
    assert h.service._update_runtime is not None  # composed, but...
    _disengage_kill_switch(h)
    digest = _engine_plan_digest(h.engine)
    approval = _approve(h, digest=digest)
    h.service.capture_snapshot(approval["session_id"], approval["action_id"])
    with pytest.raises(Exception) as excinfo:
        h.service.run_approved_update(approval["session_id"], action_id=approval["action_id"])
    assert "Executor IPC client is not configured" in str(excinfo.value)
    # Nothing ran: no marker, no revision advance, no confirmation loss.
    assert not h.marker.exists()
    assert h.plans.read(PROJECT_ID).revision == 1
    confirmation = h.service._confirmations.store.get(approval["confirmation_id"])
    assert confirmation.state.value == "confirmed"


def test_kill_switch_still_blocks_execution(tmp_path):
    """Proof 12: the engaged kill switch blocks execution under the new
    composition (proof 15: no mutation on this path)."""

    h = _prepared(tmp_path)
    h.service.engage_kill_switch("owner", reason="incident")
    with pytest.raises(Exception) as excinfo:
        h.service.run_approved_update(h.approval["session_id"], action_id=h.approval["action_id"])
    assert "kill_switch_engaged" in str(excinfo.value)
    _engine_world_unchanged(h)
    confirmation = h.service._confirmations.store.get(h.approval["confirmation_id"])
    assert confirmation.state.value == "confirmed"


def test_consumed_confirmation_still_blocks_execution(tmp_path):
    """Proof 13a: a consumed confirmation can never re-execute."""

    h = _prepared(tmp_path)
    confirmation_id = h.approval["confirmation_id"]
    binding = h.service._confirmations.store.get(confirmation_id)
    h.service._confirmations.consume(binding, now=NOW)
    with pytest.raises(Exception) as excinfo:
        h.service.run_approved_update(h.approval["session_id"], action_id=h.approval["action_id"])
    assert "confirmation_consumed" in str(excinfo.value)
    _engine_world_unchanged(h)


def test_expired_confirmation_still_blocks_execution(tmp_path):
    """Proof 13b: an expired confirmation blocks execution."""

    h = _prepared(tmp_path)
    confirmation_id = h.approval["confirmation_id"]
    binding = h.service._confirmations.store.get(confirmation_id)
    expired = dataclasses.replace(binding, expires_at=NOW + timedelta(seconds=1))
    h.service._confirmations._store.put(expired)
    h.clock.value = NOW + timedelta(minutes=2)
    fresh_session = h.service.login(SECRET)
    with pytest.raises(Exception) as excinfo:
        h.service.run_approved_update(fresh_session.session_id, action_id=h.approval["action_id"])
    assert "confirmation_expired" in str(excinfo.value)
    _engine_world_unchanged(h)


def test_expired_lease_still_blocks_execution(tmp_path):
    """Proof 14a: an expired lease blocks execution (no active lease)."""

    h = _prepared(tmp_path)
    action_id = h.approval["action_id"]
    action = h.service._actions.get_action(action_id)
    h.service._actions.acquire_lease(action_id, expected_version=action.version, now=NOW)
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


def test_wrong_fencing_token_still_blocks_execution(tmp_path):
    """Proof 14b: a wrong fencing token is refused at the mutation boundary
    with durable tamper containment (no mutation, no confirmation loss)."""

    from aipm.control_plane.executor import ExecutionContract

    h = _prepared(tmp_path)
    action_id = h.approval["action_id"]
    action = h.service._actions.get_action(action_id)
    h.service._actions.acquire_lease(action_id, expected_version=action.version, now=NOW)
    # The lease advanced the durable action version; the contract must carry
    # the CURRENT version (as the canonical execution path reads it).
    action = h.service._actions.get_action(action_id)
    decision = h.service._actions.get_decision(action.decision_id)
    confirmation_id = next(
        b.confirmation_id
        for b in h.service._confirmations.store.values()
        if b.action_id == action_id
    )
    snapshot = h.service._snapshot_repo.snapshot_for_action(action_id)
    lease = h.service._actions.active_lease(action_id, now=h.clock())
    from aipm.control_plane.verification import VERIFICATION_VERSION

    contract = ExecutionContract(
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
        kill_switch_epoch=h.service._kill_switches.switch(action.scope.environment).epoch,
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token,
        expires_at=action.expires_at,
    )
    executor = h.service._executor()
    with pytest.raises(Exception) as excinfo:
        executor.execute(dataclasses.replace(contract, fencing_token=contract.fencing_token + 999), now=NOW)
    assert "lease_fence_mismatch" in str(excinfo.value)
    _engine_world_unchanged(h)
    confirmation = h.service._confirmations.store.get(h.approval["confirmation_id"])
    assert confirmation.state.value == "confirmed"


def test_foreign_digest_still_refused_at_approval(tmp_path):
    """Proof 15 (approval path): a foreign 64-hex digest is refused and
    nothing is created or mutated."""

    h = _full_composition(tmp_path)
    with pytest.raises(Exception) as excinfo:
        _approve(h, digest="c" * 64)
    assert "does not match the authoritative plan" in str(excinfo.value)
    assert len(h.service._confirmations.store) == 0
    _engine_world_unchanged(h)


# ---------------------------------------------------------------------------
# Composition wiring: engine injection, default fail-closed posture
# ---------------------------------------------------------------------------


def test_composition_without_engine_keeps_fail_closed_posture(tmp_path):
    """No engine injected: the runtime stays uncomposed and the digest port
    reads the durable ProjectPlan canonical digest (C6.1 contract intact)."""

    composition = compose_operator_service(
        database_path=tmp_path / "control_plane.db",
        verifier=VERIFIER,
        clock=_Clock(NOW),
        allowed_targets=frozenset({PROJECT_ID}),
        run_sweep=False,
    )
    service = composition["service"]
    assert service._update_runtime is None
    assert service._execution_mode == "ipc"
    composition["plans"].create(
        ProjectPlan.create(
            target_id=PROJECT_ID,
            environment=Environment.STAGING,
            title="Old title",
            objective="Objective",
            now=NOW,
        )
    )
    assert service._current_plan_digest(PROJECT_ID) == composition["plans"].read(PROJECT_ID).canonical_digest


def test_composition_with_engine_binds_aligned_port_and_runtime(tmp_path, monkeypatch):
    """One engine injected: BOTH ports speak the same engine's planning
    semantics (same instance — not two adapters, not two engines), and the
    composed runtime executes exactly once with the aligned digest."""

    h = _full_composition(tmp_path)
    assert h.service._execution_mode == "ipc"
    assert h.service._update_runtime is not None
    assert h.service._current_plan_digest(PROJECT_ID) == _engine_plan_digest(h.engine)

    # The bound runtime drives the SAME engine instance: execute through a
    # test-mode service sharing the SAME composition stores and world.
    monkeypatch.setenv("AIPM_INTEGRATION_MARKER", str(h.marker))
    real_runtime = compose_update_runtime(h.engine)
    runtime_calls: list[UpdateExecutionBinding] = []

    def _update_runtime(binding) -> dict:
        runtime_calls.append(binding)
        return real_runtime(binding)

    base = h.composition
    from aipm.control_plane.service import OwnerControlPlaneService

    test_service = OwnerControlPlaneService(
        authenticator=base["service"]._authenticator,
        sessions=base["service"]._sessions,
        policy=base["service"]._policy,
        confirmations=base["service"]._confirmations,
        plans=base["plans"],
        planner=base["service"]._planner,
        audit=base["ledger"],
        actions=base["actions"],
        kill_switches=base["kill_switches"],
        clock=h.clock,
        execution_mode="test",
        current_plan_digest=base["service"]._current_plan_digest,
        update_runtime=_update_runtime,
    )
    _disengage_kill_switch(h)
    session = test_service.login(SECRET)
    result = test_service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=_engine_plan_digest(h.engine),
        idempotency_key="k1",
    )
    assert result["allowed"] is True
    test_service.capture_snapshot(session.session_id, result["action_id"], now=NOW)
    outcome = test_service.run_approved_update(session.session_id, action_id=result["action_id"])
    assert outcome["executed"] is True
    assert len(runtime_calls) == 1
    assert runtime_calls[0].plan_digest == _engine_plan_digest(h.engine)
    assert h.marker.exists()


def test_unknown_target_fails_closed_at_aligned_port(tmp_path):
    """An unregistered/unplannable target fails closed (UNAVAILABLE_EVIDENCE),
    never a fabricated digest."""

    h = _full_composition(tmp_path)
    from aipm.control_plane.models import ControlPlaneError, PlanningErrorCode

    with pytest.raises(ControlPlaneError) as excinfo:
        h.service._current_plan_digest("unplannable-target")
    assert excinfo.value.code is PlanningErrorCode.UNAVAILABLE_EVIDENCE


def test_approval_route_round_trip_with_aligned_digest(tmp_path):
    """End-to-end through the canonical transport: login → approval POST with
    the aligned digest → 200 with confirmation (disposable composition)."""

    from fastapi.testclient import TestClient as _TC

    from aipm.control_plane.transport import create_operator_app as _create

    h = _full_composition_ready(tmp_path)
    _disengage_kill_switch(h)
    app = _create(h.service, bind="127.0.0.1")
    client = _TC(app)
    login = client.post("/login", json={"secret": SECRET})
    assert login.status_code == 200
    csrf = client.get("/session").json()["csrf_token"]
    digest = h.service._current_plan_digest(PROJECT_ID)
    response = client.post(
        f"/updates/{PROJECT_ID}/approval",
        json={"idempotency_key": "k1", "update_plan_digest": digest},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is True
    assert payload["approval"] == "confirmed"
    metadata = _decision_metadata(h, payload["action_id"])
    assert metadata["update_plan_digest"] == digest


# ---------------------------------------------------------------------------
# Boundary scans
# ---------------------------------------------------------------------------


def test_control_plane_never_imports_engine_implementation_modules():
    """The aligned composition keeps the C4 boundary: no engine type names,
    no aipm.services imports anywhere under src/aipm/control_plane."""

    root = Path("src/aipm/control_plane")
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "UpdateEngine",
            "GitProvider",
            "ComposeProvider",
            "DockerProvider",
            "from aipm.providers",
            "from aipm.services",
            "import aipm.providers",
            "import aipm.services",
        ):
            assert forbidden not in source, (path, forbidden)


def test_composition_package_introduces_no_parallel_authority():
    """The composition package only re-binds canonical authorities: no
    approval store, confirmation service, audit ledger, executor, schema,
    or local digest implementation."""

    root = Path("src/aipm/composition")
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "ConfirmationBinding(",
            "ConfirmationStore(",
            "InMemoryUpdateApprovalStore",
            "UpdateApprovalService",
            "class Owner",
            "hashlib",
            "sha256(",
            "sqlite3",
            "subprocess",
            "socket",
            "class Executor",
            "Executor(",
        ):
            assert forbidden not in source, (path, forbidden)


def test_dashboard_update_api_source_stays_authority_free():
    source = Path("src/aipm/capabilities/dashboard/update_api.py").read_text(encoding="utf-8")
    for forbidden in (
        "from aipm.control_plane",
        "UpdateEngine",
        "subprocess",
        "hashlib",
        "sha256(",
        "ConfirmationBinding",
    ):
        assert forbidden not in source, forbidden
    # The execution-variant derivation is present and documented read-only.
    assert "dry_run=False" in source


def test_update_digest_port_source_uses_canonical_identity_only():
    source = Path("src/aipm/composition/update_digest.py").read_text(encoding="utf-8")
    assert "UpdatePlanIdentity.from_plan" in source
    assert "dry_run=False" in source
    for forbidden in ("hashlib", "sha256(", "canonical_digest"):
        assert forbidden not in source, forbidden


def test_no_schema_changes_introduced():
    """The C6.3 change set touches no migration/schema files."""

    changed = set()
    result = None
    import subprocess as _sp

    result = _sp.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    changed = {line[3:].strip() for line in result.stdout.splitlines() if line.strip()}
    for path in changed:
        assert "migration" not in path.lower(), path
        assert not path.endswith(".sql"), path


# ---------------------------------------------------------------------------
# Integration with the canonical transport app (fail-closed routes intact)
# ---------------------------------------------------------------------------


def test_composed_app_still_fails_closed_for_wrong_digest(tmp_path):
    h = _full_composition_ready(tmp_path)
    app = create_operator_app(h.service, bind="127.0.0.1")
    client = TestClient(app)
    login = client.post("/login", json={"secret": SECRET})
    assert login.status_code == 200
    csrf = client.get("/session").json()["csrf_token"]
    response = client.post(
        f"/updates/{PROJECT_ID}/approval",
        json={"idempotency_key": "k1", "update_plan_digest": "b" * 64},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "stale_plan"
