from datetime import datetime, timedelta, timezone

import json
import subprocess
import textwrap

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
    assert "fetch(`/api/incidents/${encodeURIComponent(targetId)}/timeline?${params.toString()}`" in module
    assert "WebSocket" not in module and "EventSource" not in module
    assert "acknowledge" not in module.lower()
    assert "download" not in module.lower() and "EventSource" not in module
    assert "id=\"start\"" not in module and "id=\"restart\"" not in module



def test_frontend_timeline_continuation_preserves_opaque_cursor_and_partial_state():
    from pathlib import Path

    module = Path("src/aipm/dashboard/static/mission-control-evidence.js").read_text()
    opaque_cursor = "sig.v1/abc+DEF==;punctuation"

    assert "const params = new URLSearchParams();" in module
    assert "params.set('cursor', requestState.cursor)" in module
    assert "timelineCursor = data.next_cursor;" in module
    assert "timelineHasMore = data.has_more === true && typeof data.next_cursor === 'string' && data.next_cursor.length > 0;" in module
    assert "mergeTimelineEntries(incoming);" in module
    assert "if (entry && entry.id != null && !byId.has(String(entry.id)))" in module
    assert "timelinePartial = timelinePartial || data.partial === true;" in module
    assert "if (append && timelineIncidentId !== targetId) return;" in module
    assert "decodeURIComponent" not in module
    assert "JSON.parse" not in module
    assert "sign(cursor" not in module
    assert opaque_cursor not in module


def test_frontend_timeline_and_event_detail_surfaces_are_bounded_get_only():
    from pathlib import Path

    html = Path("src/aipm/dashboard/static/index.html").read_text()
    module = Path("src/aipm/dashboard/static/mission-control-evidence.js").read_text()

    assert 'id="timelineNext"' in module
    assert 'id="eventDetail"' in html
    assert 'id="eventDetailState"' in html
    assert 'data-event-id="${esc(entry.event_id)}"' in module
    assert "const safeId = safeEventId(eventId);" in module
    assert "return /^\\d+$/.test(candidate) ? candidate : null;" in module
    assert "fetch(`/api/events/${safeId}`" in module
    assert "eventDetail" in module
    assert "#/dashboard" not in module
    assert "POST" not in module
    assert "PUT" not in module
    assert "PATCH" not in module
    assert "DELETE" not in module
    assert "acknowledge" not in module.lower()
    assert "download" not in module.lower()
    assert "WebSocket" not in module
    assert "EventSource" not in module
    assert "innerHTML" in module and "esc(" in module



