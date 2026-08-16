from __future__ import annotations

from datetime import datetime, timedelta, timezone

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

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Event query clock must be timezone-aware.")
        return value.astimezone(timezone.utc)
