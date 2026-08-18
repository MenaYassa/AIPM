"""Typed read-only project and application intelligence contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AssociationConfidence(str, Enum):
    EXACT = "exact"
    PROBABLE = "probable"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class ProjectSource(str, Enum):
    DISCOVERED = "discovered"
    RUNTIME_GROUP = "runtime_group"
    UNGROUPED = "ungrouped"


class AssociationRole(str, Enum):
    APPLICATION = "application"
    ASSOCIATED_LOCAL = "associated_local"
    LOCAL_CANDIDATE = "local_candidate"
    RUNTIME_ONLY = "runtime_only"
    UNGROUPED = "ungrouped"


class InventoryScope(str, Enum):
    APPLICATIONS = "applications"
    ASSOCIATED = "associated"
    LOCAL = "local"
    ALL = "all"


class ProjectHealthStatus(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProjectEvidence:
    code: str
    severity: str
    source: str
    message: str
    freshness: str = "unknown"
    observed_at: str | None = None


@dataclass(frozen=True)
class ProjectComponent:
    id: str
    name: str
    service_name: str | None
    container_id: str | None
    state: str
    health: str | None
    restart_count: int
    image: str | None
    resources: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[ProjectEvidence, ...] = ()


@dataclass(frozen=True)
class ProjectHealth:
    status: ProjectHealthStatus
    summary: str
    counts: dict[str, int]
    evidence: tuple[ProjectEvidence, ...]


@dataclass(frozen=True)
class ProjectApplication:
    id: str
    display_name: str
    source: ProjectSource
    confidence: AssociationConfidence
    local_project_name: str | None
    local_project_path: str | None
    runtime_group: str | None
    components: tuple[ProjectComponent, ...]
    git: dict[str, Any]
    compose: dict[str, Any]
    runtime: dict[str, Any]
    health: ProjectHealth
    freshness: dict[str, Any]
    evidence: tuple[ProjectEvidence, ...]
    warnings: tuple[str, ...] = ()
    inventory_scope: InventoryScope = InventoryScope.ALL
    association_role: AssociationRole = AssociationRole.APPLICATION
    association_explanation: str = ""
    local_project_id: str | None = None


@dataclass(frozen=True)
class ProjectInventory:
    projects: tuple[ProjectApplication, ...]
    search_paths: tuple[str, ...]
    freshness: dict[str, Any]
    source_errors: tuple[str, ...] = ()
    inventory_scope: InventoryScope = InventoryScope.ALL
    local_candidates: tuple[ProjectApplication, ...] = ()
