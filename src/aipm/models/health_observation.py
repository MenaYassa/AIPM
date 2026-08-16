from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aipm.models.finding import Severity
from aipm.models.health import HealthState


@dataclass(slots=True, frozen=True)
class HealthFindingRecord:
    fingerprint: str
    code: str
    component: str
    severity: Severity
    title: str
    description: str
    resource: str | None


@dataclass(slots=True, frozen=True)
class HealthObservation:
    id: int | None
    source_run_id: int
    sampled_at: datetime
    project_path: str
    project_name: str
    report_state: HealthState
    score: int
    findings: tuple[HealthFindingRecord, ...] = ()
