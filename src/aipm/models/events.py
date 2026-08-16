from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from aipm.models.finding import Severity


class EventType(Enum):
    CONTAINER_STARTED = "container_started"
    CONTAINER_RESTARTING = "container_restarting"
    CONTAINER_RESTARTED = "container_restarted"
    CONTAINER_STOPPED = "container_stopped"
    CONTAINER_RECOVERED = "container_recovered"
    CONTAINER_HEALTH_CHANGED = "container_health_changed"
    PROJECT_GIT_STATE_CHANGED = "project_git_state_changed"
    TUNNEL_STATE_CHANGED = "tunnel_state_changed"
    HEALTH_STATE_CHANGED = "health_state_changed"
    HEALTH_FINDING_CHANGED = "health_finding_changed"


class EventSource(Enum):
    TELEMETRY = "telemetry"
    HEALTH_ENGINE = "health_engine"
    DERIVED = "derived"


class ResourceType(Enum):
    CONTAINER = "container"
    PROJECT = "project"
    TUNNEL = "tunnel"
    HOST = "host"


@dataclass(slots=True, frozen=True)
class ResourceRef:
    resource_type: ResourceType
    identifier: str
    name: str | None = None
    project_path: str | None = None


@dataclass(slots=True, frozen=True)
class FindingEvidence:
    code: str
    component: str
    severity: Severity
    title: str
    description: str
    resource: str | None = None


@dataclass(slots=True, frozen=True)
class Event:
    id: int | None
    event_key: str
    occurred_at: datetime
    event_type: EventType
    severity: Severity
    source: EventSource
    resource: ResourceRef
    title: str
    description: str
    previous_value: str | None
    current_value: str | None
    source_run_id: int
    previous_run_id: int | None
    correlation_key: str
    evidence: tuple[FindingEvidence, ...] = ()


@dataclass(slots=True, frozen=True)
class EventFilter:
    start: datetime | None = None
    end: datetime | None = None
    status: str | None = None
    severity: Severity | None = None
    event_type: EventType | None = None
    resource_type: ResourceType | None = None
    resource_id: str | None = None
    limit: int = 500


@dataclass(slots=True, frozen=True)
class EventProcessResult:
    source_run_id: int
    processed: bool
    event_count: int
    incident_count: int
    error: str | None = None
