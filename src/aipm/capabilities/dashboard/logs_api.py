from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aipm.core.app import Application
from aipm.mappers.logs import LogsResponseMapper
from aipm.models.config import LoggingConfig
from aipm.models.logs import LogQuery, LogSource, source_registry
from aipm.models.mission_control import Observation, ObservationError, ObservationState
from aipm.providers.logs import FixedFileLogProvider, JournaldLogProvider
from aipm.services.logs.observation import ReadOnlyLogService


class DashboardLogsApi:
    """GET-only façade for backend-owned bounded log observations."""

    def __init__(self, service: ReadOnlyLogService, mapper: LogsResponseMapper | None = None) -> None:
        self.service = service
        self.mapper = mapper or LogsResponseMapper()

    @classmethod
    def from_application(cls, application: Application) -> "DashboardLogsApi":
        config = getattr(application, "config", None)
        logging_config = getattr(config, "logging", None)
        log_path = getattr(logging_config, "file", LoggingConfig().file)
        registry = source_registry(aipm_log_path=log_path)
        return cls(
            ReadOnlyLogService(
                registry,
                {"journald": JournaldLogProvider(), "file": FixedFileLogProvider()},
            )
        )

    def logs(
        self,
        *,
        source: str = "aipm-dashboard",
        since: str | None = None,
        until: str | None = None,
        severity: str | None = None,
        unit: str | None = None,
        project: str | None = None,
        limit: int = 200,
        max_bytes: int = 100_000,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        selected_source = self.service.registry.get(source)
        if unit is not None:
            valid_units = {item.unit_id for item in self.service.registry.values() if item.unit_id}
            if unit not in valid_units or (selected_source is not None and selected_source.unit_id != unit):
                return self._safe_error("INVALID_LOG_QUERY", "unit filter is not allow-listed for this source")
        try:
            query = LogQuery.build(
                source_id=source,
                since=since,
                until=until,
                severity=severity,
                unit_id=unit,
                project_id=project,
                limit=limit,
                max_bytes=max_bytes,
                cursor=cursor,
            )
        except ValueError as exc:
            return self._safe_error("INVALID_LOG_QUERY", str(exc))
        observation = self.service.read(query)
        response = self.mapper.response(observation, source=selected_source)
        response["sources"] = self.mapper.sources(list(self.service.registry.values()))
        return response

    def _safe_error(self, code: str, message: str) -> dict[str, Any]:
        safe = {"code": code[:64], "message": message[:256]}
        observation = Observation(
            transport_ok=True,
            available=False,
            state=ObservationState.ERROR,
            error=ObservationError(safe["code"], safe["message"]),
        )
        response = self.mapper.response(observation)
        response["sources"] = self.mapper.sources(list(self.service.registry.values()))
        return response
