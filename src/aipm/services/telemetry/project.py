from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from aipm.core.exceptions import ProviderError
from aipm.models.telemetry import ProjectInventorySnapshot, ProjectSnapshot, TelemetryError, TelemetryFreshness
from aipm.services.project.service import ProjectService


class ProjectTelemetryService:
    """Expose existing read-only project discovery with independently refreshed cache."""

    def __init__(self, project_service: ProjectService, *, logger: Any | None = None, clock: Callable[[], datetime] | None = None, stale_after_seconds: int = 180) -> None:
        self.project_service = project_service
        self.logger = logger
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.stale_after_seconds = stale_after_seconds
        self._cached: ProjectInventorySnapshot | None = None
        self._sampled_at: datetime | None = None

    def snapshot(self) -> ProjectInventorySnapshot:
        """Perform a project discovery refresh and update the last-known cache."""
        sampled_at = self.clock()
        search_paths = tuple(self.project_service.app.config.discovery.search_paths)
        try:
            projects = self.project_service.discover()
        except ProviderError as exc:
            error = self._error("PROJECT_DISCOVERY_UNAVAILABLE", "Project discovery unavailable", exc)
            self._cached = ProjectInventorySnapshot(available=False, status="unavailable", search_paths=search_paths, error=error, freshness=TelemetryFreshness.from_sample(self._sampled_at, now=sampled_at, max_age_seconds=self.stale_after_seconds, available=False, error=error))
            return self._cached
        except Exception as exc:
            error = self._error("PROJECT_DISCOVERY_FAILED", "Project discovery unavailable", exc)
            self._cached = ProjectInventorySnapshot(available=False, status="unavailable", search_paths=search_paths, error=error, freshness=TelemetryFreshness.from_sample(self._sampled_at, now=sampled_at, max_age_seconds=self.stale_after_seconds, available=False, error=error))
            return self._cached
        self._sampled_at = sampled_at
        self._cached = ProjectInventorySnapshot(available=True, status="healthy", search_paths=search_paths, projects=tuple(ProjectSnapshot(project=project) for project in projects), freshness=TelemetryFreshness.from_sample(sampled_at, now=sampled_at, max_age_seconds=self.stale_after_seconds))
        return self._cached

    def cached_snapshot(self, *, now: datetime | None = None) -> ProjectInventorySnapshot:
        now = now or self.clock()
        if self._cached is None:
            search_paths = tuple(self.project_service.app.config.discovery.search_paths)
            return ProjectInventorySnapshot(available=False, status="unknown", search_paths=search_paths, freshness=TelemetryFreshness.never_sampled(self.stale_after_seconds))
        freshness = TelemetryFreshness.from_sample(self._sampled_at, now=now, max_age_seconds=self.stale_after_seconds, available=self._cached.available, error=self._cached.error)
        return ProjectInventorySnapshot(available=self._cached.available, status="stale" if freshness.status.value == "stale" else self._cached.status, search_paths=self._cached.search_paths, projects=self._cached.projects, error=self._cached.error, freshness=freshness)

    def _error(self, code: str, message: str, exc: Exception) -> TelemetryError:
        if self.logger is not None:
            self.logger.exception(message, exc_info=exc)
        return TelemetryError(code=code, message=message)
