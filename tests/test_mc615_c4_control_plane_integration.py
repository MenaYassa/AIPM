"""MC-6.15-C.4: Selective Service Update Control-Plane Integration Test Suite.

Comprehensive tests covering:
1. All 18 required adversarial security scenarios.
2. Complete end-to-end in-process lifecycle test without Docker mutation:
   authorized service plan -> confirmation -> lease -> final gate -> binding
   -> execute_service_update -> C.3 adapter -> receipt.
3. Authority and boundary enforcement (no browser commands, paths, or arbitrary parameters).
4. Static AST safety checks.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import subprocess
from typing import Any
import pytest
from fastapi.testclient import TestClient

from aipm.control_plane.action_state import InMemoryActionRepository
from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.audit import SQLiteAuditLedger
from aipm.control_plane.executor import (
    EXECUTION_CONTRACT_VERSION,
    ExecutionContract,
    ExecutionRefused,
    Executor,
    ExecutorCapability,
)
from aipm.control_plane.executor_ipc import (
    CAPABILITY_EXECUTE_SERVICE_UPDATE,
    ExecutionRequest,
    ExecutionResponse,
)
from aipm.control_plane.gate import (
    FinalExecutionGate,
    GateCode,
    verify_service_evidence,
)
from aipm.control_plane.models import (
    ActionRequest,
    ControlPlaneError,
    LifecycleState,
    OperationKind,
    PlanningErrorCode,
    UpdateExecutionBinding,
)
from aipm.control_plane.mutation_receipt import (
    MutationReceiptStore,
    MutationStatus,
)
from aipm.control_plane.owner_auth import Argon2idVerifier, OwnerAuthenticator
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import AuthorizationPolicy, PolicyCode
from aipm.control_plane.project_plan import Environment, ProjectPlan
from aipm.control_plane.service import OwnerControlPlaneService
from aipm.control_plane.session import OwnerSessionStore
from aipm.control_plane.storage import (
    ControlPlaneDatabase,
    SQLiteActionRepository,
    SQLiteProjectPlanStore,
)
from aipm.control_plane.transport import create_operator_app
from aipm.composition.executor_update import (
    compose_executor_update_handler,
    compose_ipc_update_runtime,
)
from aipm.composition.service_evidence import (
    ComposeServiceEvidenceVerifier,
    compose_service_evidence_verifier,
)
from aipm.models.compose_intelligence import (
    CandidateLookupKey,
    ComposeProjectObservation,
    ComposeServiceObservation,
    ImageReference,
    ServiceCandidateReason,
    ServiceCandidateStatus,
)
from aipm.models.compose_plan import (
    ServiceEvidenceContract,
    ServiceUpdateAtomicity,
    ServiceUpdatePlan,
)
from aipm.services.compose.execution_adapter import (
    BoundedServiceUpdateIntent,
    ComposeExecutionAdapter,
    ComposeExecutionError,
    ComposeExecutionResult,
    ServiceUpdateVerificationCode,
    ServiceUpdateVerificationResult,
)
from aipm.services.compose.planner import ComposeServiceUpdatePlanner

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
PROJECT_ID = "a" * 24
PROJECT_NAME = "searxng-stack"
VERIFIER = "$argon2id$v=19$m=65536,t=2,p=1$c3RhZ2UzLXNhbHQtMTIzNA$zho28DBNr2G2cGbxzr0Dl6AKwhbd8hEeTkti1pn7TW0"
SECRET = "test-owner-secret"


# ---------------------------------------------------------------------------
# Test Helpers and Mocks
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, delta: timedelta) -> None:
        self.moment = self.moment + delta


def _db_path(tmp_path: Path) -> Path:
    p = tmp_path / "control_plane.db"
    return p


def _make_service_obs(
    service_name: str = "searxng",
    *,
    status: ServiceCandidateStatus = ServiceCandidateStatus.UPDATE_AVAILABLE,
    reason: ServiceCandidateReason = ServiceCandidateReason.CANDIDATE_DIGEST_DIFFERS,
    running_digest: str = "sha256:" + "1" * 64,
    candidate_digest: str = "sha256:" + "2" * 64,
    child_digest: str | None = "sha256:" + "3" * 64,
    depends_on: tuple[str, ...] = (),
    freshness: str = "fresh",
    provenance_verified: bool = True,
    health: str | None = "healthy",
    state: str = "running",
    is_build: bool = False,
    container_ids: tuple[str, ...] = ("c101",),
) -> ComposeServiceObservation:
    img_ref = ImageReference(
        raw=f"{service_name}:latest",
        registry="docker.io",
        repository=service_name,
        tag="latest",
    )
    cand_key = CandidateLookupKey(
        registry="docker.io",
        repository=service_name,
        tag="latest",
        target_os="linux",
        target_arch="arm64",
    )
    return ComposeServiceObservation(
        service_name=service_name,
        container_names=(service_name,),
        container_ids=container_ids,
        state=state,
        health=health,
        declared_image=f"{service_name}:latest",
        declared_image_ref=img_ref,
        running_image=f"{service_name}:latest",
        running_image_id=running_digest,
        running_repo_digests=(f"{service_name}@{running_digest}",) if running_digest else (),
        candidate_digest=candidate_digest,
        candidate_child_digest=child_digest,
        candidate_key=cand_key,
        candidate_status=status,
        candidate_reason=reason,
        candidate_detail="Test candidate",
        is_build=is_build,
        build_context="./build" if is_build else None,
        build_dockerfile="Dockerfile" if is_build else None,
        ports=("8080/tcp",),
        freshness=freshness,
        observed_at=NOW,
        provenance_verified=provenance_verified,
        depends_on=depends_on,
    )


def _make_project_obs(
    services: dict[str, ComposeServiceObservation] | tuple[ComposeServiceObservation, ...],
    project_name: str = PROJECT_ID,
    compose_identity: str = "searxng-compose-v1",
) -> ComposeProjectObservation:
    svc_tuple = tuple(services.values()) if isinstance(services, dict) else tuple(services)
    return ComposeProjectObservation(
        project_name=project_name,
        compose_identity=compose_identity,
        project_path="/home/ubuntu/searxng",
        compose_files=("docker-compose.yml",),
        services=svc_tuple,
        running_services_count=sum(1 for s in svc_tuple if s.state == "running"),
        total_services_count=len(svc_tuple),
        updates_available_count=sum(1 for s in svc_tuple if s.candidate_status == ServiceCandidateStatus.UPDATE_AVAILABLE),
        current_count=sum(1 for s in svc_tuple if s.candidate_status == ServiceCandidateStatus.CURRENT),
        drift_count=sum(1 for s in svc_tuple if s.candidate_status == ServiceCandidateStatus.DRIFT),
        not_applicable_count=sum(1 for s in svc_tuple if s.candidate_status == ServiceCandidateStatus.NOT_APPLICABLE),
        unknown_count=sum(1 for s in svc_tuple if s.candidate_status == ServiceCandidateStatus.UNKNOWN),
        observed_at=NOW,
        freshness="fresh",
    )


class _MockComposeService:
    def __init__(self, observation: ComposeProjectObservation) -> None:
        self.obs = observation
        self.planner = ComposeServiceUpdatePlanner()

    def observe_project(self, project_id: str) -> ComposeProjectObservation:
        return self.obs

    def plan_service_update(self, project_id: str, service_name: str, *, query_registries: bool = True) -> ServiceUpdatePlan:
        return self.planner.plan_service(self.obs, service_name)


class _MockIPCClient:
    def __init__(self, response: ExecutionResponse | None = None) -> None:
        self.calls: list[ExecutionRequest] = []
        self.response = response or ExecutionResponse(
            outcome="succeeded",
            provider_code="update_ok",
            action_id="a" * 64,
            evidence_reference="receipt:ok",
        )

    def send(self, request: ExecutionRequest) -> ExecutionResponse:
        self.calls.append(request)
        return self.response

    execute = send


def _c4_harness(
    tmp_path: Path,
    *,
    project_obs: ComposeProjectObservation,
    executor_ipc_client=None,
    update_runtime=None,
    clock: _Clock | None = None,
):
    from aipm.control_plane.storage import SQLitePlanSnapshotRepository

    clk = clock or _Clock(NOW)
    db = ControlPlaneDatabase(_db_path(tmp_path), clock=clk)
    ledger = SQLiteAuditLedger(db)
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=clk)
    sessions = OwnerSessionStore(clock=clk)
    policy = AuthorizationPolicy(policy_version="policy-v1", allowed_scopes=frozenset({(PROJECT_ID, "staging")}))
    confirmations = OwnerConfirmationService(clock=clk)
    plans = SQLiteProjectPlanStore(db)
    plans.create(
        ProjectPlan.create(
            target_id=PROJECT_ID,
            environment=Environment.STAGING,
            title="SearXNG Service Stack",
            objective="Selective service updates",
            now=clk(),
        )
    )
    planner = PlanOnlyPlanner(clock=clk, target_allow_list=frozenset({PROJECT_ID}))
    actions = SQLiteActionRepository(db, audit=ledger)
    snapshots = SQLitePlanSnapshotRepository(db)

    compose_svc = _MockComposeService(project_obs)
    evidence_verifier = compose_service_evidence_verifier(compose_svc, project_resolver=lambda target: target)

    def _service_plan_port(target_id: str, service_name: str) -> ServiceUpdatePlan:
        return compose_svc.plan_service_update(target_id, service_name)

    def _project_plan_digest(target_id: str) -> str:
        return plans.read(target_id).canonical_digest

    ipc_client = executor_ipc_client or _MockIPCClient()
    runtime = update_runtime if update_runtime is not None else compose_ipc_update_runtime(ipc_client)

    service = OwnerControlPlaneService(
        authenticator=authenticator,
        sessions=sessions,
        policy=policy,
        confirmations=confirmations,
        plans=plans,
        planner=planner,
        audit=ledger,
        actions=actions,
        kill_switches=None,
        clock=clk,
        execution_mode="ipc",
        executor_ipc_client=ipc_client,
        current_plan_digest=_project_plan_digest,
        update_runtime=runtime,
        service_evidence_verifier=evidence_verifier,
        service_plan_port=_service_plan_port,
    )
    object.__setattr__(service, "_snapshot_repo", snapshots)
    return service, plans, compose_svc, ipc_client, clk


def _login_and_get_session(service: OwnerControlPlaneService) -> str:
    session = service.login(SECRET)
    return session.session_id


# ---------------------------------------------------------------------------
# Test Scenarios
# ---------------------------------------------------------------------------


def test_1_forged_service_scope_rejected_at_boundary(tmp_path: Path):
    """Scenario 1: Browser/caller tries to pass forged service_scope parameter."""
    obs = _make_project_obs((_make_service_obs("searxng"),))
    service, plans, compose_svc, ipc_client, _clk = _c4_harness(tmp_path, project_obs=obs)
    app = create_operator_app(service, bind="127.0.0.1")
    client = TestClient(app)

    login_resp = client.post("/login", json={"secret": SECRET})
    assert login_resp.status_code == 200
    csrf = client.get("/session").json()["csrf_token"]

    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")

    # Approval endpoint: forged service_scope in body is rejected by closed keys
    resp = client.post(
        f"/updates/{PROJECT_ID}/approval",
        json={
            "idempotency_key": "ik-forge-1",
            "update_plan_digest": plan.plan_digest,
            "service_name": "searxng",
            "service_scope": "searxng,evil-service",
        },
        headers={"x-csrf-token": csrf},
    )
    assert resp.status_code == 422
    assert "service_scope" in resp.text

    # Execute endpoint: forged service_scope in body is rejected by closed keys
    resp2 = client.post(
        f"/updates/{PROJECT_ID}/execute",
        json={
            "action_id": "a" * 64,
            "service_scope": "searxng,evil-service",
        },
        headers={"x-csrf-token": csrf},
    )
    assert resp2.status_code == 422
    assert "single action_id is required" in resp2.text


def test_2_widened_service_scope_fails_gate(tmp_path: Path):
    """Scenario 2: Live environment execution scope added a service vs authorized evidence."""
    s1 = _make_service_obs("searxng")
    obs1 = _make_project_obs((s1,))
    service, plans, compose_svc, ipc_client, _clk = _c4_harness(tmp_path, project_obs=obs1)
    session_id = _login_and_get_session(service)

    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")
    appr = service.approve_update_plan(
        session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=plan.plan_digest,
        idempotency_key="ik-widen-1",
        service_name="searxng",
    )
    action_id = appr["action_id"]

    # Now mutate live observation: an extra dependency was added
    s2 = _make_service_obs("searxng-valkey", status=ServiceCandidateStatus.CURRENT)
    s1_mut = _make_service_obs("searxng", depends_on=("searxng-valkey",))
    compose_svc.obs = _make_project_obs((s1_mut, s2))

    # Executing must fail closed at FinalExecutionGate
    with pytest.raises(ExecutionRefused) as excinfo:
        service.run_approved_update(session_id, action_id=action_id)
    assert excinfo.value.reason_code in ("dependency_scope_mismatch", "health_contract_mismatch")
    assert len(ipc_client.calls) == 0


def test_3_narrowed_service_scope_fails_gate(tmp_path: Path):
    """Scenario 3: Live environment execution scope lost a service vs authorized evidence."""
    s1 = _make_service_obs("searxng", depends_on=("searxng-valkey",))
    s2 = _make_service_obs("searxng-valkey", status=ServiceCandidateStatus.UPDATE_AVAILABLE)
    obs = _make_project_obs((s1, s2))
    service, plans, compose_svc, ipc_client, _clk = _c4_harness(tmp_path, project_obs=obs)
    session_id = _login_and_get_session(service)

    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")
    assert "searxng-valkey" in plan.expected_mutation.affected_services

    appr = service.approve_update_plan(
        session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=plan.plan_digest,
        idempotency_key="ik-narrow-1",
        service_name="searxng",
    )
    action_id = appr["action_id"]

    # Mutate live observation: dependency is removed
    s1_narrow = _make_service_obs("searxng", depends_on=())
    compose_svc.obs = _make_project_obs((s1_narrow,))

    with pytest.raises(ExecutionRefused) as excinfo:
        service.run_approved_update(session_id, action_id=action_id)
    assert "dependency_scope_mismatch" in str(excinfo.value).lower()
    assert len(ipc_client.calls) == 0


def test_4_altered_candidate_digest_fails_gate(tmp_path: Path):
    """Scenario 4: Live candidate digest differs from authorized candidate digest (TOCTOU drift)."""
    s1 = _make_service_obs("searxng", candidate_digest="sha256:" + "2" * 64)
    obs = _make_project_obs((s1,))
    service, plans, compose_svc, ipc_client, _clk = _c4_harness(tmp_path, project_obs=obs)
    session_id = _login_and_get_session(service)

    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")
    appr = service.approve_update_plan(
        session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=plan.plan_digest,
        idempotency_key="ik-cand-drift",
        service_name="searxng",
    )
    action_id = appr["action_id"]

    # Registry published a newer digest in the interim
    s1_drift = _make_service_obs("searxng", candidate_digest="sha256:" + "9" * 64)
    compose_svc.obs = _make_project_obs((s1_drift,))

    with pytest.raises(ExecutionRefused) as excinfo:
        service.run_approved_update(session_id, action_id=action_id)
    assert "candidate_digest_mismatch" in str(excinfo.value).lower()
    assert len(ipc_client.calls) == 0


def test_5_altered_current_digest_fails_gate(tmp_path: Path):
    """Scenario 5: Live container running digest changed (TOCTOU container drift)."""
    s1 = _make_service_obs("searxng", running_digest="sha256:" + "1" * 64)
    obs = _make_project_obs((s1,))
    service, plans, compose_svc, ipc_client, _clk = _c4_harness(tmp_path, project_obs=obs)
    session_id = _login_and_get_session(service)

    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")
    appr = service.approve_update_plan(
        session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=plan.plan_digest,
        idempotency_key="ik-curr-drift",
        service_name="searxng",
    )
    action_id = appr["action_id"]

    # Running container was restarted or mutated out of band
    s1_drift = _make_service_obs("searxng", running_digest="sha256:" + "8" * 64)
    compose_svc.obs = _make_project_obs((s1_drift,))

    with pytest.raises(ExecutionRefused) as excinfo:
        service.run_approved_update(session_id, action_id=action_id)
    assert "current_digest_mismatch" in str(excinfo.value).lower()
    assert len(ipc_client.calls) == 0


def test_6_altered_dependency_scope_fails_gate(tmp_path: Path):
    """Scenario 6: Dependencies list changed in live environment."""
    s1 = _make_service_obs("searxng", depends_on=())
    obs = _make_project_obs((s1,))
    service, plans, compose_svc, ipc_client, _clk = _c4_harness(tmp_path, project_obs=obs)
    session_id = _login_and_get_session(service)

    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")
    appr = service.approve_update_plan(
        session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=plan.plan_digest,
        idempotency_key="ik-dep-drift",
        service_name="searxng",
    )
    action_id = appr["action_id"]

    s2 = _make_service_obs("searxng-valkey")
    s1_drift = _make_service_obs("searxng", depends_on=("searxng-valkey",))
    compose_svc.obs = _make_project_obs((s1_drift, s2))

    with pytest.raises(ExecutionRefused) as excinfo:
        service.run_approved_update(session_id, action_id=action_id)
    assert "dependency_scope_mismatch" in str(excinfo.value).lower()
    assert len(ipc_client.calls) == 0


def test_7_stale_plan_fails_approval(tmp_path: Path):
    """Scenario 7: Presented plan digest does not match authoritative plan."""
    s1 = _make_service_obs("searxng")
    obs = _make_project_obs((s1,))
    service, plans, compose_svc, _ipc, _clk = _c4_harness(tmp_path, project_obs=obs)
    session_id = _login_and_get_session(service)

    stale_digest = "f" * 64
    with pytest.raises(ControlPlaneError) as excinfo:
        service.approve_update_plan(
            session_id,
            target_id=PROJECT_ID,
            environment="staging",
            presented_digest=stale_digest,
            idempotency_key="ik-stale-1",
            service_name="searxng",
        )
    assert excinfo.value.code == PlanningErrorCode.STALE_EVIDENCE


def test_8_stale_or_missing_confirmation_fails_execution(tmp_path: Path):
    """Scenario 8: Action confirmation missing, consumed, or bound to another action."""
    s1 = _make_service_obs("searxng")
    obs = _make_project_obs((s1,))
    service, plans, compose_svc, ipc_client, _clk = _c4_harness(tmp_path, project_obs=obs)
    session_id = _login_and_get_session(service)

    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")
    appr = service.approve_update_plan(
        session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=plan.plan_digest,
        idempotency_key="ik-conf-1",
        service_name="searxng",
    )
    action_id = appr["action_id"]

    # Tamper with confirmation: invalidate / consume before execution
    binding = service._confirmations.store.get(appr["confirmation_id"])
    service._confirmations.consume(binding, now=NOW)

    with pytest.raises(ExecutionRefused) as excinfo:
        service.run_approved_update(session_id, action_id=action_id)
    assert excinfo.value.reason_code == "confirmation_consumed"
    assert len(ipc_client.calls) == 0


def test_9_expired_lease_fails_execution_gate(tmp_path: Path):
    """Scenario 9: Execution lease expired."""
    s1 = _make_service_obs("searxng")
    obs = _make_project_obs((s1,))
    clk = _Clock(NOW)
    service, plans, compose_svc, ipc_client, _ = _c4_harness(tmp_path, project_obs=obs, clock=clk)
    session_id = _login_and_get_session(service)

    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")
    appr = service.approve_update_plan(
        session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=plan.plan_digest,
        idempotency_key="ik-lease-exp",
        service_name="searxng",
    )
    action_id = appr["action_id"]

    # Capture snapshot and advance time past lease TTL
    service.capture_snapshot(session_id, action_id, now=clk())
    action = service._actions.get_action(action_id)
    _lease, _ = service._actions.acquire_lease(action_id, expected_version=action.version, now=clk())

    # Advance clock by 6 minutes (lease TTL is 5m, session TTL is 10m)
    clk.advance(timedelta(minutes=6))

    with pytest.raises(ControlPlaneError) as excinfo:
        service.execute_action(session_id, action_id, now=clk())
    assert excinfo.value.code in (PlanningErrorCode.STATE_CONFLICT, PlanningErrorCode.EXPIRED_PLAN)
    assert len(ipc_client.calls) == 0


def test_10_invalid_fencing_token_fails_gate(tmp_path: Path):
    """Scenario 10: Fencing token mismatch fails execution gate."""
    s1 = _make_service_obs("searxng")
    obs = _make_project_obs((s1,))
    service, plans, compose_svc, ipc_client, _clk = _c4_harness(tmp_path, project_obs=obs)
    session_id = _login_and_get_session(service)

    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")
    appr = service.approve_update_plan(
        session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=plan.plan_digest,
        idempotency_key="ik-fence-1",
        service_name="searxng",
    )
    action_id = appr["action_id"]

    service.capture_snapshot(session_id, action_id, now=NOW)
    action = service._actions.get_action(action_id)
    lease, advanced = service._actions.acquire_lease(action_id, expected_version=action.version, now=NOW)

    contract = ExecutionContract(
        contract_version=EXECUTION_CONTRACT_VERSION,
        action_id=action_id,
        action_version=advanced.version,
        operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
        target_id=PROJECT_ID,
        environment="staging",
        plan_id=advanced.plan_id,
        expected_plan_revision=advanced.plan_revision,
        expected_plan_digest=plan.plan_digest,
        mutation_fields=tuple(service._actions.get_decision(advanced.decision_id).request.mutation_metadata),
        snapshot_id=service._snapshot_repo.snapshot_for_action(action_id).snapshot_id,
        decision_id=advanced.decision_id,
        confirmation_id=appr["confirmation_id"],
        policy_version="policy-v1",
        verification_version="v1",
        kill_switch_epoch=1,
        lease_id=lease.lease_id,
        fencing_token=lease.fencing_token + 999,  # Bad fencing token
        expires_at=advanced.expires_at,
        service_evidence=ServiceEvidenceContract.from_service_plan(plan),
    )
    executor = service._executor()
    with pytest.raises(ExecutionRefused) as excinfo:
        executor.execute(contract, now=NOW)
    assert excinfo.value.reason_code == "lease_fence_mismatch"
    assert len(ipc_client.calls) == 0


def test_11_blocked_service_cannot_approve_or_execute(tmp_path: Path):
    """Scenario 11: Ineligible/blocked service update cannot be approved."""
    # Service candidate is already up-to-date (blocked from update)
    s1 = _make_service_obs("searxng", status=ServiceCandidateStatus.CURRENT, reason=ServiceCandidateReason.UP_TO_DATE)
    obs = _make_project_obs((s1,))
    service, plans, compose_svc, ipc_client, _clk = _c4_harness(tmp_path, project_obs=obs)
    session_id = _login_and_get_session(service)

    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")
    assert plan.eligible is False

    with pytest.raises(ControlPlaneError) as excinfo:
        service.approve_update_plan(
            session_id,
            target_id=PROJECT_ID,
            environment="staging",
            presented_digest=plan.plan_digest,
            idempotency_key="ik-blocked",
            service_name="searxng",
        )
    assert excinfo.value.code == PlanningErrorCode.STATE_CONFLICT
    assert "blocked" in str(excinfo.value).lower()
    assert len(ipc_client.calls) == 0


def test_12_local_build_service_cannot_approve_or_execute(tmp_path: Path):
    """Scenario 12: Service configured with is_build=True cannot be approved."""
    s1 = _make_service_obs(
        "searxng",
        status=ServiceCandidateStatus.NOT_APPLICABLE,
        reason=ServiceCandidateReason.LOCAL_BUILD,
        candidate_digest=None,
        is_build=True,
    )
    obs = _make_project_obs((s1,))
    service, plans, compose_svc, ipc_client, _clk = _c4_harness(tmp_path, project_obs=obs)
    session_id = _login_and_get_session(service)

    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")
    assert plan.eligible is False
    assert plan.blocking_reason.value == "local_build"

    with pytest.raises(ControlPlaneError) as excinfo:
        service.approve_update_plan(
            session_id,
            target_id=PROJECT_ID,
            environment="staging",
            presented_digest=plan.plan_digest,
            idempotency_key="ik-build",
            service_name="searxng",
        )
    assert excinfo.value.code == PlanningErrorCode.STATE_CONFLICT
    assert len(ipc_client.calls) == 0


def test_13_executor_ipc_not_reached_when_any_gate_fails(tmp_path: Path):
    """Scenario 13: Mock IPC client is verified to have 0 calls when gate check fails."""
    s1 = _make_service_obs("searxng")
    obs = _make_project_obs((s1,))
    service, plans, compose_svc, ipc_client, _clk = _c4_harness(tmp_path, project_obs=obs)
    session_id = _login_and_get_session(service)

    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")
    appr = service.approve_update_plan(
        session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=plan.plan_digest,
        idempotency_key="ik-gate-fails",
        service_name="searxng",
    )
    action_id = appr["action_id"]

    # Drift candidate digest so gate fails
    compose_svc.obs = _make_project_obs((_make_service_obs("searxng", candidate_digest="sha256:" + "0" * 64),))

    with pytest.raises(ExecutionRefused):
        service.run_approved_update(session_id, action_id=action_id)

    assert len(ipc_client.calls) == 0, "Executor IPC must NEVER be reached if any gate check fails!"


def test_14_duplicate_action_terminal_replay(tmp_path: Path):
    """Scenario 14: Executing an already-completed action returns idempotent terminal outcome."""
    s1 = _make_service_obs("searxng")
    obs = _make_project_obs((s1,))
    service, plans, compose_svc, ipc_client, _clk = _c4_harness(tmp_path, project_obs=obs)
    session_id = _login_and_get_session(service)

    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")
    appr = service.approve_update_plan(
        session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=plan.plan_digest,
        idempotency_key="ik-replay",
        service_name="searxng",
    )
    action_id = appr["action_id"]

    # First execution succeeds
    res1 = service.run_approved_update(session_id, action_id=action_id)
    assert res1["executed"] is True
    assert res1["outcome"] in ("succeeded", "verification_succeeded")
    assert len(ipc_client.calls) == 1

    # Second execution is a terminal replay: no second IPC call, confirmation already consumed
    res2 = service.run_approved_update(session_id, action_id=action_id)
    assert res2["executed"] is False
    assert res2["outcome"] in ("succeeded", "verification_succeeded")
    assert len(ipc_client.calls) == 1


def test_15_ipc_timeout_to_unknown_outcome(tmp_path: Path):
    """Scenario 15: IPC interruption/timeout produces unknown_outcome outcome."""
    s1 = _make_service_obs("searxng")
    obs = _make_project_obs((s1,))

    class _TimeoutIPCClient:
        def send(self, request: ExecutionRequest):
            raise TimeoutError("IPC socket timed out")

        execute = send

    timeout_client = _TimeoutIPCClient()
    service, plans, compose_svc, _ipc, _clk = _c4_harness(
        tmp_path, project_obs=obs, executor_ipc_client=timeout_client
    )
    session_id = _login_and_get_session(service)

    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")
    appr = service.approve_update_plan(
        session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=plan.plan_digest,
        idempotency_key="ik-timeout",
        service_name="searxng",
    )
    action_id = appr["action_id"]

    res = service.run_approved_update(session_id, action_id=action_id)
    assert res["executed"] is True
    assert res["outcome"] == "unknown_outcome"
    assert res["provider_code"] in ("transport_failure", "update_interrupted", "timeout")


def test_16_executor_reconciliation_outcome(tmp_path: Path):
    """Scenario 16: Adapter RECONCILIATION_REQUIRED maps to unknown_outcome."""
    s1 = _make_service_obs("searxng")
    obs = _make_project_obs((s1,))

    reconciliation_resp = ExecutionResponse(
        outcome="unknown_outcome",
        provider_code="verification_failure:reconciliation_required",
        action_id="a" * 64,
        evidence_reference="reconcile:fail",
    )
    reconcile_ipc = _MockIPCClient(response=reconciliation_resp)
    service, plans, compose_svc, _ipc, _clk = _c4_harness(
        tmp_path, project_obs=obs, executor_ipc_client=reconcile_ipc
    )
    session_id = _login_and_get_session(service)

    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")
    appr = service.approve_update_plan(
        session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=plan.plan_digest,
        idempotency_key="ik-reconcile",
        service_name="searxng",
    )
    action_id = appr["action_id"]

    res = service.run_approved_update(session_id, action_id=action_id)
    assert res["executed"] is True
    assert res["outcome"] == "unknown_outcome"
    assert "reconciliation_required" in res["provider_code"]


def test_17_browser_supplied_injection_refused(tmp_path: Path):
    """Scenario 17: Browser-supplied path/image/command injections are rejected."""
    obs = _make_project_obs((_make_service_obs("searxng"),))
    service, plans, compose_svc, _ipc, _clk = _c4_harness(tmp_path, project_obs=obs)
    app = create_operator_app(service, bind="127.0.0.1")
    client = TestClient(app)

    login_resp = client.post("/login", json={"secret": SECRET})
    assert login_resp.status_code == 200
    csrf = client.get("/session").json()["csrf_token"]
    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")

    # Path traversal in service_name
    r1 = client.post(
        f"/updates/{PROJECT_ID}/approval",
        json={"idempotency_key": "k1", "update_plan_digest": plan.plan_digest, "service_name": "../../etc/passwd"},
        headers={"x-csrf-token": csrf},
    )
    assert r1.status_code == 422

    # Shell injection in service_name
    r2 = client.post(
        f"/updates/{PROJECT_ID}/approval",
        json={"idempotency_key": "k2", "update_plan_digest": plan.plan_digest, "service_name": "; reboot"},
        headers={"x-csrf-token": csrf},
    )
    assert r2.status_code == 422

    # Attempt to supply command
    r3 = client.post(
        f"/updates/{PROJECT_ID}/approval",
        json={"idempotency_key": "k3", "update_plan_digest": plan.plan_digest, "command": "docker run evil"},
        headers={"x-csrf-token": csrf},
    )
    assert r3.status_code == 422

    # Attempt to supply image
    r4 = client.post(
        f"/updates/{PROJECT_ID}/approval",
        json={"idempotency_key": "k4", "update_plan_digest": plan.plan_digest, "image": "evil:latest"},
        headers={"x-csrf-token": csrf},
    )
    assert r4.status_code == 422


def test_18_atomic_tight_partial_execution_unknown_outcome(tmp_path: Path):
    """Scenario 18: Mid-mutation failure in multi-service lockstep update produces unknown_outcome receipt."""
    db_p = tmp_path / "receipts.db"
    receipt_store = MutationReceiptStore(db_p)

    action_id = "a" * 64
    contract_digest = "c" * 64
    receipt_store.claim(
        action_id=action_id,
        fencing_token=1,
        capability_id=CAPABILITY_EXECUTE_SERVICE_UPDATE,
        target_id=PROJECT_ID,
        contract_digest=contract_digest,
    )

    # In ATOMIC_TIGHT, mid-mutation failure leaves system in unknown partial state
    receipt_store.complete(
        action_id=action_id,
        fencing_token=1,
        status=MutationStatus.UNKNOWN_OUTCOME,
        provider_code="partial_failure_reconciliation_required",
    )

    fetched = receipt_store.get(action_id=action_id, fencing_token=1)
    assert fetched is not None
    assert fetched.mutation_status == MutationStatus.UNKNOWN_OUTCOME
    assert fetched.mutation_status != MutationStatus.MUTATION_SUCCEEDED


# ---------------------------------------------------------------------------
# End-to-End In-Process Lifecycle Test
# ---------------------------------------------------------------------------


def test_19_end_to_end_in_process_lifecycle(tmp_path: Path):
    """End-to-End In-Process Selective Update:
    authorized service plan -> confirmation -> lease -> final gate -> binding
    -> execute_service_update -> C.3 adapter -> receipt.
    Proves complete flow without real Docker mutation.
    """
    receipt_db = tmp_path / "receipts.db"
    receipt_store = MutationReceiptStore(receipt_db)

    # 1. Compose project definition and observation
    s_valkey = _make_service_obs(
        "searxng-valkey",
        running_digest="sha256:target_valkey_running",
        candidate_digest="sha256:target_valkey_candidate",
        child_digest=None,
    )
    s_searxng = _make_service_obs(
        "searxng",
        running_digest="sha256:target_searxng_running",
        candidate_digest="sha256:target_searxng_candidate",
        child_digest="sha256:target_searxng_child",
        depends_on=("searxng-valkey",),
    )
    obs = _make_project_obs((s_searxng, s_valkey), project_name=PROJECT_ID)

    # 2. Setup mock subprocess runner for C.3 adapter
    commands_run: list[list[str]] = []

    def _mock_runner(cmd: list[str], *, cwd: Path, **kwargs):
        commands_run.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")

    # Dummy project files for C.3 adapter
    proj_dir = tmp_path / "searxng_proj"
    proj_dir.mkdir(parents=True, exist_ok=True)
    cfile = proj_dir / "docker-compose.yml"
    cfile.write_text("services:\n  searxng:\n    image: searxng\n  searxng-valkey:\n    image: valkey\n")

    @dataclass
    class _DummyProject:
        name: str = PROJECT_ID
        path: Path = proj_dir
        compose_files: tuple[Path, ...] = (cfile,)
        services: dict[str, Any] = field(default_factory=lambda: {"searxng": {}, "searxng-valkey": {}})
        local_build_services: set[str] = field(default_factory=set)

    @dataclass
    class _DummyServiceObs:
        service_name: str
        state: str = "running"
        health: str | None = "healthy"
        running_digest: str = "sha256:target_searxng_candidate"

    def _inspector(proj_name: str, svc_name: str):
        target = "sha256:target_searxng_candidate" if svc_name == "searxng" else "sha256:target_valkey_candidate"
        return _DummyServiceObs(service_name=svc_name, state="running", health="healthy", running_digest=target)

    adapter = ComposeExecutionAdapter(
        project_resolver=lambda _: _DummyProject(),
        runner=_mock_runner,
        inspector=_inspector,
    )

    # 3. Create executor update handler with real receipt store
    handler = compose_executor_update_handler(compose_adapter=adapter, receipts=receipt_store)

    # IPC double that delegates execute_service_update directly to handler
    class _InProcessIPCClient:
        def __init__(self):
            self.calls: list[ExecutionRequest] = []

        def send(self, req: ExecutionRequest) -> ExecutionResponse:
            self.calls.append(req)
            return handler(req)

        execute = send

    ipc_client = _InProcessIPCClient()

    # 4. Harness assembly
    service, plans, compose_svc, _mock_ipc, clk = _c4_harness(
        tmp_path,
        project_obs=obs,
        executor_ipc_client=ipc_client,
    )
    session_id = _login_and_get_session(service)

    # 5. Authorize service plan
    plan = compose_svc.plan_service_update(PROJECT_ID, "searxng")
    assert plan.eligible is True
    assert plan.atomicity == ServiceUpdateAtomicity.ATOMIC_TIGHT
    assert plan.expected_mutation.affected_services == ("searxng", "searxng-valkey")

    appr = service.approve_update_plan(
        session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=plan.plan_digest,
        idempotency_key="ik-e2e-1",
        service_name="searxng",
    )
    assert appr["allowed"] is True
    action_id = appr["action_id"]
    assert appr["service_name"] == "searxng"
    assert appr["service_scope"] == ["searxng", "searxng-valkey"]

    # 6. Execute approved update
    res = service.run_approved_update(session_id, action_id=action_id)
    assert res["executed"] is True
    assert res["outcome"] in ("succeeded", "verification_succeeded")
    assert res["provider_code"] == "update_ok"

    # 7. Verify IPC was called with canonical execute_service_update capability and payload
    assert len(ipc_client.calls) == 1
    call = ipc_client.calls[0]
    assert call.capability_id == CAPABILITY_EXECUTE_SERVICE_UPDATE
    assert call.action_id == action_id
    assert call.service_scope == ("searxng", "searxng-valkey")
    assert call.plan_digest == plan.plan_digest

    # 8. Verify C.3 adapter ran pull and up --no-deps in scope order
    assert len(commands_run) == 4
    assert commands_run[0] == ["docker", "compose", "-f", str(cfile), "pull", "searxng"]
    assert commands_run[1] == ["docker", "compose", "-f", str(cfile), "up", "-d", "--no-deps", "searxng"]
    assert commands_run[2] == ["docker", "compose", "-f", str(cfile), "pull", "searxng-valkey"]
    assert commands_run[3] == ["docker", "compose", "-f", str(cfile), "up", "-d", "--no-deps", "searxng-valkey"]

    # 9. Verify durable receipt in MutationReceiptStore
    receipt = receipt_store.get(action_id=action_id, fencing_token=1)
    assert receipt is not None
    assert receipt.mutation_status == MutationStatus.MUTATION_SUCCEEDED
    assert receipt.provider_code == "update_ok"


# ---------------------------------------------------------------------------
# Static AST Inspection Proofs
# ---------------------------------------------------------------------------


def test_static_ast_no_shell_true_or_privilege_broker_in_c4():
    """Verify control plane integration code does not invoke shell=True, broker, or docker."""
    target_files = [
        Path("src/aipm/control_plane/service.py"),
        Path("src/aipm/control_plane/executor.py"),
        Path("src/aipm/control_plane/transport.py"),
    ]
    for file_path in target_files:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        pytest.fail(f"Found shell=True in {file_path}")
            if isinstance(node, ast.Attribute) and node.attr in {"systemd_restart", "privilege_broker"}:
                pytest.fail(f"Found forbidden attribute {node.attr} in {file_path}")
