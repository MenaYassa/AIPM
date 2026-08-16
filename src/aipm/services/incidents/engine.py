from __future__ import annotations

import json

from aipm.models.events import Event, EventType
from aipm.models.finding import Severity
from aipm.repositories.incidents.base import IncidentRepository


class IncidentEngine:
    """Group deterministic events using explicit resource/family correlation keys."""

    def __init__(self, repository: IncidentRepository):
        self.repository = repository

    def apply(self, events: tuple[Event, ...] | list[Event]) -> int:
        changed = 0
        for event in events:
            opens = self._opens_incident(event)
            resolves = self._resolves_incident(event)
            incident = self.repository.apply_event(event, opens_incident=opens, resolves_incident=resolves)
            if incident is not None and (opens or resolves):
                changed += 1
        return changed

    def acknowledge(self, incident_id: int, acknowledged_at):
        return self.repository.acknowledge(incident_id, acknowledged_at)

    @staticmethod
    def _opens_incident(event: Event) -> bool:
        if event.event_type in {
            EventType.CONTAINER_RESTARTING,
            EventType.CONTAINER_RESTARTED,
            EventType.CONTAINER_STOPPED,
        }:
            return True
        if event.event_type is EventType.CONTAINER_HEALTH_CHANGED:
            return event.current_value not in {None, "healthy"}
        if event.event_type is EventType.PROJECT_GIT_STATE_CHANGED:
            return _project_unhealthy(event.current_value)
        if event.event_type is EventType.TUNNEL_STATE_CHANGED:
            return event.current_value == "down"
        if event.event_type is EventType.HEALTH_STATE_CHANGED:
            return event.current_value in {"degraded", "critical"}
        if event.event_type is EventType.HEALTH_FINDING_CHANGED:
            return event.severity in {Severity.WARNING, Severity.HIGH, Severity.CRITICAL}
        return False

    @staticmethod
    def _resolves_incident(event: Event) -> bool:
        if event.event_type is EventType.CONTAINER_RECOVERED:
            return True
        if event.event_type is EventType.CONTAINER_HEALTH_CHANGED:
            return event.current_value == "healthy"
        if event.event_type is EventType.TUNNEL_STATE_CHANGED:
            return event.current_value == "healthy"
        if event.event_type is EventType.HEALTH_STATE_CHANGED:
            return event.current_value == "healthy"
        if event.event_type is EventType.PROJECT_GIT_STATE_CHANGED:
            return not _project_unhealthy(event.current_value)
        return False


def _project_unhealthy(value: str | None) -> bool:
    if not value:
        return False
    try:
        state = json.loads(value)
    except json.JSONDecodeError:
        return False
    return bool(state.get("dirty") or (state.get("behind") or 0) > 0)
