from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from aipm.capabilities.dashboard.api import DashboardApi
from aipm.capabilities.dashboard.history_api import DashboardHistoryApi
from aipm.mappers.dashboard import DashboardResponseMapper
from aipm.capabilities.dashboard.incidents_api import DashboardIncidentsApi
from aipm.mappers.events import EventResponseMapper
from aipm.mappers.incidents import IncidentResponseMapper
from aipm.mappers.telemetry_history import HistoryResponseMapper
from aipm.models.events import Event, EventSource, EventType, ResourceRef, ResourceType
from aipm.models.finding import Severity
from aipm.models.history import HostHistoryPoint
from aipm.models.incidents import Incident, IncidentStatus
from aipm.models.mission_control_evidence import HistoricalPoint
from aipm.models.pagination import CursorError, KeysetCursor
from aipm.services.events.query import EventQueryService
from aipm.services.incidents.query import IncidentQueryService
from aipm.services.telemetry.history import HistoricalQueryService
from aipm.dashboard.server import create_app

UTC = timezone.utc
NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def make_event(index: int, occurred_at: datetime | None = None) -> Event:
    return Event(index, f"event-{index}", occurred_at or NOW + timedelta(seconds=index), EventType.CONTAINER_RESTARTING, Severity.HIGH, EventSource.TELEMETRY, ResourceRef(ResourceType.CONTAINER, f"cid-{index}", f"container-{index}"), f"Event {index}", "safe description", None, "running", index, None, "corr")


def make_incident(index: int, updated_at: datetime | None = None) -> Incident:
    event = make_event(index, updated_at or NOW)
    return Incident(index, f"incident-{index}", f"Incident {index}", Severity.HIGH, IncidentStatus.OPEN, NOW, updated_at or NOW, None, event.resource, "corr", "safe summary", (event,))