def test_frontend_timeline_inflight_guard_blocks_duplicate_and_stale_requests(tmp_path):
    from pathlib import Path

    module_source = Path("src/aipm/dashboard/static/mission-control-evidence.js").read_text()
    script = textwrap.dedent(
        f"""
        import assert from 'node:assert/strict';
        const source = {json.dumps(module_source)};
        const moduleUrl = `data:text/javascript,${{encodeURIComponent(source)}}`;
        class Element {{
          constructor(id) {{ this.id = id; this.innerHTML = ''; this.textContent = ''; this.disabled = false; }}
          addEventListener() {{}}
          querySelectorAll() {{ return []; }}
        }}
        const elements = new Map(['incidentTimeline', 'eventDetail', 'eventDetailState'].map(id => [id, new Element(id)]));
        globalThis.document = {{ getElementById: id => elements.get(id) || new Element(id) }};
        const {{ createEvidenceController }} = await import(moduleUrl);
        const controller = createEvidenceController({{ escapeHtml: value => String(value ?? '') }});
        const pending = [];
        const requests = [];
        globalThis.fetch = url => {{
          requests.push(String(url));
          return new Promise((resolve, reject) => pending.push({{ resolve, reject }}));
        }};
        const settle = (body, ok = true) => pending.shift().resolve({{ ok, async json() {{ return body; }} }});
        const rejectNext = error => pending.shift().reject(error);
        const parse = url => new URL(url, 'http://mission-control.test');

        const initial = controller.loadTimeline('incident-a');
        assert.equal(requests.length, 1);
        settle({{ available: true, entries: [{{ id: 1, transition: 'a-opened', title: 'A', event_id: null }}], next_cursor: 'sig.v1/opaque+cursor==;unicode-é', has_more: true, partial: true }});
        await initial;
        assert.equal(elements.get('incidentTimeline').innerHTML.includes('id="timelineNext"'), true);

        const firstContinuation = controller.loadTimeline('incident-a', {{ append: true }});
        const duplicateContinuation = controller.loadTimeline('incident-a', {{ append: true }});
        const thirdContinuation = controller.loadTimeline('incident-a', {{ append: true }});
        assert.equal(requests.length, 2);
        assert.equal(parse(requests[1]).searchParams.get('cursor'), 'sig.v1/opaque+cursor==;unicode-é');
        settle({{ available: true, entries: [{{ id: 2, transition: 'continued', title: 'A2-unique', event_id: null }}], next_cursor: 'next-2', has_more: true, partial: false }});
        await Promise.all([firstContinuation, duplicateContinuation, thirdContinuation]);
        assert.equal((elements.get('incidentTimeline').innerHTML.match(/A2-unique/g) || []).length, 1);
        assert.equal(elements.get('incidentTimeline').innerHTML.includes('Some persisted event evidence was unavailable'), true);

        const secondContinuation = controller.loadTimeline('incident-a', {{ append: true }});
        const duplicateSecond = controller.loadTimeline('incident-a', {{ append: true }});
        assert.equal(requests.length, 3);
        settle({{ available: true, entries: [{{ id: 3, transition: 'a-final', title: 'A3', event_id: null }}], next_cursor: null, has_more: false, partial: false }});
        await Promise.all([secondContinuation, duplicateSecond]);
        assert.equal(elements.get('incidentTimeline').innerHTML.includes('id="timelineNext"'), false);

        const failureInitial = controller.loadTimeline('incident-failure');
        assert.equal(requests.length, 4);
        settle({{ available: true, entries: [], next_cursor: 'failure-cursor', has_more: true, partial: false }});
        await failureInitial;
        const failed = controller.loadTimeline('incident-failure', {{ append: true }});
        assert.equal(requests.length, 5);
        await Promise.resolve();
        rejectNext(new Error('network failure'));
        await failed;
        const retry = controller.loadTimeline('incident-failure', {{ append: true }});
        assert.equal(requests.length, 6);
        settle({{ available: true, entries: [], next_cursor: null, has_more: false, partial: false }});
        await retry;

        const aInitial = controller.loadTimeline('incident-a');
        assert.equal(requests.length, 7);
        const bInitial = controller.loadTimeline('incident-b');
        assert.equal(requests.length, 8);
        settle({{ available: true, entries: [{{ id: 4, transition: 'a-stale', title: 'A stale', event_id: null }}], next_cursor: null, has_more: false, partial: false }});
        settle({{ available: true, entries: [{{ id: 5, transition: 'b-current', title: 'B current', event_id: null }}], next_cursor: null, has_more: false, partial: false }});
        await Promise.all([aInitial, bInitial]);
        const finalHtml = elements.get('incidentTimeline').innerHTML;
        assert.equal(finalHtml.includes('b-current'), true);
        assert.equal(finalHtml.includes('A stale'), false);

        const malformed = controller.loadTimeline('incident-c');
        assert.equal(requests.length, 9);
        settle({{ available: true, entries: [], has_more: true, partial: false }});
        await malformed;
        assert.equal(elements.get('incidentTimeline').innerHTML.includes('id="timelineNext"'), false);
        console.log('TIMELINE_INFLIGHT_GUARD=PASS');
        console.log('OPAQUE_CURSOR_EXACT_TRANSPORT=PASS');
        console.log('STALE_INCIDENT_RESPONSE_BLOCKED=PASS');
        console.log('PARTIAL_SEMANTICS_PRESERVED=PASS');
        """
    )
    script_path = tmp_path / "mc69_inflight_guard.mjs"
    script_path.write_text(script)
    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TIMELINE_INFLIGHT_GUARD=PASS" in result.stdout
    assert "OPAQUE_CURSOR_EXACT_TRANSPORT=PASS" in result.stdout
    assert "STALE_INCIDENT_RESPONSE_BLOCKED=PASS" in result.stdout



