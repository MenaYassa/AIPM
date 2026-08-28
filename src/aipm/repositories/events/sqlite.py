from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from aipm.core.exceptions import AIPMError
from aipm.models.events import Event, EventFilter, EventSource, EventType, FindingEvidence, ResourceRef, ResourceType
from aipm.models.finding import Severity
from aipm.models.health import HealthState
from aipm.models.health_observation import HealthFindingRecord, HealthObservation
from aipm.repositories.readonly import require_read_only_filesystem


SCHEMA = """
CREATE TABLE IF NOT EXISTS event_processing_runs (
    source_run_id INTEGER PRIMARY KEY REFERENCES sample_runs(id) ON DELETE CASCADE,
    processed_at INTEGER NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_run_id INTEGER NOT NULL REFERENCES sample_runs(id) ON DELETE CASCADE,
    sampled_at INTEGER NOT NULL,
    project_path TEXT NOT NULL,
    project_name TEXT NOT NULL,
    report_state TEXT NOT NULL,
    score INTEGER NOT NULL,
    UNIQUE(source_run_id, project_path)
);

CREATE TABLE IF NOT EXISTS health_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES health_observations(id) ON DELETE CASCADE,
    finding_fingerprint TEXT NOT NULL,
    code TEXT NOT NULL,
    component TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    resource TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    occurred_at INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    source TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    resource_name TEXT,
    project_path TEXT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    previous_value TEXT,
    current_value TEXT,
    source_run_id INTEGER NOT NULL REFERENCES sample_runs(id),
    previous_run_id INTEGER,
    correlation_key TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS event_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    finding_code TEXT NOT NULL,
    component TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    resource TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_resource_time ON events(resource_type, resource_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_correlation_time ON events(correlation_key, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_source_run ON events(source_run_id);
CREATE INDEX IF NOT EXISTS idx_events_type_time ON events(event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_event_evidence_event ON event_evidence(event_id);
CREATE INDEX IF NOT EXISTS idx_health_observations_project_time ON health_observations(project_path, sampled_at);
"""


