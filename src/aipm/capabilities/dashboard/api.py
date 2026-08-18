from __future__ import annotations

from typing import Any

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

    def __init__(self, telemetry: DashboardTelemetryService, mapper: DashboardResponseMapper, history_api: DashboardHistoryApi | None = None) -> None:
        self.telemetry = telemetry
        self.mapper = mapper
        self.history_api = history_api

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
            except Exception as exc:
                application.logger.exception("Latest telemetry resource cache unavailable", exc_info=exc)
            finally:
                if repository is not None:
                    repository.close()
        history_api = DashboardHistoryApi.from_application(application) if include_history else None
        return cls(telemetry=telemetry, mapper=DashboardResponseMapper(), history_api=history_api)

    def overview(self) -> dict[str, Any]:
        snapshot = self.telemetry.fast_snapshot() if hasattr(self.telemetry, "fast_snapshot") else self.telemetry.snapshot()
        return self.mapper.to_response(snapshot)
