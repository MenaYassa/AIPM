from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PostureState(str, Enum):
    OK = "ok"
    FRESH = "fresh"
    STALE = "stale"
    NEVER_SAMPLED = "never_sampled"
    UNKNOWN = "unknown"
    ERROR = "error"
    NOT_OBSERVED = "not_observed"
    NOT_INSTANTIATED = "not_instantiated"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class NotificationProviderState(str, Enum):
    DISABLED = "disabled"
    NOT_INSTANTIATED = "not_instantiated"
    NOT_OBSERVED = "not_observed"


class NotificationAuditAvailability(str, Enum):
    OBSERVED = "observed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ApplicationPosture:
    version: str | None
    commit: str | None
    state: PostureState


@dataclass(frozen=True, slots=True)
class NotificationAuditMetrics:
    availability: NotificationAuditAvailability
    schema_version: int | None
    pending: int | None
    sending: int | None
    sent: int | None
    failed: int | None
    unknown: int | None
    suppressed: int | None
    retry_exhaustion_count: int | None
    recent_delivery_latency_seconds: float | None
    oldest_pending_age_seconds: int | None
    oldest_unknown_age_seconds: int | None
    lease_expiry_count: int | None


@dataclass(frozen=True, slots=True)
class NotificationPosture:
    enabled: bool
    provider_state: NotificationProviderState
    configured_channel_count: int
    enabled_channel_count: int
    configured_policy_count: int
    enabled_policy_count: int
    audit: NotificationAuditMetrics


@dataclass(frozen=True, slots=True)
class DatabasePosture:
    sqlite_mode: str
    query_only: bool
    filesystem_write_boundary: str
    schema_mutation: str
    checkpointing: str


@dataclass(frozen=True, slots=True)
class DeploymentPosture:
    binding: str
    public_ingress: str
    permanent_service: str


@dataclass(frozen=True, slots=True)
class CapabilityPosture:
    name: str
    state: PostureState
    available: bool


@dataclass(frozen=True, slots=True)
class TelemetryPosture:
    enabled: bool
    interval_seconds: int
    state: PostureState


@dataclass(frozen=True, slots=True)
class SettingsPosture:
    available: bool
    status: PostureState
    error: str | None
    generated_at: str
    application: ApplicationPosture
    deployment: DeploymentPosture
    database: DatabasePosture
    telemetry: TelemetryPosture
    mc3: TelemetryPosture
    notifications: NotificationPosture
    capabilities: tuple[CapabilityPosture, ...] = field(default_factory=tuple)

    @classmethod
    def unavailable(cls, *, generated_at: str, error: str = "Settings posture unavailable") -> "SettingsPosture":
        audit = NotificationAuditMetrics(
            availability=NotificationAuditAvailability.UNAVAILABLE,
            schema_version=None,
            pending=None,
            sending=None,
            sent=None,
            failed=None,
            unknown=None,
            suppressed=None,
            retry_exhaustion_count=None,
            recent_delivery_latency_seconds=None,
            oldest_pending_age_seconds=None,
            oldest_unknown_age_seconds=None,
            lease_expiry_count=None,
        )
        return cls(
            available=False,
            status=PostureState.UNAVAILABLE,
            error=error,
            generated_at=generated_at,
            application=ApplicationPosture(None, None, PostureState.UNAVAILABLE),
            deployment=DeploymentPosture("loopback_only_required", "not_observed", "not_observed"),
            database=DatabasePosture("read_only", True, "required", "prohibited", "prohibited"),
            telemetry=TelemetryPosture(False, 0, PostureState.UNAVAILABLE),
            mc3=TelemetryPosture(False, 0, PostureState.UNAVAILABLE),
            notifications=NotificationPosture(
                False,
                NotificationProviderState.NOT_INSTANTIATED,
                0,
                0,
                0,
                0,
                audit,
            ),
            capabilities=tuple(),
        )


def bounded_count(value: Any, *, maximum: int = 1_000_000) -> int:
    try:
        return max(0, min(int(value), maximum))
    except (TypeError, ValueError):
        return 0


def bounded_interval(value: Any, *, default: int = 0, maximum: int = 86_400) -> int:
    try:
        return max(0, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def bounded_latency(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), 86_400.0))
    except (TypeError, ValueError):
        return None


def bounded_optional_age(value: Any) -> int | None:
    if value is None:
        return None
    return bounded_interval(value, maximum=31_536_000)
