from __future__ import annotations

from typing import Any

from aipm.core.exceptions import ProviderError
from aipm.models.telemetry import ProjectInventorySnapshot, ProjectSnapshot, TelemetryError
from aipm.services.project.service import ProjectService


class ProjectTelemetryService:
    """Expose the existing read-only project discovery path as telemetry."""

    def __init__(self, project_service: ProjectService, *, logger: Any | None = None) -> None:
        self.project_service = project_service
        self.logger = logger

    def snapshot(self) -> ProjectInventorySnapshot:
        search_paths = tuple(self.project_service.app.config.discovery.search_paths)
        try:
            projects = self.project_service.discover()
        except ProviderError as exc:
            error = self._error("PROJECT_DISCOVERY_UNAVAILABLE", "Project discovery unavailable", exc)
            return ProjectInventorySnapshot.unavailable_snapshot(error)
        except Exception as exc:
            error = self._error("PROJECT_DISCOVERY_FAILED", "Project discovery unavailable", exc)
            return ProjectInventorySnapshot.unavailable_snapshot(error)
        return ProjectInventorySnapshot(
            available=True,
            status="healthy",
            search_paths=search_paths,
            projects=tuple(ProjectSnapshot(project=project) for project in projects),
        )

    def _error(self, code: str, message: str, exc: Exception) -> TelemetryError:
        if self.logger is not None:
            self.logger.exception(message, exc_info=exc)
        return TelemetryError(code=code, message=message)
