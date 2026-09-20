"""Deterministic unit, boundary, and security tests for MC-6.15-C.2:
Compose Service Plan Evidence and TOCTOU Gate Integration.

Verifies:
1. PLAN IDENTITY & NOISE INVARIANCE
2. DEPENDENCY & SCOPE TOPOLOGY (searxng + redis lockstep, cycle, unhealthy)
3. MULTI-ARCH & PLATFORM BINDING (linux/arm64 target verification, child digests)
4. AUTHORIZATION & CONTROL PLANE GATE BINDINGS (digests, confirmations, leases, fencing)
5. SECURITY & ADVERSARIAL INPUT RESILIENCE (injection, arbitrary parameters)
6. STATIC PROOF: NO MUTATION AUTHORITY IN C.2 CODE
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from aipm.control_plane.gate import (
    FinalExecutionGate,
    GateCode,
    verify_service_evidence,
)
from aipm.control_plane.executor import (
    ExecutionContract,
    ExecutorCapability,
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
from aipm.services.compose.planner import ComposeServiceUpdatePlanner


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
PLAN_DIGEST_A = "a" * 64
PLAN_DIGEST_B = "b" * 64
ACTION_ID = "c" * 64
CONFIRMATION_ID = "d" * 32
LEASE_ID = "e" * 32


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
    container_ids: tuple[str, ...] = ("c101",),
    observed_at: datetime | None = None,
    arch: str = "arm64",
    os_name: str = "linux",
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
        target_os=os_name,
        target_arch=arch,
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
        candidate_detail="Normal test candidate",
        is_build=False,
        build_context=None,
        build_dockerfile=None,
        ports=("8080/tcp",),
        freshness=freshness,
        observed_at=observed_at or NOW,
        provenance_verified=provenance_verified,
        depends_on=depends_on,
    )


def _make_project_obs(
    services: dict[str, ComposeServiceObservation] | tuple[ComposeServiceObservation, ...],
    project_name: str = "searxng-stack",
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


def _build_plan(
    obs: ComposeProjectObservation,
    service_name: str = "searxng",
) -> ServiceUpdatePlan:
    planner = ComposeServiceUpdatePlanner()
    return planner.plan_service(obs, service_name, project_id=obs.project_name)


# ---------------------------------------------------------------------------
# 1. PLAN IDENTITY & NOISE INVARIANCE
# ---------------------------------------------------------------------------

def test_plan_identity_same_observation_same_digest():
    svc = _make_service_obs("searxng")
    proj = _make_project_obs({"searxng": svc})
    plan1 = _build_plan(proj, "searxng")
    plan2 = _build_plan(proj, "searxng")
    assert plan1.plan_digest == plan2.plan_digest

    ev1 = ServiceEvidenceContract.from_service_plan(plan1)
    ev2 = ServiceEvidenceContract.from_service_plan(plan2)
    assert ev1 == ev2
    assert verify_service_evidence(ev1, ev2, expected_digest=plan1.plan_digest) is GateCode.ALLOWED


def test_plan_identity_noise_invariance():
    """Noise in timestamps, PIDs, container IDs, or observation times must not alter evidence or reject the gate."""
    svc1 = _make_service_obs("searxng", container_ids=("c101",), observed_at=NOW)
    svc2 = _make_service_obs(
        "searxng",
        container_ids=("c999", "c888"),  # different container IDs
        observed_at=NOW + timedelta(hours=3),  # different timestamp
    )

    proj1 = _make_project_obs({"searxng": svc1})
    proj2 = _make_project_obs({"searxng": svc2})

    plan1 = _build_plan(proj1, "searxng")
    plan2 = _build_plan(proj2, "searxng")

    assert plan1.plan_digest == plan2.plan_digest
    ev1 = ServiceEvidenceContract.from_service_plan(plan1)
    ev2 = ServiceEvidenceContract.from_service_plan(plan2)
    assert ev1.plan_digest == ev2.plan_digest
    assert verify_service_evidence(ev1, ev2, expected_digest=plan1.plan_digest) is GateCode.ALLOWED


def test_current_digest_change_fails_closed():
    svc1 = _make_service_obs("searxng", running_digest="sha256:" + "1" * 64)
    plan1 = _build_plan(_make_project_obs({"searxng": svc1}))
    ev1 = ServiceEvidenceContract.from_service_plan(plan1)

    # State changed externally (e.g. image restarted on different digest)
    svc2 = _make_service_obs("searxng", running_digest="sha256:" + "f" * 64)
    plan2 = _build_plan(_make_project_obs({"searxng": svc2}))
    ev2 = ServiceEvidenceContract.from_service_plan(plan2)

    decision = verify_service_evidence(ev1, ev2, expected_digest=plan1.plan_digest)
    assert decision is GateCode.CURRENT_DIGEST_MISMATCH


def test_candidate_digest_change_fails_closed():
    svc1 = _make_service_obs("searxng", candidate_digest="sha256:" + "2" * 64)
    plan1 = _build_plan(_make_project_obs({"searxng": svc1}))
    ev1 = ServiceEvidenceContract.from_service_plan(plan1)

    # Upstream registry published a newer candidate digest
    svc2 = _make_service_obs("searxng", candidate_digest="sha256:" + "9" * 64)
    plan2 = _build_plan(_make_project_obs({"searxng": svc2}))
    ev2 = ServiceEvidenceContract.from_service_plan(plan2)

    decision = verify_service_evidence(ev1, ev2, expected_digest=plan1.plan_digest)
    assert decision is GateCode.CANDIDATE_DIGEST_MISMATCH


def test_candidate_lookup_key_change_fails_closed():
    svc1 = _make_service_obs("searxng", arch="arm64")
    plan1 = _build_plan(_make_project_obs({"searxng": svc1}))
    ev1 = ServiceEvidenceContract.from_service_plan(plan1)

    svc2 = _make_service_obs("searxng", arch="amd64")
    plan2 = _build_plan(_make_project_obs({"searxng": svc2}))
    ev2 = ServiceEvidenceContract.from_service_plan(plan2)

    decision = verify_service_evidence(ev1, ev2, expected_digest=plan1.plan_digest)
    assert decision is GateCode.CANDIDATE_DIGEST_MISMATCH


def test_service_identity_change_fails_closed():
    svc1 = _make_service_obs("searxng")
    plan1 = _build_plan(_make_project_obs({"searxng": svc1}), "searxng")
    ev1 = ServiceEvidenceContract.from_service_plan(plan1)

    svc2 = _make_service_obs("redis")
    plan2 = _build_plan(_make_project_obs({"redis": svc2}), "redis")
    ev2 = ServiceEvidenceContract.from_service_plan(plan2)

    decision = verify_service_evidence(ev1, ev2, expected_digest=plan1.plan_digest)
    assert decision is GateCode.PLAN_IDENTITY_MISMATCH


def test_project_identity_change_fails_closed():
    svc1 = _make_service_obs("searxng")
    proj1 = _make_project_obs({"searxng": svc1}, project_name="searxng-stack")
    plan1 = _build_plan(proj1, "searxng")
    ev1 = ServiceEvidenceContract.from_service_plan(plan1)

    proj2 = _make_project_obs({"searxng": svc1}, project_name="other-stack")
    plan2 = _build_plan(proj2, "searxng")
    ev2 = ServiceEvidenceContract.from_service_plan(plan2)

    decision = verify_service_evidence(ev1, ev2, expected_digest=plan1.plan_digest)
    assert decision is GateCode.PLAN_IDENTITY_MISMATCH


def test_compose_identity_change_fails_closed():
    svc1 = _make_service_obs("searxng")
    proj1 = _make_project_obs({"searxng": svc1}, compose_identity="cid-1")
    plan1 = _build_plan(proj1, "searxng")
    ev1 = ServiceEvidenceContract.from_service_plan(plan1)

    proj2 = _make_project_obs({"searxng": svc1}, compose_identity="cid-2")
    plan2 = _build_plan(proj2, "searxng")
    ev2 = ServiceEvidenceContract.from_service_plan(plan2)

    decision = verify_service_evidence(ev1, ev2, expected_digest=plan1.plan_digest)
    assert decision is GateCode.PLAN_IDENTITY_MISMATCH


def test_atomicity_change_fails_closed():
    svc = _make_service_obs("searxng")
    plan = _build_plan(_make_project_obs({"searxng": svc}))
    ev_auth = ServiceEvidenceContract.from_service_plan(plan)

    ev_fresh = ServiceEvidenceContract(
        project_name=ev_auth.project_name,
        compose_identity=ev_auth.compose_identity,
        service_name=ev_auth.service_name,
        requested_scope=ev_auth.requested_scope,
        derived_execution_scope=ev_auth.derived_execution_scope,
        atomicity="blocked",  # atomicity changed!
        current_runtime_digest=ev_auth.current_runtime_digest,
        target_candidate_digest=ev_auth.target_candidate_digest,
        target_candidate_child_digest=ev_auth.target_candidate_child_digest,
        candidate_lookup_key=ev_auth.candidate_lookup_key,
        provenance_verified=ev_auth.provenance_verified,
        dependency_scope=ev_auth.dependency_scope,
        health_contract_summary=ev_auth.health_contract_summary,
        plan_digest=ev_auth.plan_digest,
    )
    decision = verify_service_evidence(ev_auth, ev_fresh, expected_digest=ev_auth.plan_digest)
    assert decision is GateCode.PLAN_IDENTITY_MISMATCH


def test_health_contract_change_fails_closed():
    svc = _make_service_obs("searxng")
    plan = _build_plan(_make_project_obs({"searxng": svc}))
    ev_auth = ServiceEvidenceContract.from_service_plan(plan)

    ev_fresh = ServiceEvidenceContract(
        project_name=ev_auth.project_name,
        compose_identity=ev_auth.compose_identity,
        service_name=ev_auth.service_name,
        requested_scope=ev_auth.requested_scope,
        derived_execution_scope=ev_auth.derived_execution_scope,
        atomicity=ev_auth.atomicity,
        current_runtime_digest=ev_auth.current_runtime_digest,
        target_candidate_digest=ev_auth.target_candidate_digest,
        target_candidate_child_digest=ev_auth.target_candidate_child_digest,
        candidate_lookup_key=ev_auth.candidate_lookup_key,
        provenance_verified=ev_auth.provenance_verified,
        dependency_scope=ev_auth.dependency_scope,
        health_contract_summary="svc:searxng|state:running|health:healthy|timeout:120|deps:none",  # timeout changed!
        plan_digest=ev_auth.plan_digest,
    )
    decision = verify_service_evidence(ev_auth, ev_fresh, expected_digest=ev_auth.plan_digest)
    assert decision is GateCode.HEALTH_CONTRACT_MISMATCH


# ---------------------------------------------------------------------------
# 2. DEPENDENCY & ATOMICITY TOPOLOGY
# ---------------------------------------------------------------------------

def test_searxng_atomic_lockstep_accepted():
    """searxng depending on valkey produces lockstep atomicity and matching dependency scope."""
    valkey = _make_service_obs("searxng-valkey", status=ServiceCandidateStatus.UPDATE_AVAILABLE)
    searxng = _make_service_obs("searxng", status=ServiceCandidateStatus.UPDATE_AVAILABLE, depends_on=("searxng-valkey",))
    proj = _make_project_obs({"searxng": searxng, "searxng-valkey": valkey})

    plan = _build_plan(proj, "searxng")
    assert plan.atomicity is ServiceUpdateAtomicity.ATOMIC_TIGHT
    assert "searxng-valkey" in plan.expected_mutation.affected_services

    ev = ServiceEvidenceContract.from_service_plan(plan)
    assert verify_service_evidence(ev, ev, expected_digest=plan.plan_digest) is GateCode.ALLOWED


def test_searxng_scope_widened_rejected():
    valkey = _make_service_obs("searxng-valkey", status=ServiceCandidateStatus.UPDATE_AVAILABLE)
    searxng = _make_service_obs("searxng", status=ServiceCandidateStatus.UPDATE_AVAILABLE, depends_on=("searxng-valkey",))
    plan = _build_plan(_make_project_obs({"searxng": searxng, "searxng-valkey": valkey}), "searxng")
    ev_auth = ServiceEvidenceContract.from_service_plan(plan)

    # Live environment added another affected service
    ev_fresh = ServiceEvidenceContract(
        project_name=ev_auth.project_name,
        compose_identity=ev_auth.compose_identity,
        service_name=ev_auth.service_name,
        requested_scope=ev_auth.requested_scope,
        derived_execution_scope=ev_auth.derived_execution_scope + ("nginx",),  # widened!
        atomicity=ev_auth.atomicity,
        current_runtime_digest=ev_auth.current_runtime_digest,
        target_candidate_digest=ev_auth.target_candidate_digest,
        target_candidate_child_digest=ev_auth.target_candidate_child_digest,
        candidate_lookup_key=ev_auth.candidate_lookup_key,
        provenance_verified=ev_auth.provenance_verified,
        dependency_scope=ev_auth.dependency_scope,
        health_contract_summary=ev_auth.health_contract_summary,
        plan_digest=ev_auth.plan_digest,
    )
    decision = verify_service_evidence(ev_auth, ev_fresh, expected_digest=ev_auth.plan_digest)
    assert decision is GateCode.DEPENDENCY_SCOPE_MISMATCH


def test_searxng_scope_narrowed_rejected():
    valkey = _make_service_obs("searxng-valkey", status=ServiceCandidateStatus.UPDATE_AVAILABLE)
    searxng = _make_service_obs("searxng", status=ServiceCandidateStatus.UPDATE_AVAILABLE, depends_on=("searxng-valkey",))
    plan = _build_plan(_make_project_obs({"searxng": searxng, "searxng-valkey": valkey}), "searxng")
    ev_auth = ServiceEvidenceContract.from_service_plan(plan)

    ev_fresh = ServiceEvidenceContract(
        project_name=ev_auth.project_name,
        compose_identity=ev_auth.compose_identity,
        service_name=ev_auth.service_name,
        requested_scope=ev_auth.requested_scope,
        derived_execution_scope=("searxng",),  # narrowed! lost valkey
        atomicity=ev_auth.atomicity,
        current_runtime_digest=ev_auth.current_runtime_digest,
        target_candidate_digest=ev_auth.target_candidate_digest,
        target_candidate_child_digest=ev_auth.target_candidate_child_digest,
        candidate_lookup_key=ev_auth.candidate_lookup_key,
        provenance_verified=ev_auth.provenance_verified,
        dependency_scope=ev_auth.dependency_scope,
        health_contract_summary=ev_auth.health_contract_summary,
        plan_digest=ev_auth.plan_digest,
    )
    decision = verify_service_evidence(ev_auth, ev_fresh, expected_digest=ev_auth.plan_digest)
    assert decision is GateCode.DEPENDENCY_SCOPE_MISMATCH


def test_dependency_unhealthy_rejected():
    valkey_healthy = _make_service_obs("searxng-valkey", status=ServiceCandidateStatus.CURRENT, reason=ServiceCandidateReason.UP_TO_DATE, health="healthy")
    searxng = _make_service_obs("searxng", depends_on=("searxng-valkey",))
    plan_auth = _build_plan(_make_project_obs({"searxng": searxng, "searxng-valkey": valkey_healthy}), "searxng")
    ev_auth = ServiceEvidenceContract.from_service_plan(plan_auth)

    # Live valkey became unhealthy
    valkey_unhealthy = _make_service_obs("searxng-valkey", status=ServiceCandidateStatus.CURRENT, reason=ServiceCandidateReason.UP_TO_DATE, health="unhealthy")
    plan_fresh = _build_plan(_make_project_obs({"searxng": searxng, "searxng-valkey": valkey_unhealthy}), "searxng")
    ev_fresh = ServiceEvidenceContract.from_service_plan(plan_fresh)

    decision = verify_service_evidence(ev_auth, ev_fresh, expected_digest=ev_auth.plan_digest)
    assert decision in (GateCode.DEPENDENCY_BLOCKED, GateCode.DEPENDENCY_SCOPE_MISMATCH)


def test_dependency_missing_rejected():
    searxng = _make_service_obs("searxng", depends_on=("missing-redis",))
    plan = _build_plan(_make_project_obs({"searxng": searxng}), "searxng")
    # Planner detects missing dependency and blocks plan eligibility
    assert plan.eligible is False
    assert plan.blocking_reason.value in ("missing_dependency_service", "dependency_blocked", "dependency_missing")


def test_dependency_cycle_rejected():
    svc_a = _make_service_obs("svc_a", depends_on=("svc_b",))
    svc_b = _make_service_obs("svc_b", depends_on=("svc_a",))
    plan = _build_plan(_make_project_obs({"svc_a": svc_a, "svc_b": svc_b}), "svc_a")
    assert plan.eligible is False


# ---------------------------------------------------------------------------
# 3. MULTI-ARCH & PLATFORM BINDING
# ---------------------------------------------------------------------------

def test_multiarch_arm64_target_verified():
    """Host architecture linux/arm64 must be verified via candidate_lookup_key and child digest."""
    svc = _make_service_obs(
        "searxng",
        arch="arm64",
        os_name="linux",
        child_digest="sha256:" + "3" * 64,
    )
    plan = _build_plan(_make_project_obs({"searxng": svc}))
    ev = ServiceEvidenceContract.from_service_plan(plan)

    assert "linux/arm64" in ev.candidate_lookup_key
    assert ev.target_candidate_child_digest == "sha256:" + "3" * 64
    assert verify_service_evidence(ev, ev, expected_digest=ev.plan_digest) is GateCode.ALLOWED


def test_multiarch_child_digest_mismatch_rejected():
    svc1 = _make_service_obs("searxng", child_digest="sha256:" + "3" * 64)
    plan1 = _build_plan(_make_project_obs({"searxng": svc1}))
    ev1 = ServiceEvidenceContract.from_service_plan(plan1)

    svc2 = _make_service_obs("searxng", child_digest="sha256:" + "4" * 64)
    plan2 = _build_plan(_make_project_obs({"searxng": svc2}))
    ev2 = ServiceEvidenceContract.from_service_plan(plan2)

    decision = verify_service_evidence(ev1, ev2, expected_digest=ev1.plan_digest)
    assert decision is GateCode.CANDIDATE_DIGEST_MISMATCH


def test_multiarch_provenance_invalid_rejected():
    svc = _make_service_obs("searxng", provenance_verified=False)
    plan = _build_plan(_make_project_obs({"searxng": svc}))
    ev = ServiceEvidenceContract.from_service_plan(plan)
    decision = verify_service_evidence(ev, ev, expected_digest=ev.plan_digest)
    assert decision is GateCode.PROVENANCE_INVALID


# ---------------------------------------------------------------------------
# 4. FINAL EXECUTION GATE INTEGRATION & AUTHORIZATION BINDINGS
# ---------------------------------------------------------------------------

class _FakeAction:
    def __init__(self, action_id: str, version: int = 1, state="leased", decision_id: str = "decision-01", action_protocol: str = "mc616d2-v1"):
        from aipm.control_plane.models import LifecycleState
        self.action_id = action_id
        self.version = version
        self.state = LifecycleState(state)
        self.decision_id = decision_id
        self.scope = type("Scope", (), {"policy_version": "policy-v1", "target_id": "searxng-stack"})()
        self.action_protocol = action_protocol

    def is_expired(self, now):
        return False


class _FakeConfirmationBinding:
    def __init__(self, confirmation_id: str, action_id: str, target_digest: str, state="confirmed"):
        self.confirmation_id = confirmation_id
        self.action_id = action_id
        self.target_digest = target_digest
        self.state = type("State", (), {"value": state})()

    def is_expired(self, now):
        return False


class _FakeLease:
    def __init__(self, lease_id: str, fencing_token: int, expires_at: datetime):
        self.lease_id = lease_id
        self.fencing_token = fencing_token
        self.expires_at = expires_at


class _FakeActionRepo:
    def __init__(self, action, contract_digest: str, lease: _FakeLease, decision=None):
        self._action = action
        self._contract_digest = contract_digest
        self._lease = lease
        self._decision = decision

    def get_action(self, action_id: str):
        return self._action if self._action and self._action.action_id == action_id else None

    def get_contract_evidence(self, action_id: str):
        return {"contract_digest": self._contract_digest, "capability_version": "1"}

    def active_lease(self, action_id: str, now: datetime = None):
        return self._lease

    def get_decision(self, decision_id: str):
        return self._decision


class _FakeConfirmations:
    def __init__(self, binding):
        self.store = {binding.confirmation_id: binding} if binding else {}


class _FakePlans:
    def read(self, target_id: str):
        raise NotImplementedError("Service evidence bypasses plan store")


def _make_test_contract(
    evidence: ServiceEvidenceContract,
    *,
    expected_digest: str | None = None,
    action_id: str = ACTION_ID,
    confirmation_id: str = CONFIRMATION_ID,
    lease_id: str = LEASE_ID,
    fencing_token: int = 1,
    mutation_fields: tuple[tuple[str, str], ...] = (("title", "Update searxng"),),
) -> ExecutionContract:
    digest = expected_digest or evidence.plan_digest
    return ExecutionContract(
        contract_version="mc612-execution-contract-v2",
        action_id=action_id,
        action_version=1,
        operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
        target_id="searxng-stack",
        environment="staging",
        plan_id="plan-searxng-01",
        expected_plan_revision=1,
        expected_plan_digest=digest,
        mutation_fields=mutation_fields,
        snapshot_id="snapshot-01",
        decision_id="decision-01",
        confirmation_id=confirmation_id,
        policy_version="policy-v1",
        verification_version="v1",
        kill_switch_epoch=1,
        lease_id=lease_id,
        fencing_token=fencing_token,
        expires_at=NOW + timedelta(minutes=10),
        capability_version="1",
        service_evidence=evidence,
    )


def test_final_execution_gate_service_evidence_allowed():
    svc = _make_service_obs("searxng")
    plan = _build_plan(_make_project_obs({"searxng": svc}))
    ev = ServiceEvidenceContract.from_service_plan(plan)

    contract = _make_test_contract(ev)
    contract_digest = contract.digest()

    action = _FakeAction(ACTION_ID)
    lease = _FakeLease(LEASE_ID, 1, NOW + timedelta(minutes=10))
    binding = _FakeConfirmationBinding(CONFIRMATION_ID, ACTION_ID, target_digest=ev.plan_digest)

    actions = _FakeActionRepo(action, contract_digest, lease)
    confirmations = _FakeConfirmations(binding)
    plans = _FakePlans()

    gate = FinalExecutionGate(
        actions=actions,
        plans=plans,
        confirmations=confirmations,
    )

    decision = gate.evaluate(contract, now=NOW)
    assert decision.allowed is True
    assert decision.reason is GateCode.ALLOWED


def test_final_execution_gate_wrong_plan_digest_denied():
    svc = _make_service_obs("searxng")
    plan = _build_plan(_make_project_obs({"searxng": svc}))
    ev = ServiceEvidenceContract.from_service_plan(plan)

    # Contract specifies expected digest A, but evidence is for plan B
    contract = _make_test_contract(ev, expected_digest="f" * 64)
    contract_digest = contract.digest()

    action = _FakeAction(ACTION_ID)
    lease = _FakeLease(LEASE_ID, 1, NOW + timedelta(minutes=10))
    binding = _FakeConfirmationBinding(CONFIRMATION_ID, ACTION_ID, target_digest="f" * 64)

    gate = FinalExecutionGate(
        actions=_FakeActionRepo(action, contract_digest, lease),
        plans=_FakePlans(),
        confirmations=_FakeConfirmations(binding),
    )

    decision = gate.evaluate(contract, now=NOW)
    assert decision.allowed is False
    assert decision.reason is GateCode.PLAN_IDENTITY_MISMATCH


def test_final_execution_gate_consumed_confirmation_denied():
    svc = _make_service_obs("searxng")
    plan = _build_plan(_make_project_obs({"searxng": svc}))
    ev = ServiceEvidenceContract.from_service_plan(plan)

    contract = _make_test_contract(ev)
    action = _FakeAction(ACTION_ID)
    lease = _FakeLease(LEASE_ID, 1, NOW + timedelta(minutes=10))
    # Confirmation is marked consumed
    binding = _FakeConfirmationBinding(CONFIRMATION_ID, ACTION_ID, target_digest=ev.plan_digest, state="consumed")

    gate = FinalExecutionGate(
        actions=_FakeActionRepo(action, contract.digest(), lease),
        plans=_FakePlans(),
        confirmations=_FakeConfirmations(binding),
    )

    decision = gate.evaluate(contract, now=NOW)
    assert decision.allowed is False
    assert decision.reason is GateCode.CONFIRMATION_CONSUMED


def test_final_execution_gate_expired_lease_denied():
    svc = _make_service_obs("searxng")
    plan = _build_plan(_make_project_obs({"searxng": svc}))
    ev = ServiceEvidenceContract.from_service_plan(plan)

    contract = _make_test_contract(ev)
    action = _FakeAction(ACTION_ID)
    # Lease expired
    lease = _FakeLease(LEASE_ID, 1, NOW - timedelta(seconds=1))
    binding = _FakeConfirmationBinding(CONFIRMATION_ID, ACTION_ID, target_digest=ev.plan_digest)

    gate = FinalExecutionGate(
        actions=_FakeActionRepo(action, contract.digest(), lease),
        plans=_FakePlans(),
        confirmations=_FakeConfirmations(binding),
    )

    decision = gate.evaluate(contract, now=NOW)
    assert decision.allowed is False
    assert decision.reason is GateCode.LEASE_EXPIRED


def test_final_execution_gate_wrong_fence_denied():
    svc = _make_service_obs("searxng")
    plan = _build_plan(_make_project_obs({"searxng": svc}))
    ev = ServiceEvidenceContract.from_service_plan(plan)

    contract = _make_test_contract(ev, fencing_token=5)
    action = _FakeAction(ACTION_ID)
    lease = _FakeLease(LEASE_ID, fencing_token=1, expires_at=NOW + timedelta(minutes=10))
    binding = _FakeConfirmationBinding(CONFIRMATION_ID, ACTION_ID, target_digest=ev.plan_digest)

    gate = FinalExecutionGate(
        actions=_FakeActionRepo(action, contract.digest(), lease),
        plans=_FakePlans(),
        confirmations=_FakeConfirmations(binding),
    )

    decision = gate.evaluate(contract, now=NOW)
    assert decision.allowed is False
    assert decision.reason is GateCode.LEASE_FENCE_MISMATCH


def test_final_execution_gate_wrong_action_denied():
    svc = _make_service_obs("searxng")
    plan = _build_plan(_make_project_obs({"searxng": svc}))
    ev = ServiceEvidenceContract.from_service_plan(plan)

    contract = _make_test_contract(ev, action_id="0" * 64)
    action = _FakeAction(ACTION_ID)
    lease = _FakeLease(LEASE_ID, 1, NOW + timedelta(minutes=10))
    binding = _FakeConfirmationBinding(CONFIRMATION_ID, ACTION_ID, target_digest=ev.plan_digest)

    gate = FinalExecutionGate(
        actions=_FakeActionRepo(action, contract.digest(), lease),
        plans=_FakePlans(),
        confirmations=_FakeConfirmations(binding),
    )

    decision = gate.evaluate(contract, now=NOW)
    assert decision.allowed is False
    assert decision.reason is GateCode.ACTION_MISSING


def test_final_execution_gate_wrong_confirmation_denied():
    svc = _make_service_obs("searxng")
    plan = _build_plan(_make_project_obs({"searxng": svc}))
    ev = ServiceEvidenceContract.from_service_plan(plan)

    contract = _make_test_contract(ev, confirmation_id="0" * 32)
    action = _FakeAction(ACTION_ID)
    lease = _FakeLease(LEASE_ID, 1, NOW + timedelta(minutes=10))
    binding = _FakeConfirmationBinding(CONFIRMATION_ID, ACTION_ID, target_digest=ev.plan_digest)

    gate = FinalExecutionGate(
        actions=_FakeActionRepo(action, contract.digest(), lease),
        plans=_FakePlans(),
        confirmations=_FakeConfirmations(binding),
    )

    decision = gate.evaluate(contract, now=NOW)
    assert decision.allowed is False
    assert decision.reason is GateCode.CONFIRMATION_MISSING


def test_final_execution_gate_expired_confirmation_denied():
    svc = _make_service_obs("searxng")
    plan = _build_plan(_make_project_obs({"searxng": svc}))
    ev = ServiceEvidenceContract.from_service_plan(plan)

    contract = _make_test_contract(ev)
    action = _FakeAction(ACTION_ID)
    lease = _FakeLease(LEASE_ID, 1, NOW + timedelta(minutes=10))
    binding = _FakeConfirmationBinding(CONFIRMATION_ID, ACTION_ID, target_digest=ev.plan_digest)
    binding.is_expired = lambda now: True  # expired confirmation!

    gate = FinalExecutionGate(
        actions=_FakeActionRepo(action, contract.digest(), lease),
        plans=_FakePlans(),
        confirmations=_FakeConfirmations(binding),
    )

    decision = gate.evaluate(contract, now=NOW)
    assert decision.allowed is False
    assert decision.reason is GateCode.CONFIRMATION_EXPIRED


def test_final_execution_gate_wrong_lease_denied():
    svc = _make_service_obs("searxng")
    plan = _build_plan(_make_project_obs({"searxng": svc}))
    ev = ServiceEvidenceContract.from_service_plan(plan)

    contract = _make_test_contract(ev, lease_id="0" * 32)
    action = _FakeAction(ACTION_ID)
    lease = _FakeLease(LEASE_ID, 1, NOW + timedelta(minutes=10))
    binding = _FakeConfirmationBinding(CONFIRMATION_ID, ACTION_ID, target_digest=ev.plan_digest)

    gate = FinalExecutionGate(
        actions=_FakeActionRepo(action, contract.digest(), lease),
        plans=_FakePlans(),
        confirmations=_FakeConfirmations(binding),
    )

    decision = gate.evaluate(contract, now=NOW)
    assert decision.allowed is False
    assert decision.reason is GateCode.LEASE_FENCE_MISMATCH


def test_metadata_tampering_narrow_scope_fails_closed():
    """Adversarial test: authorized plan has ["searxng", "searxng-valkey"],
    tampered metadata has ["searxng"] -> REJECT (DEPENDENCY_SCOPE_MISMATCH)."""
    valkey = _make_service_obs("searxng-valkey", status=ServiceCandidateStatus.UPDATE_AVAILABLE)
    searxng = _make_service_obs("searxng", status=ServiceCandidateStatus.UPDATE_AVAILABLE, depends_on=("searxng-valkey",))
    plan = _build_plan(_make_project_obs({"searxng": searxng, "searxng-valkey": valkey}), "searxng")
    ev = ServiceEvidenceContract.from_service_plan(plan)
    assert ev.derived_execution_scope == ("searxng", "searxng-valkey")

    contract = _make_test_contract(ev)
    action = _FakeAction(ACTION_ID, decision_id="decision-01")
    lease = _FakeLease(LEASE_ID, 1, NOW + timedelta(minutes=10))
    binding = _FakeConfirmationBinding(CONFIRMATION_ID, ACTION_ID, target_digest=ev.plan_digest)

    # Tampered decision metadata narrows scope to only searxng
    fake_decision = type("Decision", (), {
        "request": type("Request", (), {
            "metadata": (("title", "Update searxng"), ("service_scope", "searxng"))
        })()
    })()

    gate = FinalExecutionGate(
        actions=_FakeActionRepo(action, contract.digest(), lease, decision=fake_decision),
        plans=_FakePlans(),
        confirmations=_FakeConfirmations(binding),
    )

    decision = gate.evaluate(contract, now=NOW)
    assert decision.allowed is False
    assert decision.reason is GateCode.DEPENDENCY_SCOPE_MISMATCH


def test_metadata_tampering_widen_scope_fails_closed():
    """Adversarial test: authorized plan has ["ollama"],
    tampered metadata has ["ollama", "litellm"] -> REJECT (DEPENDENCY_SCOPE_MISMATCH)."""
    ollama = _make_service_obs("ollama", status=ServiceCandidateStatus.UPDATE_AVAILABLE)
    plan = _build_plan(_make_project_obs({"ollama": ollama}), "ollama")
    ev = ServiceEvidenceContract.from_service_plan(plan)
    assert ev.derived_execution_scope == ("ollama",)

    contract = _make_test_contract(ev)
    action = _FakeAction(ACTION_ID, decision_id="decision-01")
    lease = _FakeLease(LEASE_ID, 1, NOW + timedelta(minutes=10))
    binding = _FakeConfirmationBinding(CONFIRMATION_ID, ACTION_ID, target_digest=ev.plan_digest)

    # Tampered decision metadata widens scope to include litellm
    fake_decision = type("Decision", (), {
        "request": type("Request", (), {
            "metadata": (("title", "Update ollama"), ("service_scope", "ollama,litellm"))
        })()
    })()

    gate = FinalExecutionGate(
        actions=_FakeActionRepo(action, contract.digest(), lease, decision=fake_decision),
        plans=_FakePlans(),
        confirmations=_FakeConfirmations(binding),
    )

    decision = gate.evaluate(contract, now=NOW)
    assert decision.allowed is False
    assert decision.reason is GateCode.DEPENDENCY_SCOPE_MISMATCH


def test_update_execution_binding_rejects_invalid_scope():
    from aipm.control_plane.models import UpdateExecutionBinding

    # Non-tuple
    with pytest.raises(ValueError, match="Invalid service execution scope"):
        UpdateExecutionBinding(
            project_name="searxng-stack",
            plan_digest="a" * 64,
            confirmation_id="b" * 32,
            action_id="c" * 64,
            contract_digest="d" * 64,
            lease_id="e" * 32,
            fencing_token=1,
            action_protocol="mc616d2-v1",
            service_scope=["searxng"],  # list instead of tuple
        )

    # Empty service name in tuple
    with pytest.raises(ValueError, match="Invalid service execution scope"):
        UpdateExecutionBinding(
            project_name="searxng-stack",
            plan_digest="a" * 64,
            confirmation_id="b" * 32,
            action_id="c" * 64,
            contract_digest="d" * 64,
            lease_id="e" * 32,
            fencing_token=1,
            action_protocol="mc616d2-v1",
            service_scope=("",),  # empty service name
        )


# ---------------------------------------------------------------------------
# 5. COMPOSE SERVICE EVIDENCE VERIFIER & COMPOSITION ROOT
# ---------------------------------------------------------------------------

def test_compose_service_evidence_verifier_end_to_end():
    svc = _make_service_obs("searxng")
    proj = _make_project_obs({"searxng": svc})
    plan = _build_plan(proj)
    ev = ServiceEvidenceContract.from_service_plan(plan)

    class _MockComposeService:
        def plan_service_update(self, project, service_name, query_registries=True):
            return _build_plan(proj, service_name)

    verifier = compose_service_evidence_verifier(
        compose_service=_MockComposeService(),
        project_resolver=lambda tid: proj,
    )

    contract = _make_test_contract(ev)
    code = verifier.verify(contract, ev)
    assert code is GateCode.ALLOWED


def test_compose_service_evidence_verifier_stale_detection():
    svc_old = _make_service_obs("searxng", candidate_digest="sha256:" + "2" * 64)
    proj_old = _make_project_obs({"searxng": svc_old})
    plan_old = _build_plan(proj_old)
    ev_old = ServiceEvidenceContract.from_service_plan(plan_old)

    svc_new = _make_service_obs("searxng", candidate_digest="sha256:" + "8" * 64)
    proj_new = _make_project_obs({"searxng": svc_new})

    class _MockLiveComposeService:
        def plan_service_update(self, project, service_name, query_registries=True):
            return _build_plan(proj_new, service_name)

    verifier = ComposeServiceEvidenceVerifier(
        compose_service=_MockLiveComposeService(),
        project_resolver=lambda tid: proj_new,
    )

    contract = _make_test_contract(ev_old)
    code = verifier.verify(contract, ev_old)
    assert code is GateCode.CANDIDATE_DIGEST_MISMATCH


# ---------------------------------------------------------------------------
# 6. SECURITY: ADVERSARIAL INPUT RESILIENCE
# ---------------------------------------------------------------------------

def test_arbitrary_inputs_rejected_by_contract():
    valid_plan = _build_plan(_make_project_obs({"searxng": _make_service_obs("searxng")}))
    ev = ServiceEvidenceContract.from_service_plan(valid_plan)

    # Empty project name
    with pytest.raises(ValueError, match="project_name"):
        ServiceEvidenceContract(
            project_name="",
            compose_identity=ev.compose_identity,
            service_name=ev.service_name,
            requested_scope=ev.requested_scope,
            derived_execution_scope=ev.derived_execution_scope,
            atomicity=ev.atomicity,
            current_runtime_digest=ev.current_runtime_digest,
            target_candidate_digest=ev.target_candidate_digest,
            plan_digest=ev.plan_digest,
        )

    # Invalid plan digest (not 64-hex SHA-256)
    with pytest.raises(ValueError, match="plan_digest"):
        ServiceEvidenceContract(
            project_name=ev.project_name,
            compose_identity=ev.compose_identity,
            service_name=ev.service_name,
            requested_scope=ev.requested_scope,
            derived_execution_scope=ev.derived_execution_scope,
            atomicity=ev.atomicity,
            current_runtime_digest=ev.current_runtime_digest,
            target_candidate_digest=ev.target_candidate_digest,
            health_contract_summary=ev.health_contract_summary,
            plan_digest="not-a-valid-sha256-digest-value",
        )


def test_security_rejection_arbitrary_image_or_tag():
    """Adversarial candidate image or tag injected into observation changes candidate lookup key and digest."""
    svc = _make_service_obs("searxng")
    plan = _build_plan(_make_project_obs({"searxng": svc}))
    ev_auth = ServiceEvidenceContract.from_service_plan(plan)

    # Injected rogue image tag/registry
    ev_rogue = ServiceEvidenceContract(
        project_name=ev_auth.project_name,
        compose_identity=ev_auth.compose_identity,
        service_name=ev_auth.service_name,
        requested_scope=ev_auth.requested_scope,
        derived_execution_scope=ev_auth.derived_execution_scope,
        atomicity=ev_auth.atomicity,
        current_runtime_digest=ev_auth.current_runtime_digest,
        target_candidate_digest=ev_auth.target_candidate_digest,
        target_candidate_child_digest=ev_auth.target_candidate_child_digest,
        candidate_lookup_key="docker.io/malicious/evil:latest|arm64|linux",
        provenance_verified=ev_auth.provenance_verified,
        dependency_scope=ev_auth.dependency_scope,
        health_contract_summary=ev_auth.health_contract_summary,
        plan_digest=ev_auth.plan_digest,
    )
    decision = verify_service_evidence(ev_auth, ev_rogue, expected_digest=ev_auth.plan_digest)
    assert decision is GateCode.CANDIDATE_DIGEST_MISMATCH


def test_security_rejection_compose_or_path_injection():
    """Injected compose identity or path drift is rejected."""
    svc = _make_service_obs("searxng")
    plan = _build_plan(_make_project_obs({"searxng": svc}))
    ev_auth = ServiceEvidenceContract.from_service_plan(plan)

    ev_injected = ServiceEvidenceContract(
        project_name=ev_auth.project_name,
        compose_identity="evil-override-file.yml",
        service_name=ev_auth.service_name,
        requested_scope=ev_auth.requested_scope,
        derived_execution_scope=ev_auth.derived_execution_scope,
        atomicity=ev_auth.atomicity,
        current_runtime_digest=ev_auth.current_runtime_digest,
        target_candidate_digest=ev_auth.target_candidate_digest,
        target_candidate_child_digest=ev_auth.target_candidate_child_digest,
        candidate_lookup_key=ev_auth.candidate_lookup_key,
        provenance_verified=ev_auth.provenance_verified,
        dependency_scope=ev_auth.dependency_scope,
        health_contract_summary=ev_auth.health_contract_summary,
        plan_digest=ev_auth.plan_digest,
    )
    decision = verify_service_evidence(ev_auth, ev_injected, expected_digest=ev_auth.plan_digest)
    assert decision is GateCode.PLAN_IDENTITY_MISMATCH


# ---------------------------------------------------------------------------
# 7. STATIC PROOF: NO MUTATION AUTHORITY IN C.2 CODE
# ---------------------------------------------------------------------------

def test_static_proof_no_mutation_authority():
    """Verify that C.2 code contains zero subprocess, docker mutation, executor IPC, or privilege broker calls."""
    c2_files = [
        Path("/home/ubuntu/aipm/src/aipm/models/compose_plan.py"),
        Path("/home/ubuntu/aipm/src/aipm/control_plane/gate.py"),
        Path("/home/ubuntu/aipm/src/aipm/composition/service_evidence.py"),
    ]

    forbidden_call_names = {
        "subprocess",
        "Popen",
        "check_call",
        "check_output",
        "capture_snapshot",
        "create_snapshot",
        "rollback_action",
        "execute_rollback",
        "broker_client",
        "escalate",
        "sudo",
    }

    for path in c2_files:
        assert path.is_file(), f"File {path} must exist"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    assert func.id not in forbidden_call_names, f"Forbidden call {func.id} in {path}"
                elif isinstance(func, ast.Attribute):
                    assert func.attr not in forbidden_call_names, f"Forbidden call {func.attr} in {path}"
