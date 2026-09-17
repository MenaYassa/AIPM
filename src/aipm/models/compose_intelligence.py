"""Typed domain models for Compose service and image intelligence.

Preserves the critical architectural distinction between:
- Declared image (what the Compose file specifies or builds)
- Running image (what the container runtime is actually executing, with its ID and RepoDigests)
- Candidate image (what external registry intelligence discovers as potentially available)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ServiceCandidateStatus(str, Enum):
    """External candidate assessment status for a Compose service."""

    CURRENT = "current"
    UPDATE_AVAILABLE = "update_available"
    NOT_APPLICABLE = "not_applicable"
    DRIFT = "drift"
    UNKNOWN = "unknown"


class ServiceCandidateReason(str, Enum):
    """Detailed rationale for the candidate status."""

    UP_TO_DATE = "up_to_date"
    CANDIDATE_DIGEST_DIFFERS = "candidate_digest_differs"
    NEWER_DIGEST_AVAILABLE = "candidate_digest_differs"
    LOCAL_BUILD = "local_build"
    PINNED_BY_DIGEST = "pinned_by_digest"
    CONFIGURATION_DRIFT = "configuration_drift"
    DISABLED_BY_PROFILE = "disabled_by_profile"
    NO_RUNNING_CONTAINER = "no_running_container"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NETWORK_BUDGET_EXHAUSTED = "network_budget_exhausted"
    REGISTRY_UNAVAILABLE = "registry_unavailable"
    REGISTRY_TIMEOUT = "registry_timeout"
    AUTHENTICATION_REQUIRED = "authentication_required"
    ARCHITECTURE_UNAVAILABLE = "architecture_unavailable"
    MANIFEST_UNAVAILABLE = "manifest_unavailable"
    DIGEST_UNAVAILABLE = "digest_unavailable"
    SSRF_BLOCKED = "ssrf_blocked"
    MALFORMED_IMAGE_REFERENCE = "malformed_image_reference"


class CandidateCacheFreshness(str, Enum):
    """Freshness state of a cached candidate result."""

    FRESH = "fresh"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class CandidateLookupKey:
    """Canonical, deduplicated identity for a remote OCI registry candidate query.

    Ensures that identical image references across multiple services coalesce
    into a single remote lookup while maintaining strict separation of target
    platform architecture.
    """

    registry: str
    repository: str
    tag: str
    target_arch: str = "arm64"
    target_os: str = "linux"

    @classmethod
    def from_image_ref(
        cls,
        image_ref: ImageReference,
        *,
        target_arch: str = "arm64",
        target_os: str = "linux",
    ) -> CandidateLookupKey:
        registry = (image_ref.registry or "docker.io").lower().strip()
        repo = (image_ref.repository or "").lower().strip()
        tag = (image_ref.tag or "latest").strip()
        if tag.lower() == "latest":
            tag = "latest"
        return cls(
            registry=registry,
            repository=repo,
            tag=tag,
            target_arch=target_arch.lower().strip(),
            target_os=target_os.lower().strip(),
        )

    def canonical_str(self) -> str:
        return f"{self.registry}/{self.repository}:{self.tag} [{self.target_os}/{self.target_arch}]"


@dataclass(frozen=True, slots=True)
class ImageReference:
    """Normalized, validated container image reference.

    Decomposes an image string into its canonical registry, repository,
    tag, and digest components without shell or naive splitting.
    """

    raw: str
    registry: str
    repository: str
    tag: str | None = None
    digest: str | None = None
    is_pinned_by_digest: bool = False
    is_local_build: bool = False

    @property
    def canonical(self) -> str:
        """Render canonical normalized string representation."""
        base = f"{self.registry}/{self.repository}"
        if self.digest:
            return f"{base}@{self.digest}"
        if self.tag:
            return f"{base}:{self.tag}"
        return base


@dataclass(frozen=True, slots=True)
class DeclaredServiceConfig:
    """Declared Compose service configuration extracted safely from files."""

    service_name: str
    image: str | None = None
    image_ref: ImageReference | None = None
    is_build: bool = False
    build_context: str | None = None
    build_dockerfile: str | None = None
    profiles: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    source_files: tuple[str, ...] = ()
    parse_error: str | None = None


@dataclass(frozen=True, slots=True)
class ComposeServiceObservation:
    """Complete read-only observation of an individual Compose service.

    Explicitly separates declared configuration, running container state,
    and external candidate intelligence.
    """

    service_name: str
    container_names: tuple[str, ...]
    container_ids: tuple[str, ...]
    state: str
    health: str | None
    declared_image: str | None
    declared_image_ref: ImageReference | None
    running_image: str | None
    running_image_id: str | None
    running_repo_digests: tuple[str, ...]
    candidate_digest: str | None
    candidate_child_digest: str | None
    candidate_status: ServiceCandidateStatus
    candidate_reason: ServiceCandidateReason
    candidate_detail: str | None
    is_build: bool
    build_context: str | None
    build_dockerfile: str | None
    ports: tuple[str, ...]
    freshness: str
    observed_at: datetime | None
    provenance_verified: bool
    depends_on: tuple[str, ...] = ()
    candidate_key: CandidateLookupKey | None = None


@dataclass(frozen=True, slots=True)
class ComposeProjectObservation:
    """Aggregate read-only observation of a project's Compose stack."""

    project_name: str
    compose_identity: str
    project_path: str
    compose_files: tuple[str, ...]
    services: tuple[ComposeServiceObservation, ...]
    running_services_count: int
    total_services_count: int
    updates_available_count: int
    current_count: int
    drift_count: int
    not_applicable_count: int
    unknown_count: int
    observed_at: datetime | None
    freshness: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dictionary."""
        return {
            "project_name": self.project_name,
            "compose_identity": self.compose_identity,
            "project_path": self.project_path,
            "compose_files": list(self.compose_files),
            "services": [
                {
                    "service_name": s.service_name,
                    "container_names": list(s.container_names),
                    "container_ids": list(s.container_ids),
                    "state": s.state,
                    "health": s.health,
                    "declared_image": s.declared_image,
                    "running_image": s.running_image,
                    "running_image_id": s.running_image_id,
                    "running_repo_digests": list(s.running_repo_digests),
                    "candidate_digest": s.candidate_digest,
                    "candidate_child_digest": s.candidate_child_digest,
                    "candidate_status": s.candidate_status.value,
                    "candidate_reason": s.candidate_reason.value,
                    "candidate_detail": s.candidate_detail,
                    "is_build": s.is_build,
                    "build_context": s.build_context,
                    "build_dockerfile": s.build_dockerfile,
                    "ports": list(s.ports),
                    "depends_on": list(s.depends_on),
                    "candidate_key": s.candidate_key.canonical_str() if s.candidate_key else None,
                    "freshness": s.freshness,
                    "observed_at": s.observed_at.isoformat() if s.observed_at else None,
                    "provenance_verified": s.provenance_verified,
                }
                for s in self.services
            ],
            "running_services_count": self.running_services_count,
            "total_services_count": self.total_services_count,
            "updates_available_count": self.updates_available_count,
            "current_count": self.current_count,
            "drift_count": self.drift_count,
            "not_applicable_count": self.not_applicable_count,
            "unknown_count": self.unknown_count,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "freshness": self.freshness,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class ServiceUpdatePlanDesign:
    """Design-only representation for future selective service update planning (MC-6.15-A.3/A.4).

    Strictly read-only architecture model; owns NO execution authority or mutation capability.
    """

    project_name: str
    service_name: str
    current_runtime_digest: str | None
    declared_image_ref: ImageReference | None
    target_candidate_digest: str
    dependency_scope: tuple[str, ...]
    health_contract: str | None
