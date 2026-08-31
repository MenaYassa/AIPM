from __future__ import annotations

from datetime import datetime, timezone

from typing import Any

from aipm.core.app import Application
from aipm.mappers.events import EventResponseMapper
from aipm.mappers.incidents import IncidentResponseMapper
from aipm.repositories.events.sqlite import SQLiteEventRepository
from aipm.repositories.incidents.sqlite import SQLiteIncidentRepository
from aipm.services.events.query import EventQueryService
from aipm.services.incidents.query import IncidentQueryService


class DashboardIncidentsApi:
    """HTTP-facing façade for deterministic event and incident data."""

    def __init__(self, events: EventQueryService | None, incidents: IncidentQueryService | None, event_mapper=None, incident_mapper=None, logger: Any | None = None):
        self.events_query = events
        self.incidents_query = incidents
        self.event_mapper = event_mapper or EventResponseMapper()
        self.incident_mapper = incident_mapper or IncidentResponseMapper(self.event_mapper)
        self.logger = logger

    @classmethod
    def from_application(cls, application: Application) -> "DashboardIncidentsApi":
        if not application.config.events.enabled:
            return cls(None, None, logger=application.logger)
        try:
            database_path = application.config.telemetry.database_path
            event_repository = SQLiteEventRepository(database_path, read_only=True)
            incident_repository = SQLiteIncidentRepository(database_path, read_only=True)
            return cls(
                EventQueryService(event_repository),
                                    IncidentQueryService(incident_repository, event_repository=event_repository),

                logger=application.logger,
            )
        except Exception as exc:
            application.logger.exception("Event and incident repositories unavailable", exc_info=exc)
            return cls(None, None, logger=application.logger)

    def events(self, **filters) -> dict[str, Any]:
        if self.events_query is None:
            return self.event_mapper.unavailable()
        try:
            return self.event_mapper.list(self.events_query.list(**filters))
        except (ValueError, LookupError):
            return self.event_mapper.unavailable("Invalid event query")
        except Exception as exc:
            self._log("Event API failed", exc)
            return self.event_mapper.unavailable()

    def events_page(self, **filters) -> dict[str, Any]:
        if self.events_query is None:
            return self.event_mapper.unavailable()
        try:
            if not hasattr(self.events_query, "page"):
                filters.pop("cursor", None)
                return self.events(**filters)
            items, next_cursor = self.events_query.page(**filters)
            response = self.event_mapper.list(items)
            if filters.get("cursor") is not None or next_cursor is not None:
                response["next_cursor"] = next_cursor
                response["has_more"] = next_cursor is not None
            return response
        except (ValueError, LookupError):
            return self.event_mapper.unavailable("Invalid event query")
        except Exception as exc:
            self._log("Event page API failed", exc)
            return self.event_mapper.unavailable()

    def event(self, event_id: int) -> dict[str, Any]:
        if self.events_query is None:
            return self.event_mapper.unavailable()
        try:
            result = self.events_query.repository.get_event(event_id)
            if result is None:
                return self.event_mapper.unavailable("Event not found")
            return {"available": True, "status": "ok", "error": None, "event": self.event_mapper.to_dict(result)}
        except Exception as exc:
            self._log("Event detail API failed", exc)
            return self.event_mapper.unavailable()

    def incidents(self, **filters) -> dict[str, Any]:
        if self.incidents_query is None:
            return self.incident_mapper.unavailable()
        try:
            return self.incident_mapper.list(self.incidents_query.list(**filters))
        except (ValueError, LookupError):
            return self.incident_mapper.unavailable("Invalid incident query")
        except Exception as exc:
            self._log("Incident API failed", exc)
            return self.incident_mapper.unavailable()

    def incidents_page(self, **filters) -> dict[str, Any]:
        if self.incidents_query is None:
            return self.incident_mapper.unavailable()
        try:
            if not hasattr(self.incidents_query, "page"):
                filters.pop("cursor", None)
                return self.incidents(**filters)
            items, next_cursor = self.incidents_query.page(**filters)
            response = self.incident_mapper.list(items)
            if filters.get("cursor") is not None or next_cursor is not None:
                response["next_cursor"] = next_cursor
                response["has_more"] = next_cursor is not None
            return response
        except (ValueError, LookupError):
            return self.incident_mapper.unavailable("Invalid incident query")
        except Exception as exc:
            self._log("Incident page API failed", exc)
            return self.incident_mapper.unavailable()

    def timeline(self, incident_id: int, *, limit: int = 200, cursor: str | None = None) -> dict[str, Any]:
        if self.incidents_query is None:
            return {"available": False, "status": "unavailable", "error": "Incidents unavailable", "entries": []}
        try:
            incident = self.incidents_query.get(incident_id)
            if incident is None:
                return {"available": False, "status": "not_found", "error": "Incident not found", "entries": []}
            rows, next_cursor, has_more, events_by_id = self.incidents_query.timeline(incident_id, limit=limit, cursor=cursor)
            entries = []
            partial = False
            for row in rows:
                event = events_by_id.get(int(row["event_id"])) if row.get("event_id") is not None else None
                if row.get("event_id") is not None and event is None:
                    partial = True
                resource = {
                    "type": row.get("resource_type"),
                    "identifier": row.get("resource_id"),
                    "name": row.get("resource_name"),
                }
                entries.append({
                    "id": int(row["id"]),
                    "occurred_at": _iso(row.get("occurred_at")),
                    "transition": row.get("transition"),
                    "previous_status": row.get("previous_status"),
                    "current_status": row.get("current_status"),
                    "previous_severity": row.get("previous_severity"),
                    "current_severity": row.get("current_severity"),
                    "event_id": event.id if event is not None else row.get("event_id"),
                    "source_event_key": row.get("source_event_key"),
                    "resource": resource,
                    "title": event.title if event is not None else incident.title,
                    "summary": event.description if event is not None else incident.summary,
                    "links": ([{"kind": "event", "identifier": str(event.id), "label": event.title, "route": f"/api/events/{event.id}"}] if event is not None and event.id is not None else []),
                })
            return {"available": True, "status": "partial" if partial else "ok", "error": None, "entries": entries, "next_cursor": next_cursor, "has_more": has_more, "partial": partial}
        except (ValueError, LookupError):
            return {"available": False, "status": "error", "error": "Invalid incident timeline query", "entries": []}
        except Exception as exc:
            self._log("Incident timeline API failed", exc)
            return {"available": False, "status": "unavailable", "error": "Incident timeline unavailable", "entries": []}

    def incident(self, incident_id: int) -> dict[str, Any]:
        if self.incidents_query is None:
            return self.incident_mapper.unavailable()
        try:
            result = self.incidents_query.get(incident_id)
            if result is None:
                return self.incident_mapper.unavailable("Incident not found")
            return {"available": True, "status": "ok", "error": None, "incident": self.incident_mapper.to_dict(result)}
        except Exception as exc:
            self._log("Incident detail API failed", exc)
            return self.incident_mapper.unavailable()


    def _log(self, message: str, exc: Exception) -> None:
        if self.logger is not None:
            self.logger.exception(message, exc_info=exc)


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
