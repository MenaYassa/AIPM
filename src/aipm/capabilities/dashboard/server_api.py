from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from aipm.capabilities.dashboard.service_health_api import DashboardServiceHealthApi
from aipm.core.app import Application
from aipm.mappers.server import ServerResponseMapper
from aipm.models.mission_control import Observation, ObservationError
from aipm.models.server import ServerHostSnapshot
from aipm.services.telemetry.host import HostTelemetryService


class DashboardServerApi:
    """Read-only façade for the detailed Server & Host Intelligence surface."""

    def __init__(
        self,
        host: HostTelemetryService,
        mapper: ServerResponseMapper,
        *,
        service_health_api: DashboardServiceHealthApi | None = None,
        incidents_api: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        stale_after_seconds: int = 45,
    ) -> None:
        self.host = host
        self.mapper = mapper
        self.service_health_api = service_health_api
        self.incidents_api = incidents_api
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.stale_after_seconds = stale_after_seconds

    @classmethod
    def from_application(
        cls,
        application: Application,
        *,
        dashboard_api: Any | None = None,
        incidents_api: Any | None = None,
        service_health_api: DashboardServiceHealthApi | None = None,
    ) -> "DashboardServerApi":
        from aipm.capabilities.dashboard.api import DashboardApi
        from aipm.capabilities.dashboard.incidents_api import DashboardIncidentsApi

        overview_api = dashboard_api or DashboardApi.from_application(application)
        event_api = incidents_api or DashboardIncidentsApi.from_application(application)
        health_api = service_health_api or DashboardServiceHealthApi.from_application(
            application,
            dashboard_api=overview_api,
            incidents_api=event_api,
        )
        host = getattr(getattr(overview_api, "telemetry", None), "host", None)
        if host is None:
            host = HostTelemetryService(system_service=application.system, logger=application.logger)
        return cls(
            host,
            ServerResponseMapper(),
            service_health_api=health_api,
            incidents_api=event_api,
            stale_after_seconds=max(45, int(application.config.telemetry.interval_seconds) * 3),
        )

    def server(self) -> dict[str, Any]:
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        try:
            snapshot = self.host.server_snapshot()
            error = None
            if snapshot.host.error is not None:
                error = ObservationError(snapshot.host.error.code, snapshot.host.error.message)
            observation = Observation.from_sample(
                snapshot.host,
                observed_at=now if snapshot.host.available else None,
                now=now,
                max_age_seconds=self.stale_after_seconds,
                available=snapshot.host.available,
                transport_ok=True,
                error=error,
            )
        except Exception:
            snapshot = ServerHostSnapshot(host=self.host_unavailable())
            observation = Observation.from_sample(
                None,
                observed_at=None,
                now=now,
                max_age_seconds=self.stale_after_seconds,
                available=False,
                transport_ok=False,
                error=ObservationError("SERVER_HOST_TELEMETRY_FAILED", "Host telemetry unavailable"),
            )
        health = self._health()
        incidents = self._incidents()
        return self.mapper.to_response(snapshot, observation, health=health, incidents=incidents)

    def _health(self) -> dict[str, Any]:
        if self.service_health_api is None:
            return {"available": False, "error": "Service health unavailable"}
        try:
            return self.service_health_api.services()
        except Exception:
            return {"available": False, "error": "Service health unavailable"}

    def _incidents(self) -> dict[str, Any]:
        if self.incidents_api is None:
            return {"available": False, "error": "Incident summary unavailable"}
        try:
            return self.incidents_api.incidents(range_name="7d", status="open", limit=20)
        except Exception:
            return {"available": False, "error": "Incident summary unavailable"}

    @staticmethod
    def host_unavailable():
        from aipm.models.telemetry import HostSnapshot, TelemetryError

        return HostSnapshot.unavailable(TelemetryError("SERVER_HOST_TELEMETRY_FAILED", "Host telemetry unavailable"))
