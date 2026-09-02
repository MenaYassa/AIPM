from __future__ import annotations

import time
from typing import Any, Callable

from aipm.capabilities.dashboard.history_api import DashboardHistoryApi
from aipm.capabilities.dashboard.routes import handbook_routes
from aipm.core.app import Application
from aipm.mappers.dashboard import DashboardResponseMapper
from aipm.services.project.service import ProjectService
from aipm.services.telemetry.dashboard import DashboardTelemetryService
from aipm.services.telemetry.docker import DockerTelemetryService
from aipm.services.telemetry.host import HostTelemetryService
from aipm.services.telemetry.project import ProjectTelemetryService
from aipm.services.telemetry.tunnel import TunnelTelemetryService
from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository


class DashboardApi:
    """AIPM capability façade for the read-only Mission Control overview."""

    def __init__(
        self,
        telemetry: DashboardTelemetryService,
        mapper: DashboardResponseMapper,
        history_api: DashboardHistoryApi | None = None,
        *,
        project_history_refresher: Callable[[], None] | None = None,
        project_history_refresh_interval_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.telemetry = telemetry
        self.mapper = mapper
        self.history_api = history_api
        self._project_history_refresher = project_history_refresher
        self._project_history_refresh_interval_seconds = max(1.0, float(project_history_refresh_interval_seconds))
        self._last_project_history_refresh: float | None = None
        self._clock = clock or time.monotonic

    @classmethod
    def from_application(cls, application: Application, *, include_history: bool = False) -> "DashboardApi":
        project_service = ProjectService(app=application)
        telemetry = DashboardTelemetryService(
            host=HostTelemetryService(
                system_service=application.system,
                logger=application.logger,
            ),
            docker=DockerTelemetryService(
                docker_service=application.docker,
                logger=application.logger,
                resource_stale_after_seconds=application.config.telemetry.resource_stale_after_seconds,
            ),
            projects=ProjectTelemetryService(
                project_service=project_service,
                logger=application.logger,
                stale_after_seconds=application.config.telemetry.resource_stale_after_seconds,
            ),
            tunnel=TunnelTelemetryService(logger=application.logger),
            handbook=handbook_routes(),
            logger=application.logger,
        )
        if application.config.telemetry.enabled:
            repository = None
            try:
                repository = SQLiteHistoryRepository(application.config.telemetry.database_path, read_only=True)
                telemetry.docker.hydrate_resources(repository.get_latest_resource_samples())
                telemetry.projects.hydrate_projects(repository.get_latest_project_samples())
            except Exception as exc:
                application.logger.exception("Latest telemetry resource cache unavailable", exc_info=exc)
            finally:
                if repository is not None:
                    repository.close()
            project_history_refresher: Callable[[], None] | None = _project_history_refresher(application, telemetry.projects)
        else:
            project_history_refresher = None
        history_api = DashboardHistoryApi.from_application(application) if include_history else None
        return cls(
            telemetry=telemetry,
            mapper=DashboardResponseMapper(),
            history_api=history_api,
            project_history_refresher=project_history_refresher,
            project_history_refresh_interval_seconds=application.config.telemetry.project_interval_seconds,
        )

    def overview(self) -> dict[str, Any]:
        self._maybe_refresh_project_history()
        snapshot = self.telemetry.fast_snapshot() if hasattr(self.telemetry, "fast_snapshot") else self.telemetry.snapshot()
        return self.mapper.to_response(snapshot)

    def _maybe_refresh_project_history(self) -> None:
        """Re-hydrate the cached project snapshot from persisted history so freshness tracks new samples without a restart."""
        if self._project_history_refresher is None:
            return
        now = self._clock()
        if self._last_project_history_refresh is not None and now - self._last_project_history_refresh < self._project_history_refresh_interval_seconds:
            return
        self._last_project_history_refresh = now
        try:
            self._project_history_refresher()
        except Exception as exc:
            if self.telemetry.logger is not None:
                self.telemetry.logger.exception("Project telemetry history refresh unavailable", exc_info=exc)


def _project_history_refresher(application: Application, projects: ProjectTelemetryService) -> Callable[[], None]:
    """Build a re-hydration callable that reads persisted project samples through the history repository."""

    def refresh() -> None:
        repository = SQLiteHistoryRepository(application.config.telemetry.database_path, read_only=True)
        try:
            projects.hydrate_projects(repository.get_latest_project_samples())
        finally:
            repository.close()

    return refresh
