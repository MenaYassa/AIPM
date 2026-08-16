from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from aipm.models.history import (
    ContainerHistoryPoint,
    HistoryQuery,
    HistoryResponse,
    HostHistoryPoint,
    ProjectHistoryPoint,
    TunnelHistoryPoint,
)
from aipm.repositories.telemetry.base import HistoryRepository


class HistoricalQueryService:
    """Query historical telemetry through the repository boundary."""

    MAX_LIMIT = 5000
    RANGE_SECONDS = {
        "1h": 3600,
        "6h": 21600,
        "24h": 86400,
        "7d": 604800,
    }

    def __init__(self, repository: HistoryRepository, *, logger: Any | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self.repository = repository
        self.logger = logger
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def query_from_range(self, range_name: str = "24h", limit: int = 500) -> HistoryQuery:
        if range_name not in self.RANGE_SECONDS:
            raise ValueError("Unsupported history range.")
        if limit <= 0 or limit > self.MAX_LIMIT:
            raise ValueError(f"History limit must be between 1 and {self.MAX_LIMIT}.")
        end = _utc(self.clock())
        return HistoryQuery(start=end - timedelta(seconds=self.RANGE_SECONDS[range_name]), end=end, limit=limit)

    def host(self, query: HistoryQuery) -> HistoryResponse:
        return self._safe(lambda: self.repository.get_host_history(query.start, query.end, query.limit))

    def containers(self, query: HistoryQuery, name: str | None = None) -> HistoryResponse:
        return self._safe(lambda: self.repository.get_container_history(name, query.start, query.end, query.limit))

    def projects(self, query: HistoryQuery, name: str | None = None) -> HistoryResponse:
        return self._safe(lambda: self.repository.get_project_history(name, query.start, query.end, query.limit))

    def resources(self, query: HistoryQuery, name: str | None = None) -> HistoryResponse:
        return self._safe(lambda: self.repository.get_resource_history(name, query.start, query.end, query.limit))

    def tunnel(self, query: HistoryQuery) -> HistoryResponse:
        return self._safe(lambda: self.repository.get_tunnel_history(query.start, query.end, query.limit))

    def _safe(self, query: Callable[[], list[object]]) -> HistoryResponse:
        try:
            points = tuple(query())
            return HistoryResponse(available=True, status="ok", error=None, points=points)
        except Exception as exc:
            if self.logger is not None:
                self.logger.exception("Historical telemetry query failed", exc_info=exc)
            return HistoryResponse(
                available=False,
                status="unavailable",
                error="Historical telemetry unavailable",
                points=(),
            )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("History query timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc)
