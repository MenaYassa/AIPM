from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aipm.models.events import Event, EventSource, EventType, ResourceRef, ResourceType
from aipm.models.finding import Severity
from aipm.models.history import SampleRunRecord
from aipm.repositories.events.sqlite import SQLiteEventRepository
from aipm.repositories.incidents.sqlite import SQLiteIncidentRepository
from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository
from aipm.services.incidents.engine import IncidentEngine


UTC = timezone.utc


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="aipm-mc3-measure-") as directory:
        database = Path(directory) / "mission_control.db"
        history = SQLiteHistoryRepository(database)
        events = SQLiteEventRepository(database)
        incidents = SQLiteIncidentRepository(database)
        before = database.stat().st_size
        started = datetime(2026, 8, 16, tzinfo=UTC)
        engine = IncidentEngine(incidents)
        for index in range(120):
            sampled_at = started + timedelta(seconds=index * 15)
            history.save_sample(SampleRunRecord(sampled_at, True, True, True, "healthy"), None, [], [], None)
            item = Event(
                id=None,
                event_key=f"measurement-{index}",
                occurred_at=sampled_at,
                event_type=EventType.CONTAINER_RESTARTING,
                severity=Severity.HIGH,
                source=EventSource.DERIVED,
                resource=ResourceRef(ResourceType.CONTAINER, "measure-container", "measure", "/tmp/measure"),
                title="Container entered restarting",
                description="Measured representative event row.",
                previous_value="running",
                current_value="restarting",
                source_run_id=index + 1,
                previous_run_id=index,
                correlation_key="container:measure-container:stability",
            )
            events.save_processed_run(index + 1, sampled_at, [], [item])
            persisted = events.get_event_by_key(item.event_key)
            engine.apply((persisted,))
        after = database.stat().st_size
        print(f"database_before_bytes={before}")
        print(f"database_after_bytes={after}")
        print(f"growth_bytes={after - before}")
        print("sample_runs=120")
        print("events=120")
        print("open_incidents=1")
        print(f"bytes_per_event_cycle={(after - before) / 120:.1f}")
        print(f"projected_24h_bytes={(after - before) / 120 * 5760:.0f}")
        print(f"projected_30d_bytes={(after - before) / 120 * 5760 * 30:.0f}")


if __name__ == "__main__":
    main()