def test_frontend_event_detail_request_identity_blocks_stale_responses(tmp_path):
    from pathlib import Path

    module_source = Path("src/aipm/dashboard/static/mission-control-evidence.js").read_text()
    script = textwrap.dedent(
        f"""
        import assert from 'node:assert/strict';
        const source = {json.dumps(module_source)};
        const moduleUrl = `data:text/javascript,${{encodeURIComponent(source)}}`;
        class Element {{
          constructor(id) {{ this.id = id; this.innerHTML = ''; this.textContent = ''; this.disabled = false; }}
          addEventListener() {{}}
          querySelectorAll() {{ return []; }}
        }}
        const elements = new Map(['incidentTimeline', 'eventDetail', 'eventDetailState'].map(id => [id, new Element(id)]));
        globalThis.document = {{ getElementById: id => elements.get(id) || new Element(id) }};
        const {{ createEvidenceController }} = await import(moduleUrl);
        const controller = createEvidenceController({{ escapeHtml: value => String(value ?? '') }});
        const pending = [];
        const requests = [];
        globalThis.fetch = url => {{
          const item = {{ url: String(url), resolve: null, reject: null }};
          const promise = new Promise((resolve, reject) => {{ item.resolve = resolve; item.reject = reject; }});
          pending.push(item); requests.push(item); return promise;
        }};
        const response = event => ({{ ok: true, async json() {{ return {{ available: true, event }}; }} }});
        const timelineResponse = entries => ({{ ok: true, async json() {{ return {{ available: true, entries, next_cursor: null, has_more: false, partial: false }}; }} }});
        const take = fragment => {{ const index = pending.findIndex(item => item.url.includes(fragment)); return pending.splice(index, 1)[0]; }};
        const settle = (fragment, value) => take(fragment).resolve(value);
        const reject = (fragment, value) => take(fragment).reject(value);
        const eventData = (id, title) => ({{ id, event_key: `event-${{id}}`, title, description: title, occurred_at: '2026-01-01', event_type: 'test', severity: 'low', source: 'test', resource: {{ name: title }}, source_run_id: id, previous_run_id: null, correlation_key: title, evidence: [] }});

        const aTimeline = controller.loadTimeline('A');
        settle('/api/incidents/A/timeline', timelineResponse([{{ id: 1, event_id: 101, transition: 'opened', title: 'A timeline' }}]));
        await aTimeline;
        const a101 = controller.loadEventDetail('101');
        const bTimeline = controller.loadTimeline('B');
        settle('/api/incidents/B/timeline', timelineResponse([{{ id: 2, event_id: 202, transition: 'opened', title: 'B timeline' }}]));
        await bTimeline;
        const b202 = controller.loadEventDetail('202');
        settle('/api/events/202', response(eventData(202, 'Event Y')));
        await b202;
        settle('/api/events/101', response(eventData(101, 'Event X')));
        await a101;
        assert.equal(elements.get('eventDetail').innerHTML.includes('Event Y'), true);
        assert.equal(elements.get('eventDetail').innerHTML.includes('Event X'), false);

        const same101 = controller.loadEventDetail('101');
        const same202 = controller.loadEventDetail('202');
        settle('/api/events/202', response(eventData(202, 'Newest Y')));
        await same202;
        settle('/api/events/101', response(eventData(101, 'Older X')));
        await same101;
        assert.equal(elements.get('eventDetail').innerHTML.includes('Newest Y'), true);
        assert.equal(elements.get('eventDetail').innerHTML.includes('Older X'), false);

        const a303 = controller.loadEventDetail('303');
        const b404 = controller.loadEventDetail('404');
        settle('/api/events/303', response(eventData(303, 'A success')));
        await a303;
        reject('/api/events/404', new Error('B failed'));
        await b404;
        assert.equal(elements.get('eventDetail').innerHTML.includes('Event detail unavailable'), true);
        assert.equal(elements.get('eventDetail').innerHTML.includes('A success'), false);

        const oldReload = controller.loadEventDetail('505');
        const reload = controller.loadTimeline('B');
        settle('/api/incidents/B/timeline', timelineResponse([]));
        await reload;
        settle('/api/events/505', response(eventData(505, 'Stale reload')));
        await oldReload;
        assert.equal(elements.get('eventDetail').innerHTML.includes('Stale reload'), false);

        const a1 = controller.loadTimeline('A');
        const b1 = controller.loadTimeline('B');
        const a2 = controller.loadTimeline('A');
        settle('/api/incidents/A/timeline', timelineResponse([{{ id: 3, event_id: null, transition: 'a-old', title: 'A old' }}]));
        settle('/api/incidents/B/timeline', timelineResponse([{{ id: 4, event_id: null, transition: 'b', title: 'B' }}]));
        settle('/api/incidents/A/timeline', timelineResponse([{{ id: 5, event_id: null, transition: 'a-new', title: 'A new' }}]));
        await Promise.all([a1, b1, a2]);
        assert.equal(elements.get('incidentTimeline').innerHTML.includes('A new'), true);
        assert.equal(elements.get('incidentTimeline').innerHTML.includes('A old'), false);
        assert.equal(elements.get('incidentTimeline').innerHTML.includes('B'), false);
        console.log('EVENT_DETAIL_CROSS_INCIDENT_ISOLATION=PASS');
        console.log('EVENT_DETAIL_SAME_INCIDENT_RACE=PASS');
        console.log('EVENT_DETAIL_FAILURE_STATE=PASS');
        console.log('TIMELINE_GENERATION_RELOAD=PASS');
        """
    )
    script_path = tmp_path / "mc69_event_detail_identity.mjs"
    script_path.write_text(script)
    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EVENT_DETAIL_CROSS_INCIDENT_ISOLATION=PASS" in result.stdout
    assert "EVENT_DETAIL_SAME_INCIDENT_RACE=PASS" in result.stdout
    assert "TIMELINE_GENERATION_RELOAD=PASS" in result.stdout
