from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

from aipm.models.mission_control_evidence import TimelineEntry
from aipm.models.pagination import CursorError, KeysetCursor

from aipm.models.events import Event
from aipm.models.finding import Severity
from aipm.models.incidents import Incident, IncidentFilter, IncidentStatus
from aipm.repositories.incidents.base import IncidentRepository


class IncidentQueryService:
    MAX_LIMIT = 5000
    RANGES = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}

    def __init__(self, repository: IncidentRepository, clock=None, event_repository=None):
        self.repository = repository
        self.event_repository = event_repository or getattr(repository, "event_repository", None)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def list(self, *, range_name: str = "7d", limit: int = 500, status: str | None = None, severity: str | None = None, resource_id: str | None = None) -> list[Incident]:
        end = self.clock().astimezone(timezone.utc)
        if range_name not in self.RANGES:
            raise ValueError("Unsupported incident range.")
        if limit <= 0 or limit > self.MAX_LIMIT:
            raise ValueError(f"Incident limit must be between 1 and {self.MAX_LIMIT}.")
        return self.repository.get_incidents(IncidentFilter(
            status=IncidentStatus(status) if status else None,
            severity=Severity(severity) if severity else None,
            resource_id=resource_id,
            start=end - timedelta(seconds=self.RANGES[range_name]),
            end=end,
            limit=limit,
        ))

    def page(self, *, range_name: str = "7d", limit: int = 500, status: str | None = None, severity: str | None = None, resource_id: str | None = None, cursor: str | None = None) -> tuple[list[Incident], str | None]:
        if range_name not in self.RANGES:
            raise ValueError("Unsupported incident range.")
        if limit <= 0 or limit > self.MAX_LIMIT:
            raise ValueError(f"Incident limit must be between 1 and {self.MAX_LIMIT}.")
        fingerprint = self._fingerprint(range_name, status, severity, resource_id)
        end = None
        start = None
        before = None
        if cursor:
            try:
                decoded = KeysetCursor.decode(cursor)
            except CursorError as exc:
                raise ValueError("Invalid incident cursor") from exc
            if decoded.family != "incidents" or decoded.direction != "desc" or decoded.fingerprint != fingerprint or decoded.start_at is None or decoded.end_at is None:
                raise ValueError("Invalid incident cursor")
            before = (decoded.occurred_at, decoded.item_id)
            start, end = decoded.start_at, decoded.end_at
        else:
            end = self.clock().astimezone(timezone.utc)
            start = end - timedelta(seconds=self.RANGES[range_name])
        incident_filter = IncidentFilter(
            status=IncidentStatus(status) if status else None,
            severity=Severity(severity) if severity else None,
            resource_id=resource_id,
            start=start,
            end=end,
            limit=limit + 1,
        )
        items = self.repository.get_incidents_page(incident_filter, before=before)
        has_more = len(items) > limit
        page = items[:limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = KeysetCursor("incidents", "desc", last.updated_at, int(last.id or 0), fingerprint, start_at=start, end_at=end).encode()
        return page, next_cursor

    def timeline(self, incident_id: int, *, limit: int = 200, cursor: str | None = None) -> tuple[list[dict[str, object]], str | None, bool, dict[int, Event]]:
        if limit <= 0 or limit > 500:
            raise ValueError("Timeline limit is outside the supported bounds.")
        fingerprint = hashlib.sha256(f"timeline|{incident_id}".encode()).hexdigest()[:16]
        after = None
        if cursor:
            try:
                decoded = KeysetCursor.decode(cursor)
            except CursorError as exc:
                raise ValueError("Invalid timeline cursor") from exc
            if decoded.family != "timeline" or decoded.direction != "asc" or decoded.fingerprint != fingerprint:
                raise ValueError("Invalid timeline cursor")
            after = (decoded.occurred_at, decoded.item_id)
        rows = self.repository.get_timeline(incident_id, limit=limit + 1, after=after)
        has_more = len(rows) > limit
        page = rows[:limit]
        event_ids = tuple(int(row["event_id"]) for row in page if row.get("event_id") is not None)
        events_by_id = {int(event.id): event for event in self.event_repository.get_events_by_ids(event_ids)} if event_ids and self.event_repository is not None else {}
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = KeysetCursor("timeline", "asc", _datetime(last["occurred_at"]), int(last["id"]), fingerprint).encode()
        return page, next_cursor, has_more, events_by_id

    @staticmethod
    def _fingerprint(range_name: str, status: str | None, severity: str | None, resource_id: str | None) -> str:
        material = "|".join(("incidents", range_name, status or "", severity or "", resource_id or ""))
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def get(self, incident_id: int) -> Incident | None:
        return self.repository.get_incident(incident_id)


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromtimestamp(int(value), timezone.utc)
