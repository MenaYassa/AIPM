from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from aipm.models.events import Event


class EventResponseMapper:
    def to_dict(self, event: Event) -> dict[str, Any]:
        return {
            "id": event.id,
            "event_key": event.event_key,
            "occurred_at": event.occurred_at.isoformat(),
            "event_type": event.event_type.value,
            "severity": event.severity.value,
            "source": event.source.value,
            "resource": {
                "type": event.resource.resource_type.value,
                "identifier": event.resource.identifier,
                "name": event.resource.name,
                "project_path": event.resource.project_path,
            },
            "title": event.title,
            "description": event.description,
            "previous_value": event.previous_value,
            "current_value": event.current_value,
            "source_run_id": event.source_run_id,
            "previous_run_id": event.previous_run_id,
            "correlation_key": event.correlation_key,
            "evidence": [
                {
                    "code": item.code,
                    "component": item.component,
                    "severity": item.severity.value,
                    "title": item.title,
                    "description": item.description,
                    "resource": item.resource,
                }
                for item in event.evidence
            ],
        }

    def list(self, events: list[Event]) -> dict[str, Any]:
        return {"available": True, "status": "ok", "error": None, "events": [self.to_dict(event) for event in events]}

    def unavailable(self, message: str = "Events unavailable") -> dict[str, Any]:
        return {"available": False, "status": "unavailable", "error": message, "events": []}
