from __future__ import annotations

from typing import Any

from aipm.models.incidents import Incident
from aipm.mappers.events import EventResponseMapper


class IncidentResponseMapper:
    def __init__(self, event_mapper: EventResponseMapper | None = None):
        self.event_mapper = event_mapper or EventResponseMapper()

    def to_dict(self, incident: Incident) -> dict[str, Any]:
        return {
            "id": incident.id,
            "incident_key": incident.incident_key,
            "title": incident.title,
            "severity": incident.severity.value,
            "status": incident.status.value,
            "started_at": incident.started_at.isoformat(),
            "updated_at": incident.updated_at.isoformat(),
            "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
            "resource": {
                "type": incident.resource.resource_type.value,
                "identifier": incident.resource.identifier,
                "name": incident.resource.name,
                "project_path": incident.resource.project_path,
            },
            "correlation_key": incident.correlation_key,
            "summary": incident.summary,
            "events": [self.event_mapper.to_dict(event) for event in incident.events],
        }

    def list(self, incidents: list[Incident]) -> dict[str, Any]:
        return {"available": True, "status": "ok", "error": None, "incidents": [self.to_dict(item) for item in incidents]}

    def unavailable(self, message: str = "Incidents unavailable") -> dict[str, Any]:
        return {"available": False, "status": "unavailable", "error": message, "incidents": []}
