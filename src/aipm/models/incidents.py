from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from aipm.models.events import Event, ResourceRef
from aipm.models.finding import Severity


class IncidentStatus(Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


@dataclass(slots=True, frozen=True)
class Incident:
    id: int | None
    incident_key: str
    title: str
    severity: Severity
    status: IncidentStatus
    started_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    resource: ResourceRef
    correlation_key: str
    summary: str
    events: tuple[Event, ...] = ()


@dataclass(slots=True, frozen=True)
class IncidentEvent:
    incident_id: int
    event_id: int
    attached_at: datetime


@dataclass(slots=True, frozen=True)
class IncidentFilter:
    status: IncidentStatus | None = None
    severity: Severity | None = None
    resource_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    limit: int = 500
