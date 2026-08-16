from datetime import datetime, timezone

from aipm.models.events import Event, EventFilter, EventSource, EventType, ResourceRef, ResourceType
from aipm.models.finding import Severity
from aipm.models.history import SampleRunRecord
from aipm.repositories.events.sqlite import SQLiteEventRepository
from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository

UTC = timezone.utc
NOW = datetime(2026, 8, 16, tzinfo=UTC)


def event(run_id: int, key: str = "event-key") -> Event:
    return Event(
        id=None,
        event_key=key,
        occurred_at=NOW,
        event_type=EventType.CONTAINER_RESTARTING,
        severity=Severity.HIGH,
        source=EventSource.DERIVED,
        resource=ResourceRef(ResourceType.CONTAINER, "cid", "app", "/srv/app"),
        title="Container entered restarting",
        description="The container state changed.",
        previous_value="running",
        current_value="restarting",
        source_run_id=run_id,
        previous_run_id=run_id - 1,
        correlation_key="container:cid:stability",
    )


def test_event_schema_persists_and_deduplicates_source_run(tmp_path):
    history = SQLiteHistoryRepository(tmp_path / "telemetry.db")
    history.save_sample(SampleRunRecord(NOW, True, True, True, "healthy"), None, [], [], None)
    repository = SQLiteEventRepository(tmp_path / "telemetry.db")
    assert repository.save_processed_run(1, NOW, [], [event(1)]) is True
    assert repository.save_processed_run(1, NOW, [], [event(1)]) is False
    rows = repository.get_events(EventFilter(severity=Severity.HIGH, event_type=EventType.CONTAINER_RESTARTING))
    assert len(rows) == 1
    assert rows[0].event_key == "event-key"
    assert repository.get_event_by_key("event-key").id == rows[0].id
    assert repository.is_processed(1)


def test_event_repository_rejects_missing_source_run(tmp_path):
    repository = SQLiteEventRepository(tmp_path / "telemetry.db")
    try:
        repository.save_processed_run(99, NOW, [], [event(99)])
    except Exception as exc:
        assert "sample_runs" in str(exc) or "FOREIGN KEY" in str(exc).upper()
    else:
        raise AssertionError("missing source run was accepted")
