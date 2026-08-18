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
                IncidentQueryService(incident_repository),
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

    def acknowledge(self, incident_id: int) -> dict[str, Any]:
        if self.incidents_query is None:
            return self.incident_mapper.unavailable()
        try:
            result = self.incidents_query.acknowledge(incident_id)
            if result is None:
                return self.incident_mapper.unavailable("Incident not found")
            return {"available": True, "status": "ok", "error": None, "incident": self.incident_mapper.to_dict(result)}
        except Exception as exc:
            self._log("Incident acknowledgement failed", exc)
            return self.incident_mapper.unavailable("Incident acknowledgement unavailable")

    def _log(self, message: str, exc: Exception) -> None:
        if self.logger is not None:
            self.logger.exception(message, exc_info=exc)
