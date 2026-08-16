from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from aipm.models.container import Container
from aipm.models.project import Project
from aipm.models.system import SystemSummary


@dataclass(slots=True, frozen=True)
class TelemetryError:
    """Safe diagnostic information intended for domain-to-response mapping."""

    code: str
    message: str


@dataclass(slots=True, frozen=True)
class SwapStats:
    total_gb: float = 0.0
    used_gb: float = 0.0
    percent: float = 0.0
    available: bool = True
    error: TelemetryError | None = None


@dataclass(slots=True, frozen=True)
class NetworkStats:
    interfaces: int = 0
    established: int | None = 0
    available: bool = True
    error: TelemetryError | None = None


@dataclass(slots=True, frozen=True)
class HostSnapshot:
    """A host snapshot composed from existing AIPM system models."""

    system: SystemSummary | None
    swap: SwapStats
    load_one: float | None
    load_five: float | None
    load_fifteen: float | None
    uptime_seconds: int | None
    network: NetworkStats
    available: bool = True
    error: TelemetryError | None = None

    @classmethod
    def unavailable(cls, error: TelemetryError) -> "HostSnapshot":
        return cls(
            system=None,
            swap=SwapStats(available=False, error=error),
            load_one=None,
            load_five=None,
            load_fifteen=None,
            uptime_seconds=None,
            network=NetworkStats(available=False, established=None, error=error),
            available=False,
            error=error,
        )


@dataclass(slots=True, frozen=True)
class ResourceStats:
    cpu_percent: float | None = None
    memory_used_mb: float | None = None
    memory_limit_mb: float | None = None
    memory_percent: float | None = None
    available: bool = True
    error: TelemetryError | None = None


@dataclass(slots=True, frozen=True)
class ContainerSnapshot:
    """Existing Container identity/state plus telemetry-specific measurements."""

    container: Container
    resources: ResourceStats = field(default_factory=ResourceStats)
    restart_count: int = 0
    started_at: str | None = None


@dataclass(slots=True, frozen=True)
class DockerSnapshot:
    available: bool
    status: str
    containers: tuple[ContainerSnapshot, ...] = ()
    error: TelemetryError | None = None

    @property
    def running(self) -> int:
        return sum(item.container.state == "running" for item in self.containers)

    @property
    def stopped(self) -> int:
        return len(self.containers) - self.running

    @property
    def unhealthy(self) -> int:
        return sum(item.container.health == "unhealthy" for item in self.containers if item.container.state == "running")

    @classmethod
    def unavailable_snapshot(cls, error: TelemetryError) -> "DockerSnapshot":
        return cls(available=False, status="unknown", error=error)


@dataclass(slots=True, frozen=True)
class ProjectSnapshot:
    project: Project


@dataclass(slots=True, frozen=True)
class ProjectInventorySnapshot:
    available: bool
    status: str
    search_paths: tuple[str, ...] = ()
    projects: tuple[ProjectSnapshot, ...] = ()
    error: TelemetryError | None = None

    @classmethod
    def unavailable_snapshot(cls, error: TelemetryError) -> "ProjectInventorySnapshot":
        return cls(available=False, status="unknown", error=error)


@dataclass(slots=True, frozen=True)
class TunnelSnapshot:
    state: str
    source: str
    local_containers: tuple[str, ...] = ()
    systemd: str | None = None
    available: bool = True
    error: TelemetryError | None = None


@dataclass(slots=True, frozen=True)
class HandbookRoute:
    id: str
    title: str
    description: str
    commands: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class DashboardSnapshot:
    generated_at: datetime
    host: HostSnapshot
    docker: DockerSnapshot
    projects: ProjectInventorySnapshot
    tunnel: TunnelSnapshot
    handbook: tuple[HandbookRoute, ...] = ()