class EventRepo:
    def __init__(self):
        self.events = [make_event(i, NOW + timedelta(seconds=i // 2)) for i in range(1, 6)]
        self.filters = []

    def get_events(self, event_filter):
        return self.events[:event_filter.limit]

    def get_events_page(self, event_filter, *, after=None):
        self.filters.append((event_filter.start, event_filter.end))
        items = self.events
        if after:
            items = [item for item in items if (item.occurred_at, item.id) > after]
        return items[:event_filter.limit]

    def get_event(self, event_id):
        return next((item for item in self.events if item.id == event_id), None)

    def get_events_by_ids(self, event_ids):
        self.batch_calls = getattr(self, "batch_calls", 0) + 1
        return [item for item in self.events if item.id in event_ids]


class IncidentRepo:
    def __init__(self):
        self.incidents = [make_incident(i, NOW + timedelta(seconds=i // 2)) for i in range(1, 6)]
        self.filters = []

    def get_incidents(self, incident_filter):
        return self.incidents[:incident_filter.limit]

    def get_incidents_page(self, incident_filter, *, before=None):
        self.filters.append((incident_filter.start, incident_filter.end))
        items = list(reversed(self.incidents))
        if before:
            items = [item for item in items if (item.updated_at, item.id) < before]
        return items[:incident_filter.limit]

    def get_incident(self, incident_id):
        return next((item for item in self.incidents if item.id == incident_id), None)

    def get_timeline(self, incident_id, *, limit, after=None):
        rows = [{"id": 1, "incident_id": incident_id, "transition": "opened", "occurred_at": int(NOW.timestamp()), "previous_status": None, "current_status": "open", "previous_severity": None, "current_severity": "high", "event_id": 1, "source_event_key": "event-1", "resource_type": "container", "resource_id": "cid-1", "resource_name": "container-1", "project_path": None}]
        if after:
            rows = []
        return rows[:limit]


class HistoryRepo:
    def __init__(self, baseline, current):
        self.baseline = HistoricalPoint(baseline, 101)
        self.current = HistoricalPoint(current, 202)

    def get_latest_host_at(self, end):
        return self.baseline if end < NOW else self.current

    def get_latest_container_at(self, name, end):
        return self.baseline if name == "container-1" and end < NOW else self.current if name == "container-1" else None

    def get_latest_project_at(self, name, end):
        return self.baseline if name == "project-1" and end < NOW else self.current if name == "project-1" else None

    def get_latest_tunnel_at(self, end):
        return self.baseline if end < NOW else self.current


def test_keyset_cursor_is_integrity_checked_and_bound():
    cursor = KeysetCursor("events", "asc", NOW, 4, "a" * 16).encode()
    assert KeysetCursor.decode(cursor).item_id == 4
    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    try:
        KeysetCursor.decode(tampered)
    except CursorError:
        pass
    else:
        raise AssertionError("tampered cursor accepted")


def test_event_page_has_no_duplicates_at_equal_timestamp():
    service = EventQueryService(EventRepo(), clock=lambda: NOW + timedelta(days=1))
    first, cursor = service.page(range_name="7d", limit=2)
    second, _ = service.page(range_name="7d", limit=2, cursor=cursor)
    assert [item.id for item in first] == [1, 2]
    assert [item.id for item in second] == [3, 4]
    assert not ({item.id for item in first} & {item.id for item in second})


def test_incident_page_preserves_descending_tie_break():
    service = IncidentQueryService(IncidentRepo(), clock=lambda: NOW + timedelta(days=1))
    first, cursor = service.page(range_name="7d", limit=2)
    second, _ = service.page(range_name="7d", limit=2, cursor=cursor)
    assert [item.id for item in first] == [5, 4]
    assert [item.id for item in second] == [3, 2]


def test_incident_timeline_uses_persisted_rows_and_bounds():
    event_repo = EventRepo()
    incident_repo = IncidentRepo()
    service = IncidentQueryService(incident_repo, clock=lambda: NOW, event_repository=event_repo)
    rows, cursor, more, events = service.timeline(1, limit=1)
    assert rows[0]["transition"] == "opened"
    assert cursor is None
    assert more is False
    assert set(events) == {1}
    assert event_repo.batch_calls == 1


def test_container_and_project_comparisons_require_identity_and_use_same_identity():
    baseline = HostHistoryPoint(NOW - timedelta(hours=1), "host", 10.0, 1.0, 1.0, 1.0, 8.0, 4.0, 4.0, 50.0, 0.0, 0.0, 0.0, 100.0, 40.0, 60.0, 40.0, 1, 2, True)
    current = HostHistoryPoint(NOW, "host", 20.0, 1.0, 1.0, 1.0, 8.0, 5.0, 3.0, 62.5, 0.0, 0.0, 0.0, 100.0, 50.0, 50.0, 50.0, 1, 2, True)
    service = HistoricalQueryService(HistoryRepo(baseline, current), clock=lambda: NOW)
    with pytest.raises(ValueError):
        service.compare(resource_type="container", range_name="24h")
    with pytest.raises(ValueError):
        service.compare(resource_type="project", range_name="24h")
    assert service.compare(resource_type="container", name="container-1", range_name="24h").resource_id == "container-1"
    assert service.compare(resource_type="project", name="project-1", range_name="24h").resource_id == "project-1"
    assert service.compare(resource_type="container", name="missing", range_name="24h").status == "unavailable"


def test_page_cursor_freezes_event_and_incident_time_boundary():
    event_repo = EventRepo()
    incident_repo = IncidentRepo()
    now = [NOW]
    event_service = EventQueryService(event_repo, clock=lambda: now[0])
    incident_service = IncidentQueryService(incident_repo, clock=lambda: now[0])
    _, event_cursor = event_service.page(range_name="7d", limit=2)
    _, incident_cursor = incident_service.page(range_name="7d", limit=2)
    first_event_bounds = event_repo.filters[-1]
    first_incident_bounds = incident_repo.filters[-1]
    now[0] = NOW + timedelta(days=2)
    event_service.page(range_name="7d", limit=2, cursor=event_cursor)
    incident_service.page(range_name="7d", limit=2, cursor=incident_cursor)
    assert event_repo.filters[-1] == first_event_bounds
    assert incident_repo.filters[-1] == first_incident_bounds


def test_history_comparison_exposes_changed_and_missing_without_zero_fill():
    baseline = HostHistoryPoint(NOW - timedelta(hours=1), "host", 10.0, 1.0, 1.0, 1.0, 8.0, 4.0, 4.0, 50.0, 0.0, 0.0, 0.0, 100.0, 40.0, 60.0, 40.0, 1, 2, True)
    current = HostHistoryPoint(NOW, "host", 20.0, 1.0, 1.0, 1.0, 8.0, 5.0, 3.0, 62.5, 0.0, 0.0, 0.0, 100.0, 50.0, 50.0, 50.0, 1, 2, True)
    response = HistoricalQueryService(HistoryRepo(baseline, current), clock=lambda: NOW).compare(resource_type="host", range_name="24h")
    assert response.baseline.run_id == 101
    assert response.current.run_id == 202
    statuses = {item.name: item.status.value for item in response.changes}
    assert statuses["cpu_percent"] == "changed"
    assert statuses["hostname"] == "unchanged"
    assert all(item.before is not None for item in response.changes if item.name != "__side__")


def test_dashboard_page_and_comparison_routes_remain_get_only():
    class FakeDashboard:
        pass

    events = EventQueryService(EventRepo(), clock=lambda: NOW)
    incidents = IncidentQueryService(IncidentRepo(), clock=lambda: NOW)
    incident_api = DashboardIncidentsApi(events, incidents, EventResponseMapper(), IncidentResponseMapper())
    history = DashboardHistoryApi(HistoricalQueryService(HistoryRepo(None, None), clock=lambda: NOW), HistoryResponseMapper())
    dashboard_api = DashboardApi(FakeDashboard(), DashboardResponseMapper(), history_api=history)
    client = TestClient(create_app(dashboard_api=dashboard_api, incidents_api=incident_api))
    assert client.get("/api/events?limit=2").status_code == 200
    assert client.get("/api/incidents?limit=2&cursor=bad").status_code == 200
    assert client.get("/api/incidents/1/timeline").status_code == 200
    assert client.get("/api/history/compare?resource_type=host").status_code == 200
    assert client.post("/api/incidents/1/timeline").status_code == 405


def test_frontend_uses_static_evidence_module_and_read_only_controls():
    from pathlib import Path

    html = Path("src/aipm/dashboard/static/index.html").read_text()
    module = Path("src/aipm/dashboard/static/mission-control-evidence.js").read_text()
    assert "from '/static/mission-control-evidence.js'" in html
    assert 'data-view="incidents"' in html and 'data-view="history"' in html
    assert 'id="incidentNext"' in html and 'id="comparisonRefresh"' in html
    assert "fetch(`/api/incidents/${encodeURIComponent(incidentId)}/timeline" in module
    assert "WebSocket" not in module and "EventSource" not in module
    assert "acknowledge" not in module.lower()
    assert "download" not in module.lower() and "EventSource" not in module
    assert "id=\"start\"" not in module and "id=\"restart\"" not in module
