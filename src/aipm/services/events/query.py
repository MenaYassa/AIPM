from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

from aipm.models.pagination import CursorError, KeysetCursor

from aipm.models.events import Event, EventFilter, EventType, ResourceType
from aipm.models.finding import Severity
from aipm.repositories.events.base import EventRepository


class EventQueryService:
    MAX_LIMIT = 5000
    RANGES = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}

    def __init__(self, repository: EventRepository, clock=None):
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def list(self, *, range_name: str = "24h", limit: int = 500, severity: str | None = None, event_type: str | None = None, resource_type: str | None = None, resource_id: str | None = None) -> list[Event]:
        end = self._utc(self.clock())
        if range_name not in self.RANGES:
            raise ValueError("Unsupported event range.")
        if limit <= 0 or limit > self.MAX_LIMIT:
            raise ValueError(f"Event limit must be between 1 and {self.MAX_LIMIT}.")
        return self.repository.get_events(EventFilter(
            start=end - timedelta(seconds=self.RANGES[range_name]),
            end=end,
            severity=Severity(severity) if severity else None,
            event_type=EventType(event_type) if event_type else None,
            resource_type=ResourceType(resource_type) if resource_type else None,
            resource_id=resource_id,
            limit=limit,
        ))

    def page(self, *, range_name: str = "24h", limit: int = 500, severity: str | None = None, event_type: str | None = None, resource_type: str | None = None, resource_id: str | None = None, cursor: str | None = None) -> tuple[list[Event], str | None]:
        if limit <= 0 or limit > self.MAX_LIMIT:
            raise ValueError(f"Event limit must be between 1 and {self.MAX_LIMIT}.")
        if range_name not in self.RANGES:
            raise ValueError("Unsupported event range.")
        fingerprint = self._fingerprint(range_name, severity, event_type, resource_type, resource_id)
        end = None
        start = None
        after = None
        if cursor:
            try:
                decoded = KeysetCursor.decode(cursor)
            except CursorError as exc:
                raise ValueError("Invalid event cursor") from exc
            if decoded.family != "events" or decoded.direction != "asc" or decoded.fingerprint != fingerprint or decoded.start_at is None or decoded.end_at is None:
                raise ValueError("Invalid event cursor")
            after = (decoded.occurred_at, decoded.item_id)
            start, end = decoded.start_at, decoded.end_at
        else:
            end = self._utc(self.clock())
            start = end - timedelta(seconds=self.RANGES[range_name])
        event_filter = EventFilter(
            start=start,
            end=end,
            severity=Severity(severity) if severity else None,
            event_type=EventType(event_type) if event_type else None,
            resource_type=ResourceType(resource_type) if resource_type else None,
            resource_id=resource_id,
            limit=limit + 1,
        )
        items = self.repository.get_events_page(event_filter, after=after)
        has_more = len(items) > limit
        page = items[:limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = KeysetCursor("events", "asc", last.occurred_at, int(last.id or 0), fingerprint, start_at=start, end_at=end).encode()
        return page, next_cursor

    @staticmethod
    def _fingerprint(range_name: str, severity: str | None, event_type: str | None, resource_type: str | None, resource_id: str | None) -> str:
        material = "|".join(("events", range_name, severity or "", event_type or "", resource_type or "", resource_id or ""))
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Event query clock must be timezone-aware.")
        return value.astimezone(timezone.utc)
