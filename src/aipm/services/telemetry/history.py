from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from aipm.models.mission_control_evidence import ComparisonField, ComparisonSide, ComparisonStatus, HistoricalPoint, HistoryComparison, RelatedResourceLink

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

    def compare(self, *, resource_type: str, range_name: str = "24h", name: str | None = None, baseline: str | None = None, current: str | None = None) -> HistoryComparison:
        if resource_type not in {"host", "container", "project", "tunnel"}:
            raise ValueError("Unsupported comparison resource")
        if resource_type in {"container", "project"} and not name:
            raise ValueError("A resource identity is required for this comparison")
        if range_name not in self.RANGE_SECONDS:
            raise ValueError("Unsupported comparison range")
        current_at = _parse_timestamp(current) if current else _utc(self.clock())
        baseline_at = _parse_timestamp(baseline) if baseline else current_at - timedelta(seconds=self.RANGE_SECONDS[range_name])
        if baseline_at > current_at or current_at - baseline_at > timedelta(seconds=self.RANGE_SECONDS[range_name]):
            raise ValueError("Comparison window is outside the supported bounds")
        baseline_point = self._latest(resource_type, name, baseline_at)
        current_point = self._latest(resource_type, name, current_at)
        baseline_side = self._side(baseline_point)
        current_side = self._side(current_point)
        changes = self._changes(baseline_side, current_side)
        links = self._links(resource_type, name)
        available = baseline_side.available or current_side.available
        status = "ok" if available else "unavailable"
        return HistoryComparison(available, status, None if available else "Historical comparison unavailable", resource_type, name, baseline_side, current_side, tuple(changes), tuple(links))

    def _latest(self, resource_type: str, name: str | None, boundary: datetime) -> HistoricalPoint | None:
        if resource_type == "host":
            return self.repository.get_latest_host_at(boundary)
        if resource_type == "container":
            return self.repository.get_latest_container_at(name, boundary)
        if resource_type == "project":
            return self.repository.get_latest_project_at(name, boundary)
        return self.repository.get_latest_tunnel_at(boundary)

    @staticmethod
    def _side(point: HistoricalPoint | None) -> ComparisonSide:
        if point is None:
            return ComparisonSide(False, "missing", None, None, None)
        if isinstance(point, HistoricalPoint):
            raw_point, run_id = point.point, point.run_id
        else:
            raw_point, run_id = point, None
        values = asdict(raw_point)
        observed_at = values.pop("sampled_at", None)
        available = values.pop("available", True)
        status = "ok" if available else "unavailable"
        return ComparisonSide(bool(available), status, observed_at, run_id, values)

    @staticmethod
    def _changes(baseline: ComparisonSide, current: ComparisonSide) -> list[ComparisonField]:
        if not baseline.available and baseline.status == "missing":
            return [ComparisonField("__side__", ComparisonStatus.MISSING_BASELINE)]
        if not current.available and current.status == "missing":
            return [ComparisonField("__side__", ComparisonStatus.MISSING_CURRENT)]
        if not baseline.available:
            return [ComparisonField("__side__", ComparisonStatus.UNAVAILABLE_BASELINE)]
        if not current.available:
            return [ComparisonField("__side__", ComparisonStatus.UNAVAILABLE_CURRENT)]
        before = baseline.value or {}
        after = current.value or {}
        changes: list[ComparisonField] = []
        for name in sorted(set(before) | set(after)):
            left, right = before.get(name), after.get(name)
            if name not in before or name not in after:
                status = ComparisonStatus.INDETERMINATE
            elif left == right:
                status = ComparisonStatus.UNCHANGED
            elif left is None or right is None:
                status = ComparisonStatus.INDETERMINATE
            else:
                status = ComparisonStatus.CHANGED
            delta = right - left if isinstance(left, (int, float)) and isinstance(right, (int, float)) else None
            changes.append(ComparisonField(name, status, left, right, delta))
        return changes

    @staticmethod
    def _links(resource_type: str, name: str | None) -> list[RelatedResourceLink]:
        if resource_type == "host":
            return [RelatedResourceLink("server", "host", "Server", "/server")]
        if not name:
            return []
        route = "/docker" if resource_type == "container" else "/projects" if resource_type == "project" else "/dashboard"
        return [RelatedResourceLink(resource_type, name, name, route)]

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


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Comparison timestamp must be ISO-8601") from exc
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("History query timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc)
