from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from aipm.models.git import GitRepository
from aipm.models.health_report import HealthReport


class UpdateRisk(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


@dataclass(slots=True, frozen=True)
class UpdatePlan:
    project: str
    project_path: str
    dry_run: bool
    proceed: bool
    approval_required: bool
    risk: UpdateRisk
    reasons: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    snapshot_required: bool = True
    estimated_restart: bool = False
    stash_required: bool = False
    pull_required: bool = False
    git: GitRepository | None = None
    health_before: HealthReport | None = None


@dataclass(slots=True, frozen=True)
class UpdateAudit:
    project: str
    started_at: datetime
    finished_at: datetime
    mode: str
    outcome: str
    risk: UpdateRisk
    plan: UpdatePlan
    snapshot_path: Path | None = None
    health_after: HealthReport | None = None
    error: str | None = None
