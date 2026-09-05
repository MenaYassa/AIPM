from __future__ import annotations

from dataclasses import dataclass

from aipm.capabilities.dashboard.api import DashboardApi
from aipm.capabilities.dashboard.docker_api import DashboardDockerApi
from aipm.capabilities.dashboard.history_api import DashboardHistoryApi
from aipm.capabilities.dashboard.incidents_api import DashboardIncidentsApi
from aipm.capabilities.dashboard.logs_api import DashboardLogsApi
from aipm.capabilities.dashboard.notifications_api import DashboardNotificationsApi
from aipm.capabilities.dashboard.project_api import DashboardProjectApi
from aipm.capabilities.dashboard.server_api import DashboardServerApi
from aipm.capabilities.dashboard.service_health_api import DashboardServiceHealthApi
from aipm.capabilities.dashboard.settings_api import DashboardSettingsApi
from aipm.capabilities.dashboard.systemd_api import DashboardSystemdApi
from aipm.capabilities.dashboard.update_api import DashboardUpdateApi
from aipm.core.app import Application


@dataclass(frozen=True, slots=True)
class MissionControlContext:
    """Shared read-only Mission Control façade composition.

    This object owns dependency wiring only. Observation business logic remains in
    the existing capability façades, services, providers, and repositories.
    """

    application: Application
    dashboard: DashboardApi
    incidents: DashboardIncidentsApi
    notifications: DashboardNotificationsApi
    service_health: DashboardServiceHealthApi
    server: DashboardServerApi
    docker: DashboardDockerApi
    projects: DashboardProjectApi
    systemd: DashboardSystemdApi
    logs: DashboardLogsApi
    settings: DashboardSettingsApi
    update: DashboardUpdateApi

    @classmethod
    def from_application(
        cls,
        application: Application,
        *,
        dashboard: DashboardApi | None = None,
        incidents: DashboardIncidentsApi | None = None,
        notifications: DashboardNotificationsApi | None = None,
        service_health: DashboardServiceHealthApi | None = None,
        server: DashboardServerApi | None = None,
        docker: DashboardDockerApi | None = None,
        projects: DashboardProjectApi | None = None,
        systemd: DashboardSystemdApi | None = None,
        logs: DashboardLogsApi | None = None,
        settings: DashboardSettingsApi | None = None,
        update: DashboardUpdateApi | None = None,
    ) -> "MissionControlContext":
        dashboard_api = dashboard or DashboardApi.from_application(application, include_history=True)
        incidents_api = incidents or DashboardIncidentsApi.from_application(application)
        notifications_api = notifications or DashboardNotificationsApi.from_application(application)
        health_api = service_health or DashboardServiceHealthApi.from_application(
            application,
            dashboard_api=dashboard_api,
            incidents_api=incidents_api,
        )
        return cls(
            application=application,
            dashboard=dashboard_api,
            incidents=incidents_api,
            notifications=notifications_api,
            service_health=health_api,
            server=server
            or DashboardServerApi.from_application(
                application,
                dashboard_api=dashboard_api,
                incidents_api=incidents_api,
                service_health_api=health_api,
            ),
            docker=docker or DashboardDockerApi.from_application(application, dashboard_api=dashboard_api),
            projects=projects or DashboardProjectApi.from_application(application, dashboard_api=dashboard_api),
            systemd=systemd or DashboardSystemdApi.from_application(application),
            logs=logs or DashboardLogsApi.from_application(application),
            settings=settings
            or DashboardSettingsApi.from_application(
                application,
                service_health_api=health_api,
            ),
            update=update or DashboardUpdateApi.from_application(application, dashboard_api=dashboard_api),
        )

    @property
    def history(self) -> DashboardHistoryApi | None:
        """Return the already-composed history façade when telemetry exposes it."""

        return getattr(self.dashboard, "history_api", None)
