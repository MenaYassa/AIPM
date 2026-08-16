from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True, frozen=True)
class HistoryQuery:
    start: datetime | None = None
    end: datetime | None = None
    limit: int = 500


@dataclass(slots=True, frozen=True)
class HostHistoryPoint:
    sampled_at: datetime
    hostname: str | None
    cpu_percent: float | None
    load_one: float | None
    load_five: float | None
    load_fifteen: float | None
    memory_total_gb: float | None
    memory_used_gb: float | None
    memory_available_gb: float | None
    memory_percent: float | None
    swap_total_gb: float | None
    swap_used_gb: float | None
    swap_percent: float | None
    disk_total_gb: float | None
    disk_used_gb: float | None
    disk_free_gb: float | None
    disk_percent: float | None
    network_interfaces: int | None
    network_established: int | None
    available: bool


@dataclass(slots=True, frozen=True)
class ContainerHistoryPoint:
    sampled_at: datetime
    container_id: str
    container_name: str
    image: str | None
    state: str | None
    health: str | None
    stack: str | None
    restart_count: int | None
    cpu_percent: float | None
    memory_used_mb: float | None
    memory_limit_mb: float | None
    memory_percent: float | None
    stats_available: bool


@dataclass(slots=True, frozen=True)
class ProjectHistoryPoint:
    sampled_at: datetime
    name: str
    path: str | None
    branch: str | None
    has_git: bool
    has_compose: bool
    dirty: bool | None
    ahead: int | None
    behind: int | None


@dataclass(slots=True, frozen=True)
class TunnelHistoryPoint:
    sampled_at: datetime
    state: str
    source: str
    systemd: str | None
    local_containers: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class SampleRunRecord:
    sampled_at: datetime
    host_available: bool
    docker_available: bool
    projects_available: bool
    tunnel_state: str
    duration_ms: int | None = None


@dataclass(slots=True, frozen=True)
class HistoricalSample:
    run: SampleRunRecord
    host: HostHistoryPoint | None
    containers: tuple[ContainerHistoryPoint, ...]
    projects: tuple[ProjectHistoryPoint, ...]
    tunnel: TunnelHistoryPoint | None


@dataclass(slots=True, frozen=True)
class SampleResult:
    sampled_at: datetime
    run_id: int | None
    host_rows: int
    container_rows: int
    project_rows: int
    tunnel_rows: int
    retention_deleted: int
    skipped: bool = False
    error: str | None = None


@dataclass(slots=True, frozen=True)
class HistoryResponse:
    available: bool
    status: str
    error: str | None
    points: tuple[object, ...]
