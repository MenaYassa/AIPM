from __future__ import annotations

from typing import Any

from aipm.core.app import Application
from aipm.mappers.telemetry_history import HistoryResponseMapper
from aipm.models.history import HistoryResponse
from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository
from aipm.services.telemetry.history import HistoricalQueryService


class DashboardHistoryApi:
    """History capability façade; SQL and repository failures remain internal."""

    def __init__(self, query_service: HistoricalQueryService | None, mapper: HistoryResponseMapper, logger: Any | None = None) -> None:
        self.query_service = query_service
        self.mapper = mapper
        self.logger = logger

    @classmethod
    def from_application(cls, application: Application) -> "DashboardHistoryApi":
        query_service = None
        if application.config.telemetry.enabled:
            try:
                repository = SQLiteHistoryRepository(application.config.telemetry.database_path)
                query_service = HistoricalQueryService(repository, logger=application.logger)
            except Exception as exc:
                application.logger.exception("Historical telemetry repository unavailable", exc_info=exc)
        return cls(query_service=query_service, mapper=HistoryResponseMapper(), logger=application.logger)

    def host(self, range_name: str = "24h", limit: int = 500) -> dict[str, Any]:
        return self._query("host", range_name, limit)

    def containers(self, name: str | None = None, range_name: str = "24h", limit: int = 500) -> dict[str, Any]:
        return self._query("containers", range_name, limit, name=name)

    def projects(self, name: str | None = None, range_name: str = "24h", limit: int = 500) -> dict[str, Any]:
        return self._query("projects", range_name, limit, name=name)

    def tunnel(self, range_name: str = "24h", limit: int = 500) -> dict[str, Any]:
        return self._query("tunnel", range_name, limit)

    def _query(self, kind: str, range_name: str, limit: int, name: str | None = None) -> dict[str, Any]:
        if self.query_service is None:
            return self.mapper.to_response(_unavailable("Historical telemetry unavailable"))
        try:
            query = self.query_service.query_from_range(range_name, limit)
            if kind == "host":
                response = self.query_service.host(query)
            elif kind == "containers":
                response = self.query_service.containers(query, name=name)
            elif kind == "projects":
                response = self.query_service.projects(query, name=name)
            else:
                response = self.query_service.tunnel(query)
            return self.mapper.to_response(response)
        except ValueError:
            return self.mapper.to_response(_unavailable("Invalid history query"))
        except Exception as exc:
            if self.logger is not None:
                self.logger.exception("Historical dashboard API failed", exc_info=exc)
            return self.mapper.to_response(_unavailable("Historical telemetry unavailable"))


def _unavailable(message: str) -> HistoryResponse:
    return HistoryResponse(available=False, status="unavailable", error=message, points=())
