from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from aipm.models.telemetry import DashboardSnapshot, DockerSnapshot, HandbookRoute, HostSnapshot, ProjectInventorySnapshot, TelemetryError, TunnelSnapshot
from aipm.models.telemetry_sampling import ResourceRefreshResult
from aipm.services.telemetry.docker import DockerTelemetryService
from aipm.services.telemetry.host import HostTelemetryService
from aipm.services.telemetry.project import ProjectTelemetryService
from aipm.services.telemetry.tunnel import TunnelTelemetryService


class DashboardTelemetryService:
    """Collect typed telemetry with legacy and non-blocking split paths."""

    def __init__(self, host: HostTelemetryService, docker: DockerTelemetryService, projects: ProjectTelemetryService, tunnel: TunnelTelemetryService, *, handbook: tuple[HandbookRoute, ...] = (), clock: Callable[[], datetime] | None = None, logger: Any | None = None) -> None:
        self.host = host
        self.docker = docker
        self.projects = projects
        self.tunnel = tunnel
        self.handbook = handbook
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.logger = logger

    def snapshot(self) -> DashboardSnapshot:
        """Legacy synchronous snapshot retained for rollback mode."""
        now = self.clock()
        host = self._collect_host()
        docker = self._collect_docker_legacy()
        projects = self._collect_projects_legacy()
        tunnel = self._collect_tunnel(docker)
        return DashboardSnapshot(generated_at=now, host=host, docker=docker, projects=projects, tunnel=tunnel, handbook=self.handbook)

    def fast_snapshot(self) -> DashboardSnapshot:
        """Fast snapshot that never waits for resource stats or project discovery."""
        now = self.clock()
        host = self._collect_host()
        docker = self._collect_docker_fast(now)
        projects = self._collect_projects_cached(now)
        tunnel = self._collect_tunnel(docker)
        return DashboardSnapshot(generated_at=now, host=host, docker=docker, projects=projects, tunnel=tunnel, handbook=self.handbook)

    def refresh_resources(self, *, timeout_seconds: int, now: datetime | None = None) -> ResourceRefreshResult:
        return self.docker.refresh_resources(timeout_seconds=timeout_seconds, now=now or self.clock())

    def refresh_projects(self) -> ProjectInventorySnapshot:
        return self.projects.snapshot()

    def _collect_host(self) -> HostSnapshot:
        try:
            return self.host.snapshot()
        except Exception as exc:
            error = self._error("HOST_TELEMETRY_FAILED", "Host telemetry unavailable", exc)
            return HostSnapshot.unavailable(error)

    def _collect_docker_legacy(self) -> DockerSnapshot:
        try:
            return self.docker.snapshot()
        except Exception as exc:
            error = self._error("DOCKER_TELEMETRY_FAILED", "Docker telemetry unavailable", exc)
            return DockerSnapshot.unavailable_snapshot(error)

    def _collect_docker_fast(self, now: datetime) -> DockerSnapshot:
        try:
            return self.docker.fast_snapshot(now=now)
        except Exception as exc:
            error = self._error("DOCKER_TELEMETRY_FAILED", "Docker telemetry unavailable", exc)
            return DockerSnapshot.unavailable_snapshot(error)

    def _collect_projects_legacy(self) -> ProjectInventorySnapshot:
        try:
            return self.projects.snapshot()
        except Exception as exc:
            error = self._error("PROJECT_DISCOVERY_FAILED", "Project discovery unavailable", exc)
            return ProjectInventorySnapshot.unavailable_snapshot(error)

    def _collect_projects_cached(self, now: datetime) -> ProjectInventorySnapshot:
        try:
            return self.projects.cached_snapshot(now=now)
        except Exception as exc:
            error = self._error("PROJECT_DISCOVERY_FAILED", "Project discovery unavailable", exc)
            return ProjectInventorySnapshot.unavailable_snapshot(error)

    def _collect_tunnel(self, docker: DockerSnapshot) -> TunnelSnapshot:
        try:
            return self.tunnel.snapshot(docker)
        except Exception as exc:
            error = self._error("TUNNEL_TELEMETRY_FAILED", "Cloudflared telemetry unavailable", exc)
            return TunnelSnapshot(state="unknown", source="error", available=False, error=error)

    def _error(self, code: str, message: str, exc: Exception) -> TelemetryError:
        if self.logger is not None:
            self.logger.exception(message, exc_info=exc)
        return TelemetryError(code=code, message=message)
