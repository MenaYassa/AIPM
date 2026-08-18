from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SystemdUnitStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    FAILED = "failed"
    ACTIVATING = "activating"
    DEACTIVATING = "deactivating"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class SystemdUnitId(StrEnum):
    DASHBOARD = "aipm-dashboard"
    TELEMETRY = "aipm-telemetry"
    EVENTS = "aipm-events"
    CLOUDFLARED = "cloudflared"


@dataclass(frozen=True, slots=True)
class SystemdUnitRegistryEntry:
    id: SystemdUnitId
    display_name: str
    unit_name: str
    manager_scope: str


@dataclass(frozen=True, slots=True)
class SystemdUnitSnapshot:
    id: SystemdUnitId
    display_name: str
    load_state: str
    active_state: str
    sub_state: str
    enabled: bool | None
    status: SystemdUnitStatus
    evidence: tuple[str, ...] = ()


SYSTEMD_UNIT_REGISTRY: tuple[SystemdUnitRegistryEntry, ...] = (
    SystemdUnitRegistryEntry(SystemdUnitId.DASHBOARD, "AIPM Dashboard", "aipm-dashboard.service", "user"),
    SystemdUnitRegistryEntry(SystemdUnitId.TELEMETRY, "AIPM Telemetry", "aipm-telemetry.service", "user"),
    SystemdUnitRegistryEntry(SystemdUnitId.EVENTS, "AIPM Events", "aipm-events.service", "user"),
    SystemdUnitRegistryEntry(SystemdUnitId.CLOUDFLARED, "Cloudflared Tunnel", "cloudflared.service", "system"),
)


def registry_entry(unit_id: str) -> SystemdUnitRegistryEntry | None:
    return next((entry for entry in SYSTEMD_UNIT_REGISTRY if entry.id.value == unit_id), None)


def status_from_states(active_state: str, sub_state: str) -> SystemdUnitStatus:
    active = active_state.strip().lower()
    sub = sub_state.strip().lower()
    if active == "active":
        return SystemdUnitStatus.ACTIVE
    if active == "failed":
        return SystemdUnitStatus.FAILED
    if active == "activating":
        return SystemdUnitStatus.ACTIVATING
    if active == "deactivating":
        return SystemdUnitStatus.DEACTIVATING
    if active == "inactive" or sub in {"dead", "exited"}:
        return SystemdUnitStatus.INACTIVE
    return SystemdUnitStatus.UNKNOWN
