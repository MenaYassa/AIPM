"""Typed domain models for Compose service update planning (MC-6.15-B.1/B.2).

Strictly read-only architecture models; own NO execution authority or mutation capability.
Distinguishes presentation models, canonical binding identities, and health/rollback designs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aipm.models.compose_intelligence import (
        CandidateLookupKey,
        ImageReference,
        ServiceCandidateReason,
        ServiceCandidateStatus,
    )


class ServiceUpdateAtomicity(str, Enum):
    """Atomicity classification for a service update."""

    LEAF_INDEPENDENT = "leaf_independent"
    ATOMIC_TIGHT = "atomic_tight"
    BLOCKED = "blocked"


class ServicePlanBlockingReason(str, Enum):
    """Explicit reasons why a service update plan is blocked/ineligible."""

    NOT_UPDATE_AVAILABLE = "not_update_available"
    CURRENT_DIGEST_MISSING = "current_digest_missing"
    CANDIDATE_DIGEST_MISSING = "candidate_digest_missing"
    OBSERVATION_STALE = "observation_stale"
    PROVENANCE_INVALID = "provenance_invalid"
    NON_COMPOSE_PROJECT = "non_compose_project"
    DEPENDENCY_CYCLE_DETECTED = "dependency_cycle_detected"
    MISSING_DEPENDENCY_SERVICE = "missing_dependency_service"
    DEPENDENCY_NOT_RUNNING = "dependency_not_running"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    LOCAL_BUILD = "local_build"
    DISABLED_BY_PROFILE = "disabled_by_profile"
    SERVICE_NOT_FOUND = "service_not_found"
    CONTRADICTORY_TOPOLOGY = "contradictory_topology"


@dataclass(frozen=True, slots=True)
class DependencyScopeItem:
    """Read-only representation of a dependency in scope for a service update plan."""

    service_name: str
    running_image: str | None
    running_digest: str | None
    candidate_digest: str | None
    candidate_status: str
    state: str
    health: str | None
    in_scope_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_name": self.service_name,
            "running_image": self.running_image,
            "running_digest": self.running_digest,
            "candidate_digest": self.candidate_digest,
            "candidate_status": self.candidate_status,
            "state": self.state,
            "health": self.health,
            "in_scope_reason": self.in_scope_reason,
        }


@dataclass(frozen=True, slots=True)
class ServiceHealthContract:
    """Read-only health verification contract for a service update plan.

    Pure data specification; never executes probes or mutations.
    """

    service_name: str
    expected_state: str = "running"
    expected_health: str | None = "healthy"
    has_health_check: bool = True
    timeout_seconds: int = 30
    success_condition: str = "container_running_and_healthy"
    check_dependencies: bool = False
    dependency_services: tuple[str, ...] = ()

    def canonical_summary(self) -> str:
        """Deterministic summary used for plan identity binding."""
        deps = ",".join(sorted(self.dependency_services)) if self.check_dependencies else "none"
        health_req = self.expected_health or "none"
        return f"svc:{self.service_name}|state:{self.expected_state}|health:{health_req}|timeout:{self.timeout_seconds}|deps:{deps}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_name": self.service_name,
            "expected_state": self.expected_state,
            "expected_health": self.expected_health,
            "has_health_check": self.has_health_check,
            "timeout_seconds": self.timeout_seconds,
            "success_condition": self.success_condition,
            "check_dependencies": self.check_dependencies,
            "dependency_services": list(self.dependency_services),
            "canonical_summary": self.canonical_summary(),
        }


@dataclass(frozen=True, slots=True)
class ServiceMutationShape:
    """Descriptive-only schema of what a future execution engine would perform.

    Strictly data; contains NO execution primitives, runnable commands, or subprocess hooks.
    """

    target_service: str
    candidate_digest: str
    dependency_mode: str  # "no_deps", "atomic_group"
    affected_services: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_service": self.target_service,
            "candidate_digest": self.candidate_digest,
            "dependency_mode": self.dependency_mode,
            "affected_services": list(self.affected_services),
        }


@dataclass(frozen=True, slots=True)
class ServiceRollbackDesign:
    """Design-only representation for future rollback semantics.

    Strictly data; owns NO execution, snapshot, or filesystem authority.
    """

    snapshot_required: bool = True
    targeted_service_backup: bool = True
    rollback_supported: bool = True
    rollback_scope: str = "target_only"  # "target_only", "dependency_group", "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_required": self.snapshot_required,
            "targeted_service_backup": self.targeted_service_backup,
            "rollback_supported": self.rollback_supported,
            "rollback_scope": self.rollback_scope,
        }


@dataclass(frozen=True, slots=True)
class ServiceUpdatePlan:
    """Complete, immutable read-only plan for a single Compose service update."""

    project_name: str
    project_id: str | None
    compose_identity: str
    service_name: str
    current_runtime_image: str | None
    current_runtime_digest: str | None
    declared_image: str | None
    declared_image_ref: ImageReference | None
    target_candidate_digest: str | None
    target_candidate_child_digest: str | None
    candidate_key: CandidateLookupKey | None
    candidate_freshness: str
    candidate_status: ServiceCandidateStatus
    candidate_reason: ServiceCandidateReason
    candidate_detail: str | None
    provenance_verified: bool
    dependency_scope: tuple[DependencyScopeItem, ...]
    atomicity: ServiceUpdateAtomicity
    health_contract: ServiceHealthContract
    eligible: bool
    blocking_reason: ServicePlanBlockingReason | None
    expected_mutation: ServiceMutationShape
    rollback_design: ServiceRollbackDesign
    observed_at: datetime | None
    plan_digest: str
    actions: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    plan_identity: Any | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_name": self.project_name,
            "project_id": self.project_id,
            "compose_identity": self.compose_identity,
            "service_name": self.service_name,
            "current_runtime_image": self.current_runtime_image,
            "current_runtime_digest": self.current_runtime_digest,
            "declared_image": self.declared_image,
            "declared_image_ref": self.declared_image_ref.canonical if self.declared_image_ref else None,
            "target_candidate_digest": self.target_candidate_digest,
            "target_candidate_child_digest": self.target_candidate_child_digest,
            "candidate_key": self.candidate_key.canonical_str() if self.candidate_key else None,
            "candidate_freshness": self.candidate_freshness,
            "candidate_status": self.candidate_status.value,
            "candidate_reason": self.candidate_reason.value,
            "candidate_detail": self.candidate_detail,
            "provenance_verified": self.provenance_verified,
            "dependency_scope": [d.to_dict() for d in self.dependency_scope],
            "atomicity": self.atomicity.value,
            "health_contract": self.health_contract.to_dict(),
            "eligible": self.eligible,
            "blocking_reason": self.blocking_reason.value if self.blocking_reason else None,
            "expected_mutation": self.expected_mutation.to_dict(),
            "rollback_design": self.rollback_design.to_dict(),
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "plan_digest": self.plan_digest,
            "plan_identity": self.plan_identity.canonical_payload() if hasattr(self.plan_identity, "canonical_payload") else None,
            "actions": list(self.actions),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class ServiceEvidenceContract:
    """Typed immutable service evidence contract for FinalExecutionGate (MC-6.15-C.2).

    Binds the authoritative pre-mutation evidence required to prove that the live
    environment still produces the exact same service update plan that was authorized.
    Contains strictly identity and security-relevant verification metadata;
    contains NO PIDs, container IDs, timestamps, shell commands, or arbitrary paths.
    """

    project_name: str
    compose_identity: str
    service_name: str
    requested_scope: tuple[str, ...]
    derived_execution_scope: tuple[str, ...]
    atomicity: str
    current_runtime_digest: str | None
    target_candidate_digest: str | None
    target_candidate_child_digest: str | None = None
    candidate_lookup_key: str | None = None
    provenance_verified: bool = True
    dependency_scope: tuple[str, ...] = ()
    health_contract_summary: str = ""
    plan_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.project_name, str) or not self.project_name:
            raise ValueError("project_name must be a non-empty string")
        if not isinstance(self.compose_identity, str) or not self.compose_identity:
            raise ValueError("compose_identity must be a non-empty string")
        if not isinstance(self.service_name, str) or not self.service_name:
            raise ValueError("service_name must be a non-empty string")
        if not isinstance(self.requested_scope, tuple) or not self.requested_scope:
            raise ValueError("requested_scope must be a non-empty tuple of service names")
        if not isinstance(self.derived_execution_scope, tuple) or not self.derived_execution_scope:
            raise ValueError("derived_execution_scope must be a non-empty tuple of service names")
        if not isinstance(self.atomicity, str) or not self.atomicity:
            raise ValueError("atomicity must be a non-empty string")
        if not isinstance(self.provenance_verified, bool):
            raise ValueError("provenance_verified must be a boolean")
        if not isinstance(self.dependency_scope, tuple):
            raise ValueError("dependency_scope must be a tuple")
        if not isinstance(self.health_contract_summary, str) or not self.health_contract_summary:
            raise ValueError("health_contract_summary must be a non-empty string")
        if not isinstance(self.plan_digest, str) or len(self.plan_digest) != 64 or any(c not in "0123456789abcdef" for c in self.plan_digest):
            raise ValueError("plan_digest must be a 64-hex SHA-256 string")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "atomicity": self.atomicity,
            "candidate_lookup_key": self.candidate_lookup_key,
            "compose_identity": self.compose_identity,
            "current_runtime_digest": self.current_runtime_digest,
            "dependency_scope": list(self.dependency_scope),
            "derived_execution_scope": list(self.derived_execution_scope),
            "health_contract_summary": self.health_contract_summary,
            "plan_digest": self.plan_digest,
            "project_name": self.project_name,
            "provenance_verified": self.provenance_verified,
            "requested_scope": list(self.requested_scope),
            "service_name": self.service_name,
            "target_candidate_child_digest": self.target_candidate_child_digest,
            "target_candidate_digest": self.target_candidate_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.canonical_payload()

    @classmethod
    def from_service_plan(
        cls,
        plan: ServiceUpdatePlan,
        *,
        requested_scope: tuple[str, ...] | None = None,
    ) -> "ServiceEvidenceContract":
        req = requested_scope or (plan.service_name,)
        derived = (
            plan.expected_mutation.affected_services
            if plan.expected_mutation and plan.expected_mutation.affected_services
            else (plan.service_name,)
        )
        candidate_key_str = plan.candidate_key.canonical_str() if plan.candidate_key else None
        health_summary = (
            plan.health_contract.canonical_summary()
            if plan.health_contract
            else f"svc:{plan.service_name}|state:running|health:healthy|timeout:30|deps:none"
        )
        dep_summaries = tuple(
            sorted(
                f"svc:{d.service_name}|status:{d.candidate_status}|health:{d.health or 'none'}|state:{d.state}"
                for d in plan.dependency_scope
            )
        ) if plan.dependency_scope else ()

        return cls(
            project_name=plan.project_name,
            compose_identity=plan.compose_identity,
            service_name=plan.service_name,
            requested_scope=tuple(req),
            derived_execution_scope=tuple(derived),
            atomicity=plan.atomicity.value if hasattr(plan.atomicity, "value") else str(plan.atomicity),
            current_runtime_digest=plan.current_runtime_digest,
            target_candidate_digest=plan.target_candidate_digest,
            target_candidate_child_digest=plan.target_candidate_child_digest,
            candidate_lookup_key=candidate_key_str,
            provenance_verified=bool(plan.provenance_verified),
            dependency_scope=dep_summaries,
            health_contract_summary=health_summary,
            plan_digest=plan.plan_digest,
        )
