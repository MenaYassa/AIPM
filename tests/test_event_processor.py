from datetime import datetime, timedelta, timezone

from aipm.models.history import ContainerHistoryPoint, SampleRunRecord
from aipm.repositories.events.sqlite import SQLiteEventRepository
from aipm.repositories.incidents.sqlite import SQLiteIncidentRepository
from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository
from aipm.services.events.derivation import EventDerivationService
from aipm.services.events.frame import HistoricalFrameService
from aipm.services.events.processor import EventProcessor
from aipm.services.incidents.engine import IncidentEngine

UTC = timezone.utc
FIRST = datetime(2026, 8, 16, tzinfo=UTC)


class EmptyHealthEvidence:
    def observe(self, source_run_id, sampled_at, project_points):
        return ()

    def transition_events(self, current, previous_lookup):
        return ()


def container(at, state):
    return ContainerHistoryPoint(at, "cid", "app", "app:latest", state, "healthy", "stack", 0, 1.0, 2.0, 10.0, 20.0, True)


def test_processor_derives_and_persists_once(tmp_path):
    database = tmp_path / "telemetry.db"
    history = SQLiteHistoryRepository(database)
    history.save_sample(SampleRunRecord(FIRST, True, True, True, "healthy"), None, [container(FIRST, "running")], [], None)
    second = FIRST + timedelta(seconds=15)
    history.save_sample(SampleRunRecord(second, True, True, True, "healthy"), None, [container(second, "restarting")], [], None)
    events = SQLiteEventRepository(database)
    incidents = SQLiteIncidentRepository(database)
    processor = EventProcessor(
        HistoricalFrameService(history),
        events,
        EventDerivationService(),
        EmptyHealthEvidence(),
        IncidentEngine(incidents),
        clock=lambda: second,
    )
    first_result = processor.process_run(2)
    second_result = processor.process_run(2)
    assert first_result.processed is True
    assert first_result.event_count == 1
    assert second_result.processed is False
    assert len(events.get_events(__import__("aipm.models.events", fromlist=["EventFilter"]).EventFilter())) == 1
    assert incidents.get_open_by_correlation("container:cid:stability") is not None


def test_sparse_resource_only_refresh_is_not_an_event_source(tmp_path):
    database = tmp_path / "resource-only.db"
    history = SQLiteHistoryRepository(database)
    history.save_resource_sample(FIRST, [container(FIRST, "running")], duration_ms=3, status="healthy")
    assert history.get_run(1) is None

    events = SQLiteEventRepository(database)
    incidents = SQLiteIncidentRepository(database)
    processor = EventProcessor(
        HistoricalFrameService(history),
        events,
        EventDerivationService(),
        EmptyHealthEvidence(),
        IncidentEngine(incidents),
        clock=lambda: FIRST,
    )
    result = processor.process_run(1)

    assert result.processed is False
    assert result.error == "Telemetry run not found"
    assert events.get_events(__import__("aipm.models.events", fromlist=["EventFilter"]).EventFilter()) == []
    assert incidents.get_open_by_correlation("container:cid:stability") is None
