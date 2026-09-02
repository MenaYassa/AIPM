from __future__ import annotations

import time
from datetime import datetime, timezone
from threading import Event
from typing import Any, Callable

from aipm.core.exceptions import ProviderError
from aipm.models.git import GitRepository
from aipm.models.history import ProjectHistoryPoint
from aipm.models.project import Project, ProjectCapabilities
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

    def snapshot(self, *, cancel_event: Event | None = None, deadline: float | None = None, bounded: bool = False) -> ProjectInventorySnapshot:
        """Perform a project discovery refresh and update the last-known cache."""
        sampled_at = self.clock()
        started = time.monotonic()
        search_paths = tuple(self.project_service.app.config.discovery.search_paths)
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("project discovery cancelled")
            if cancel_event is None and deadline is None and not bounded:
                projects = self.project_service.discover()
            else:
                projects = self.project_service.discover(cancel_event=cancel_event, deadline=deadline, bounded=True)
        except (ProviderError, TimeoutError) as exc:
            error = self._error("PROJECT_DISCOVERY_UNAVAILABLE", "Project discovery unavailable", exc)
            if self.logger is not None:
                self.logger.info("Project discovery finished", extra={"duration_ms": max(0, int((time.monotonic() - started) * 1000)), "project_count": 0, "status": "unavailable"})
            self._cached = ProjectInventorySnapshot(available=False, status="unavailable", search_paths=search_paths, error=error, freshness=TelemetryFreshness.from_sample(self._sampled_at, now=sampled_at, max_age_seconds=self.stale_after_seconds, available=False, error=error))
            return self._cached
        except Exception as exc:
            error = self._error("PROJECT_DISCOVERY_FAILED", "Project discovery unavailable", exc)
            if self.logger is not None:
                self.logger.info("Project discovery finished", extra={"duration_ms": max(0, int((time.monotonic() - started) * 1000)), "project_count": 0, "status": "failed"})
            self._cached = ProjectInventorySnapshot(available=False, status="unavailable", search_paths=search_paths, error=error, freshness=TelemetryFreshness.from_sample(self._sampled_at, now=sampled_at, max_age_seconds=self.stale_after_seconds, available=False, error=error))
            return self._cached
        self._sampled_at = sampled_at
        if self.logger is not None:
            self.logger.info("Project discovery finished", extra={"duration_ms": max(0, int((time.monotonic() - started) * 1000)), "project_count": len(projects), "status": "healthy"})
        self._cached = ProjectInventorySnapshot(available=True, status="healthy", search_paths=search_paths, projects=tuple(ProjectSnapshot(project=project) for project in projects), freshness=TelemetryFreshness.from_sample(sampled_at, now=sampled_at, max_age_seconds=self.stale_after_seconds))
        return self._cached

    def hydrate_projects(self, points: list[ProjectHistoryPoint], *, now: datetime | None = None) -> None:
        """Seed the cache from persisted history so the dashboard shows projects without waiting for discovery."""
        now = now or self.clock()
        if not points:
            return
        search_paths = tuple(self.project_service.app.config.discovery.search_paths)
        snapshots = []
        for point in points:
            git = GitRepository(exists=True, branch=point.branch, dirty=bool(point.dirty), ahead=point.ahead or 0, behind=point.behind or 0) if point.branch else None
            project = Project(name=point.name, path=point.path, capabilities=ProjectCapabilities(has_git=point.has_git, has_compose=point.has_compose), git=git)
            snapshots.append(ProjectSnapshot(project=project))
        self._sampled_at = max(point.sampled_at for point in points)
        self._cached = ProjectInventorySnapshot(available=True, status="healthy", search_paths=search_paths, projects=tuple(snapshots), freshness=TelemetryFreshness.from_sample(self._sampled_at, now=now, max_age_seconds=self.stale_after_seconds))

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
