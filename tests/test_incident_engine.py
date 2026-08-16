from datetime import datetime, timezone

from aipm.models.events import Event, EventSource, EventType, ResourceRef, ResourceType
from aipm.models.finding import Severity
from aipm.models.history import SampleRunRecord
from aipm.repositories.events.sqlite import SQLiteEventRepository
from aipm.repositories.incidents.sqlite import SQLiteIncidentRepository
from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository
from aipm.services.incidents.engine import IncidentEngine

UTC = timezone.utc
NOW = datetime(2026, 8, 16, tzinfo=UTC)


def make_event(run_id: int, event_type: EventType, key: str, current: str) -> Event:
    return Event(None, key, NOW, event_type, Severity.HIGH, EventSource.DERIVED, ResourceRef(ResourceType.CONTAINER, "cid", "app", "/srv/app"), event_type.value, "deterministic evidence", "running", current, run_id, run_id - 1, "container:cid:stability")


def setup(tmp_path):
    history = SQLiteHistoryRepository(tmp_path / "telemetry.db")
    history.save_sample(SampleRunRecord(NOW, True, True, True, "healthy"), None, [], [], None)
    events = SQLiteEventRepository(tmp_path / "telemetry.db")
    incidents = SQLiteIncidentRepository(tmp_path / "telemetry.db")
    return events, incidents


def persist(events, item):
    assert events.save_processed_run(1, NOW, [], [item])
    return events.get_event_by_key(item.event_key)


def test_incident_correlation_and_resolution(tmp_path):
    event_repository, incident_repository = setup(tmp_path)
    opening = persist(event_repository, make_event(1, EventType.CONTAINER_RESTARTING, "open", "restarting"))
    engine = IncidentEngine(incident_repository)
    created = engine.apply((opening,))
    assert created == 1
    incident = incident_repository.get_open_by_correlation("container:cid:stability")
    assert incident is not None
    assert incident.status.value == "open"

    recovery = Event(opening.id, "recovery", NOW, EventType.CONTAINER_RECOVERED, Severity.INFO, EventSource.DERIVED, opening.resource, "Container recovered", "back to running", "restarting", "running", 1, 1, opening.correlation_key)
    event_repository2 = SQLiteEventRepository(tmp_path / "telemetry.db")
    event_repository2.save_processed_run(2, NOW, [], []) if False else None
    # The recovery uses the persisted opening's source run and event ID only for this repository-level engine test.
    recovery = Event(opening.id, "recovery", NOW, EventType.CONTAINER_RECOVERED, Severity.INFO, EventSource.DERIVED, opening.resource, "Container recovered", "back to running", "restarting", "running", 1, 1, opening.correlation_key)
    assert engine.apply((recovery,)) == 1
    assert incident_repository.get_open_by_correlation("container:cid:stability") is None
    resolved = incident_repository.get_incident(incident.id)
    assert resolved.status.value == "resolved"


def test_acknowledgement_is_not_remediation(tmp_path):
    event_repository, incident_repository = setup(tmp_path)
    opening = persist(event_repository, make_event(1, EventType.CONTAINER_STOPPED, "stop", "exited"))
    engine = IncidentEngine(incident_repository)
    engine.apply((opening,))
    incident = incident_repository.get_open_by_correlation("container:cid:stability")
    acknowledged = engine.acknowledge(incident.id, NOW)
    assert acknowledged.status.value == "acknowledged"
    assert acknowledged.resolved_at is None