class SQLiteEventRepository:
    def __init__(self, database_path: str | Path, *, read_only: bool = False):
        self.database_path = Path(database_path).expanduser()
        self.read_only = read_only
        if self.read_only:
            require_read_only_filesystem(self.database_path)
        else:
            self.initialize()

    def _validate_read_only_path(self) -> None:
        if not self.database_path.is_file():
            raise FileNotFoundError(f"Read-only event database does not exist: {self.database_path}")

    def _assert_writable(self) -> None:
        if self.read_only:
            raise AIPMError("Event repository is read-only")

    def initialize(self) -> None:
        self._assert_writable()
        with self._connection() as connection:
            connection.executescript(SCHEMA)

    def save_processed_run(
        self,
        source_run_id: int,
        processed_at: datetime,
        observations: Sequence[HealthObservation],
        events: Sequence[Event],
    ) -> bool:
        self._assert_writable()
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO event_processing_runs (source_run_id, processed_at, status) VALUES (?, ?, ?)",
                (source_run_id, _timestamp(processed_at), "processed"),
            )
            if cursor.rowcount != 1:
                return False
            for observation in observations:
                observation_cursor = connection.execute(
                    """
                    INSERT INTO health_observations
                        (source_run_id, sampled_at, project_path, project_name, report_state, score)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation.source_run_id,
                        _timestamp(observation.sampled_at),
                        observation.project_path,
                        observation.project_name,
                        observation.report_state.value,
                        observation.score,
                    ),
                )
                observation_id = int(observation_cursor.lastrowid)
                connection.executemany(
                    """
                    INSERT INTO health_findings
                        (observation_id, finding_fingerprint, code, component, severity, title, description, resource)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            observation_id,
                            finding.fingerprint,
                            finding.code,
                            finding.component,
                            finding.severity.value,
                            finding.title,
                            finding.description,
                            finding.resource,
                        )
                        for finding in observation.findings
                    ],
                )
            for event in events:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO events (
                        event_key, occurred_at, event_type, severity, source,
                        resource_type, resource_id, resource_name, project_path,
                        title, description, previous_value, current_value,
                        source_run_id, previous_run_id, correlation_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_key,
                        _timestamp(event.occurred_at),
                        event.event_type.value,
                        event.severity.value,
                        event.source.value,
                        event.resource.resource_type.value,
                        event.resource.identifier,
                        event.resource.name,
                        event.resource.project_path,
                        event.title,
                        event.description,
                        event.previous_value,
                        event.current_value,
                        event.source_run_id,
                        event.previous_run_id,
                        event.correlation_key,
                        _timestamp(processed_at),
                    ),
                )
                row = connection.execute("SELECT id FROM events WHERE event_key = ?", (event.event_key,)).fetchone()
                event_id = int(row[0])
                connection.executemany(
                    """
                    INSERT INTO event_evidence
                        (event_id, finding_code, component, severity, title, description, resource)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            event_id,
                            evidence.code,
                            evidence.component,
                            evidence.severity.value,
                            evidence.title,
                            evidence.description,
                            evidence.resource,
                        )
                        for evidence in event.evidence
                    ],
                )
            return True

    def get_previous_health_observation(self, project_path: str, before_run_id: int) -> HealthObservation | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM health_observations WHERE project_path = ? AND source_run_id < ? ORDER BY source_run_id DESC LIMIT 1",
                (project_path, before_run_id),
            ).fetchone()
            return self._observation(connection, row) if row is not None else None

    def get_events(self, event_filter: EventFilter) -> list[Event]:
        conditions: list[str] = []
        values: list[object] = []
        if event_filter.start is not None:
            conditions.append("occurred_at >= ?")
            values.append(_timestamp(event_filter.start))
        if event_filter.end is not None:
            conditions.append("occurred_at <= ?")
            values.append(_timestamp(event_filter.end))
        if event_filter.severity is not None:
            conditions.append("severity = ?")
            values.append(event_filter.severity.value)
        if event_filter.event_type is not None:
            conditions.append("event_type = ?")
            values.append(event_filter.event_type.value)
        if event_filter.resource_type is not None:
            conditions.append("resource_type = ?")
            values.append(event_filter.resource_type.value)
        if event_filter.resource_id is not None:
            conditions.append("resource_id = ?")
            values.append(event_filter.resource_id)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(event_filter.limit)
        with self._connection() as connection:
            rows = connection.execute(f"SELECT * FROM events{where} ORDER BY occurred_at ASC, id ASC LIMIT ?", values).fetchall()
            return [self._event(connection, row) for row in rows]

    def get_events_page(self, event_filter: EventFilter, *, after: tuple[object, int] | None = None) -> list[Event]:
        conditions: list[str] = []
        values: list[object] = []
        if event_filter.start is not None:
            conditions.append("occurred_at >= ?")
            values.append(_timestamp(event_filter.start))
        if event_filter.end is not None:
            conditions.append("occurred_at <= ?")
            values.append(_timestamp(event_filter.end))
        if event_filter.severity is not None:
            conditions.append("severity = ?")
            values.append(event_filter.severity.value)
        if event_filter.event_type is not None:
            conditions.append("event_type = ?")
            values.append(event_filter.event_type.value)
        if event_filter.resource_type is not None:
            conditions.append("resource_type = ?")
            values.append(event_filter.resource_type.value)
        if event_filter.resource_id is not None:
            conditions.append("resource_id = ?")
            values.append(event_filter.resource_id)
        if after is not None:
            after_at, after_id = after
            conditions.append("(occurred_at > ? OR (occurred_at = ? AND id > ?))")
            values.extend((_timestamp(after_at), _timestamp(after_at), after_id))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(event_filter.limit)
        with self._connection() as connection:
            rows = connection.execute(f"SELECT * FROM events{where} ORDER BY occurred_at ASC, id ASC LIMIT ?", values).fetchall()
            return [self._event(connection, row) for row in rows]

    def get_event(self, event_id: int) -> Event | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            return self._event(connection, row) if row is not None else None

    def get_events_by_ids(self, event_ids: tuple[int, ...]) -> list[Event]:
        bounded_ids = tuple(dict.fromkeys(int(event_id) for event_id in event_ids))
        if not bounded_ids or len(bounded_ids) > 500:
            raise ValueError("Event batch size is outside the supported bounds.")
        placeholders = ",".join("?" for _ in bounded_ids)
        with self._connection() as connection:
            rows = connection.execute(f"SELECT * FROM events WHERE id IN ({placeholders}) ORDER BY id ASC", bounded_ids).fetchall()
            evidence_rows = connection.execute(f"SELECT * FROM event_evidence WHERE event_id IN ({placeholders}) ORDER BY event_id ASC, id ASC", bounded_ids).fetchall()
        evidence_by_event: dict[int, list[sqlite3.Row]] = {}
        for evidence in evidence_rows:
            evidence_by_event.setdefault(int(evidence["event_id"]), []).append(evidence)
        return [self._event_from_rows(row, evidence_by_event.get(int(row["id"]), ())) for row in rows]

    def get_event_by_key(self, event_key: str) -> Event | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM events WHERE event_key = ?", (event_key,)).fetchone()
            return self._event(connection, row) if row is not None else None

    def is_processed(self, source_run_id: int) -> bool:
        with self._connection() as connection:
            row = connection.execute("SELECT 1 FROM event_processing_runs WHERE source_run_id = ?", (source_run_id,)).fetchone()
            return row is not None

    def delete_old_events(self, cutoff: datetime) -> int:
        self._assert_writable()
        cutoff_ts = _timestamp(cutoff)
        with self._connection() as connection:
            connection.execute("DELETE FROM event_evidence WHERE event_id IN (SELECT id FROM events WHERE occurred_at < ?)", (cutoff_ts,))
            cursor = connection.execute("DELETE FROM events WHERE occurred_at < ?", (cutoff_ts,))
            return max(0, cursor.rowcount)

    def _event(self, connection: sqlite3.Connection, row: sqlite3.Row) -> Event:
        evidence_rows = connection.execute("SELECT * FROM event_evidence WHERE event_id = ? ORDER BY id ASC", (row["id"],)).fetchall()
        return self._event_from_rows(row, evidence_rows)

    @staticmethod
    def _event_from_rows(row: sqlite3.Row, evidence_rows: Sequence[sqlite3.Row]) -> Event:
        return Event(
            id=int(row["id"]),
            event_key=row["event_key"],
            occurred_at=_datetime(row["occurred_at"]),
            event_type=EventType(row["event_type"]),
            severity=Severity(row["severity"]),
            source=EventSource(row["source"]),
            resource=ResourceRef(
                resource_type=ResourceType(row["resource_type"]),
                identifier=row["resource_id"],
                name=row["resource_name"],
                project_path=row["project_path"],
            ),
            title=row["title"],
            description=row["description"],
            previous_value=row["previous_value"],
            current_value=row["current_value"],
            source_run_id=int(row["source_run_id"]),
            previous_run_id=row["previous_run_id"],
            correlation_key=row["correlation_key"],
            evidence=tuple(
                FindingEvidence(
                    code=item["finding_code"],
                    component=item["component"],
                    severity=Severity(item["severity"]),
                    title=item["title"],
                    description=item["description"],
                    resource=item["resource"],
                )
                for item in evidence_rows
            ),
        )

    def _observation(self, connection: sqlite3.Connection, row: sqlite3.Row) -> HealthObservation:
        finding_rows = connection.execute("SELECT * FROM health_findings WHERE observation_id = ? ORDER BY id ASC", (row["id"],)).fetchall()
        return HealthObservation(
            id=int(row["id"]),
            source_run_id=int(row["source_run_id"]),
            sampled_at=_datetime(row["sampled_at"]),
            project_path=row["project_path"],
            project_name=row["project_name"],
            report_state=HealthState(row["report_state"]),
            score=int(row["score"]),
            findings=tuple(
                HealthFindingRecord(
                    fingerprint=item["finding_fingerprint"],
                    code=item["code"],
                    component=item["component"],
                    severity=Severity(item["severity"]),
                    title=item["title"],
                    description=item["description"],
                    resource=item["resource"],
                )
                for item in finding_rows
            ),
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            connection = sqlite3.connect(
                f"{self.database_path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=5,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA query_only = ON")
                yield connection
            finally:
                connection.close()
            return

        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                connection.execute("PRAGMA journal_mode = DELETE")
            except sqlite3.DatabaseError:
                pass
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _timestamp(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("Event timestamps must be timezone-aware.")
    return int(value.astimezone(timezone.utc).timestamp())


def _datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)
