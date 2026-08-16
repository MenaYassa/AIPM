from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aipm.models.finding import Severity
from aipm.models.incidents import Incident, IncidentFilter, IncidentStatus
from aipm.repositories.incidents.base import IncidentRepository


class IncidentQueryService:
    MAX_LIMIT = 5000
    RANGES = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}

    def __init__(self, repository: IncidentRepository, clock=None):
        self.repository = repository
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

    def get(self, incident_id: int) -> Incident | None:
        return self.repository.get_incident(incident_id)

    def acknowledge(self, incident_id: int) -> Incident | None:
        return self.repository.acknowledge(incident_id, self.clock().astimezone(timezone.utc))
