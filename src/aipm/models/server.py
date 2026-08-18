"""Typed read-only Server detail projections for Mission Control."""

from __future__ import annotations

from dataclasses import dataclass

from aipm.models.telemetry import HostSnapshot, TelemetryError


@dataclass(frozen=True, slots=True)
class FilesystemDetail:
    """Safe allow-listed filesystem capacity detail."""

    mountpoint: str
    filesystem: str | None
    total_gb: float
    used_gb: float
    free_gb: float
    percent: float


@dataclass(frozen=True, slots=True)
class NetworkInterfaceDetail:
    """Safe per-interface counters without addresses or endpoints."""

    name: str
    is_up: bool | None
    rx_bytes: int | None
    tx_bytes: int | None


@dataclass(frozen=True, slots=True)
class ServerHostSnapshot:
    """Existing host observation plus optional bounded detail projections."""

    host: HostSnapshot
    filesystems: tuple[FilesystemDetail, ...] = ()
    filesystem_available: bool = False
    filesystem_error: TelemetryError | None = None
    interfaces: tuple[NetworkInterfaceDetail, ...] = ()
    interface_detail_available: bool = False
    interface_detail_error: TelemetryError | None = None
    connection_states: tuple[tuple[str, int], ...] = ()
    connection_states_available: bool = False
    connection_states_error: TelemetryError | None = None
