from __future__ import annotations

from typing import Any

from aipm.core.exceptions import DockerError
from aipm.mappers.docker import DockerMapper
from aipm.models.telemetry import ContainerSnapshot, DockerSnapshot, ResourceStats, TelemetryError
from aipm.services.docker.service import DockerService


class DockerTelemetryService:
    """Collect read-only Docker state through AIPM's provider boundary."""

    def __init__(self, docker_service: DockerService, *, logger: Any | None = None) -> None:
        self.docker_service = docker_service
        self.logger = logger

    def snapshot(self) -> DockerSnapshot:
        try:
            raw_containers = self.docker_service.provider.list_containers()
        except DockerError as exc:
            error = self._error("DOCKER_TELEMETRY_UNAVAILABLE", "Docker telemetry unavailable", exc)
            return DockerSnapshot.unavailable_snapshot(error)
        except Exception as exc:
            error = self._error("DOCKER_TELEMETRY_FAILED", "Docker telemetry unavailable", exc)
            return DockerSnapshot.unavailable_snapshot(error)

        snapshots = []
        for raw_container in sorted(raw_containers, key=lambda item: getattr(item, "name", "").lower()):
            base = DockerMapper.container(raw_container)
            stats = None
            stats_error = None
            if base.state == "running":
                try:
                    stats = self.docker_service.provider.stats(raw_container)
                except Exception as exc:
                    stats_error = self._error("CONTAINER_STATS_UNAVAILABLE", "Container resource telemetry unavailable", exc)
            snapshot = DockerMapper.container_snapshot(raw_container, stats)
            if stats_error is not None:
                snapshot = ContainerSnapshot(
                    container=snapshot.container,
                    resources=ResourceStats(
                        cpu_percent=None,
                        memory_used_mb=None,
                        memory_limit_mb=None,
                        memory_percent=None,
                        available=False,
                        error=stats_error,
                    ),
                    restart_count=snapshot.restart_count,
                    started_at=snapshot.started_at,
                )
            snapshots.append(snapshot)

        return DockerSnapshot(available=True, status="healthy", containers=tuple(snapshots))

    def _error(self, code: str, message: str, exc: Exception) -> TelemetryError:
        if self.logger is not None:
            self.logger.exception(message, exc_info=exc)
        return TelemetryError(code=code, message=message)
