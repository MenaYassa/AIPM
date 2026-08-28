from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from aipm.models.events import EventType, ResourceRef, ResourceType
from aipm.models.finding import Severity
from aipm.models.incidents import IncidentStatus


class NotificationTrigger(Enum):
    INCIDENT_OPENED = "incident_opened"
    INCIDENT_ESCALATED = "incident_escalated"
    INCIDENT_UPDATED = "incident_updated"
    INCIDENT_RECOVERED = "incident_recovered"
    INCIDENT_REOPENED = "incident_reopened"
    INCIDENT_ACKNOWLEDGED = "incident_acknowledged"


class NotificationStatus(Enum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"
    UNKNOWN = "unknown"


class DeliveryStatus(Enum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class NotificationChannel:
    id: str
    name: str
    channel_type: str
    enabled: bool = False
    secret_ref: str | None = None
    destination_ref: str | None = None
    timeout_seconds: int = 10
    max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    id: str
    name: str
    enabled: bool = False
    minimum_severity: Severity = Severity.CRITICAL
    event_types: tuple[EventType, ...] = ()
    resource_types: tuple[ResourceType, ...] = ()
    project_paths: tuple[str, ...] = ()
    transitions: tuple[NotificationTrigger, ...] = (NotificationTrigger.INCIDENT_OPENED,)
    notify_recovery: bool = False
    notify_acknowledgement: bool = False
    notify_updates: bool = False
    cooldown_seconds: int = 900
    window_seconds: int = 3600
    max_notifications: int = 3
    channels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IncidentTransition:
    id: int | None
    incident_id: int
    incident_key: str
    transition: NotificationTrigger
    occurred_at: datetime
    previous_status: IncidentStatus | None
    current_status: IncidentStatus
    previous_severity: Severity | None
    current_severity: Severity
    event_id: int | None
    source_event_key: str | None
    correlation_key: str
    resource: ResourceRef
    event_type: EventType | None = None


@dataclass(frozen=True, slots=True)
class NotificationContent:
    title: str
    body: str
    incident_id: int
    event_id: int | None
    trigger: NotificationTrigger
    severity: Severity
    resource: ResourceRef


@dataclass(frozen=True, slots=True)
class Notification:
    id: int | None
    identity_key: str
    incident_id: int
    event_id: int | None
    transition_id: int
    policy_id: str
    channel_id: str
    trigger: NotificationTrigger
    status: NotificationStatus
    severity: Severity
    resource: ResourceRef
    title: str
    body: str
    created_at: datetime
    next_attempt_at: datetime | None = None
    attempt_count: int = 0
    suppressed_reason: str | None = None
    lease_token: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationAttempt:
    id: int | None
    delivery_id: int
    attempt_number: int
    started_at: datetime
    finished_at: datetime | None
    outcome: DeliveryStatus
    retryable: bool
    provider_status_code: int | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class NotificationFilter:
    status: NotificationStatus | None = None
    incident_id: int | None = None
    channel_id: str | None = None
    include_suppressed: bool = False
    limit: int = 100


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    matched: bool
    suppressed: bool
    reason: str | None
    policy_id: str
    channel_id: str
    identity_key: str


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    status: DeliveryStatus
    retryable: bool
    provider_message_id: str | None = None
    provider_status_code: int | None = None
    error_code: str | None = None
    error_message: str | None = None
