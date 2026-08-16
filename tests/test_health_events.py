from datetime import datetime, timezone

from aipm.models.finding import Severity
from aipm.models.health import HealthState
from aipm.models.health_observation import HealthFindingRecord, HealthObservation
from aipm.models.events import EventType
from aipm.services.events.health import HealthEvidenceService

UTC = timezone.utc
NOW = datetime(2026, 8, 16, tzinfo=UTC)


def observation(run_id, state, finding_code=None):
    findings = () if finding_code is None else (HealthFindingRecord(f"fp-{finding_code}", finding_code, "DockerAnalyzer", Severity.WARNING, finding_code, "evidence", "app"),)
    return HealthObservation(None, run_id, NOW, "/srv/app", "app", state, 70, findings)


def test_health_state_transition_emits_event():
    service = HealthEvidenceService.__new__(HealthEvidenceService)
    current = (observation(2, HealthState.DEGRADED),)
    previous = observation(1, HealthState.HEALTHY)
    events = service.transition_events(current, lambda _path: previous)
    assert any(item.event_type is EventType.HEALTH_STATE_CHANGED for item in events)
    assert any(item.severity is Severity.WARNING for item in events)


def test_health_finding_change_emits_event():
    service = HealthEvidenceService.__new__(HealthEvidenceService)
    current = (observation(2, HealthState.HEALTHY, "CONTAINER_UNHEALTHY"),)
    previous = observation(1, HealthState.HEALTHY)
    events = service.transition_events(current, lambda _path: previous)
    assert [item.event_type for item in events] == [EventType.HEALTH_FINDING_CHANGED]
