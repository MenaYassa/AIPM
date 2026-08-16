from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from aipm.models.telemetry import (
    DashboardSnapshot,
    DockerSnapshot,
    HandbookRoute,
    HostSnapshot,
    ProjectInventorySnapshot,
    TelemetryError,
    TunnelSnapshot,
)
from aipm.services.telemetry.docker import DockerTelemetryService
from aipm.services.telemetry.host import HostTelemetryService
from aipm.services.telemetry.project import ProjectTelemetryService
from aipm.services.telemetry.tunnel import TunnelTelemetryService


class DashboardTelemetryService:
    """Collect and aggregate telemetry while isolating component failures."""

    def __init__(
        self,
        host: HostTelemetryService,
        docker: DockerTelemetryService,
        projects: ProjectTelemetryService,
        tunnel: TunnelTelemetryService,
        *,
        handbook: tuple[HandbookRoute, ...] = (),
        clock: Callable[[], datetime] | None = None,
        logger: Any | None = None,
    ) -> None:
        self.host = host
        self.docker = docker
        self.projects = projects
        self.tunnel = tunnel
        self.handbook = handbook
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.logger = logger

    def snapshot(self) -> DashboardSnapshot:
        host = self._collect_host()
        docker = self._collect_docker()
        projects = self._collect_projects()
        tunnel = self._collect_tunnel(docker)
        return DashboardSnapshot(
            generated_at=self.clock(),
            host=host,
            docker=docker,
            projects=projects,
            tunnel=tunnel,
            handbook=self.handbook,
        )

    def _collect_host(self) -> HostSnapshot:
        try:
            return self.host.snapshot()
        except Exception as exc:
            error = self._error("HOST_TELEMETRY_FAILED", "Host telemetry unavailable", exc)
            return HostSnapshot.unavailable(error)

    def _collect_docker(self) -> DockerSnapshot:
        try:
            return self.docker.snapshot()
        except Exception as exc:
            error = self._error("DOCKER_TELEMETRY_FAILED", "Docker telemetry unavailable", exc)
            return DockerSnapshot.unavailable_snapshot(error)

    def _collect_projects(self) -> ProjectInventorySnapshot:
        try:
            return self.projects.snapshot()
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
