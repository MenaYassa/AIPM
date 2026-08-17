from datetime import datetime, timezone

from fastapi.testclient import TestClient

from aipm.capabilities.dashboard.incidents_api import DashboardIncidentsApi
from aipm.dashboard.server import create_app
from aipm.mappers.events import EventResponseMapper
from aipm.mappers.incidents import IncidentResponseMapper
from aipm.models.events import Event, EventSource, EventType, ResourceRef, ResourceType
from aipm.models.finding import Severity
from aipm.models.incidents import Incident, IncidentStatus

UTC = timezone.utc
NOW = datetime(2026, 8, 16, tzinfo=UTC)


class FakeHistory:
    def host(self, range_name, limit):
        return {"available": True, "points": []}

    def containers(self, name, range_name, limit):
        return {"available": True, "points": []}

    def projects(self, name, range_name, limit):
        return {"available": True, "points": []}

    def tunnel(self, range_name, limit):
        return {"available": True, "points": []}


class FakeDashboard:
    history_api = FakeHistory()

    def overview(self):
        return {"generated_at": NOW.isoformat(), "host": {}, "docker": {}, "tunnel": {}, "projects": {}, "handbook": []}


def make_event() -> Event:
    return Event(1, "event-key", NOW, EventType.CONTAINER_RESTARTING, Severity.HIGH, EventSource.DERIVED, ResourceRef(ResourceType.CONTAINER, "cid", "app"), "Restarting", "evidence", "running", "restarting", 2, 1, "container:cid:stability")


class FakeEventQuery:
    def __init__(self):
        self.item = make_event()

    def list(self, **filters):
        if filters.get("severity") == "bad":
            raise ValueError("bad severity")
        return [self.item]

    def get(self, event_id):
        return self.item if event_id == 1 else None

    @property
    def repository(self):
        return self

    def get_event(self, event_id):
        return self.get(event_id)


class FakeIncidentQuery:
    def __init__(self):
        self.item = Incident(1, "incident:key", "Container instability", Severity.HIGH, IncidentStatus.OPEN, NOW, NOW, None, make_event().resource, "container:cid:stability", "Repeated restart evidence", (make_event(),))

    def list(self, **filters):
        return [self.item]

    def get(self, incident_id):
        return self.item if incident_id == 1 else None

    def acknowledge(self, incident_id):
        return self.item


api = DashboardIncidentsApi(FakeEventQuery(), FakeIncidentQuery(), EventResponseMapper(), IncidentResponseMapper())
client = TestClient(create_app(dashboard_api=FakeDashboard(), incidents_api=api))


def test_event_and_incident_routes():
    events = client.get("/api/events?severity=high&resource_id=cid")
    assert events.status_code == 200
    assert events.json()["events"][0]["event_type"] == "container_restarting"

    incident = client.get("/api/incidents/1")
    assert incident.status_code == 200
    assert incident.json()["incident"]["status"] == "open"
    assert incident.json()["incident"]["events"]


def test_invalid_event_filter_is_safe():
    response = client.get("/api/events?severity=bad")
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert "bad severity" not in response.text


def test_dashboard_does_not_expose_acknowledgement_action():
    response = client.post("/api/incidents/1/acknowledge")
    assert response.status_code == 404
