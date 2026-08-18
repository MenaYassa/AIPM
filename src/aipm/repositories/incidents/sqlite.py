from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from aipm.core.exceptions import AIPMError
from aipm.models.events import Event, EventFilter
from aipm.models.finding import Severity
from aipm.models.incidents import Incident, IncidentFilter, IncidentStatus
from aipm.repositories.events.sqlite import SQLiteEventRepository
from aipm.repositories.readonly import require_read_only_filesystem

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    resolved_at INTEGER,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    resource_name TEXT,
    project_path TEXT,
    correlation_key TEXT NOT NULL,
    summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incident_events (
    incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    attached_at INTEGER NOT NULL,
    PRIMARY KEY (incident_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_incidents_status_updated ON incidents(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_incidents_severity_updated ON incidents(severity, updated_at);
CREATE INDEX IF NOT EXISTS idx_incidents_resource_updated ON incidents(resource_type, resource_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_incidents_correlation_status ON incidents(correlation_key, status);
CREATE INDEX IF NOT EXISTS idx_incident_events_event ON incident_events(event_id);
CREATE TABLE IF NOT EXISTS incident_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    incident_key TEXT NOT NULL,
    transition TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    previous_status TEXT,
    current_status TEXT NOT NULL,
    previous_severity TEXT,
    current_severity TEXT NOT NULL,
    event_id INTEGER,
    source_event_key TEXT,
    correlation_key TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    resource_name TEXT,
    project_path TEXT,
    event_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_incident_transitions_incident ON incident_transitions(incident_id, occurred_at);
"""


class SQLiteIncidentRepository:
    def __init__(self, database_path: str | Path, *, read_only: bool = False):
        self.database_path = Path(database_path).expanduser()
        self.read_only = read_only
        if self.read_only:
            require_read_only_filesystem(self.database_path)
        self.event_repository = SQLiteEventRepository(self.database_path, read_only=read_only)
        if not self.read_only:
            self.initialize()

    def _validate_read_only_path(self) -> None:
        if not self.database_path.is_file():
            raise FileNotFoundError(f"Read-only incident database does not exist: {self.database_path}")

    def _assert_writable(self) -> None:
        if self.read_only:
            raise AIPMError("Incident repository is read-only")

    def initialize(self) -> None:
        self._assert_writable()
        with self._connection() as connection:
            connection.executescript(SCHEMA)

    def get_open_by_correlation(self, correlation_key: str) -> Incident | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE correlation_key = ? AND status IN (?, ?) ORDER BY id DESC LIMIT 1",
                (correlation_key, IncidentStatus.OPEN.value, IncidentStatus.ACKNOWLEDGED.value),
            ).fetchone()
        return self._incident(row) if row is not None else None

    def apply_event(self, event: Event, *, opens_incident: bool, resolves_incident: bool) -> Incident | None:
        self._assert_writable()
        if event.id is None:
            raise ValueError("Incident persistence requires a persisted event id.")
        now = _utc(event.occurred_at)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE correlation_key = ? AND status IN (?, ?) ORDER BY id DESC LIMIT 1",
                (event.correlation_key, IncidentStatus.OPEN.value, IncidentStatus.ACKNOWLEDGED.value),
            ).fetchone()
            transition = ""
            previous_status = row["status"] if row is not None else None
            previous_severity = row["severity"] if row is not None else None
            if resolves_incident:
                if row is None:
                    return None
                connection.execute(
                    "UPDATE incidents SET status = ?, updated_at = ?, resolved_at = ? WHERE id = ?",
                    (IncidentStatus.RESOLVED.value, _timestamp(now), _timestamp(now), row["id"]),
                )
                incident_id = int(row["id"])
                transition = "incident_recovered"
            elif opens_incident:
                if row is None:
                    cursor = connection.execute(
                        """INSERT INTO incidents
                        (incident_key, title, severity, status, started_at, updated_at, resolved_at,
                         resource_type, resource_id, resource_name, project_path, correlation_key, summary)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (f"incident:{event.correlation_key}", event.title, event.severity.value, IncidentStatus.OPEN.value,
                         _timestamp(now), _timestamp(now), None, event.resource.resource_type.value,
                         event.resource.identifier, event.resource.name, event.resource.project_path,
                         event.correlation_key, event.description),
                    )
                    incident_id = int(cursor.lastrowid)
                    transition = "incident_opened"
                else:
                    incident_id = int(row["id"])
                    new_severity = _max_severity(row["severity"], event.severity.value)
                    transition = "incident_escalated" if new_severity != row["severity"] else "incident_updated"
                    connection.execute(
                        "UPDATE incidents SET severity = ?, status = ?, updated_at = ?, title = ?, summary = ? WHERE id = ?",
                        (new_severity, IncidentStatus.OPEN.value,
                         _timestamp(now), event.title, event.description, incident_id),
                    )
            else:
                return self._incident(row) if row is not None else None
            connection.execute(
                "INSERT OR IGNORE INTO incident_events (incident_id, event_id, attached_at) VALUES (?, ?, ?)",
                (incident_id, event.id, _timestamp(now)),
            )
            current = connection.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            connection.execute(
                """INSERT INTO incident_transitions
                (incident_id, incident_key, transition, occurred_at, previous_status, current_status,
                 previous_severity, current_severity, event_id, source_event_key, correlation_key,
                 resource_type, resource_id, resource_name, project_path, event_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (incident_id, current["incident_key"], transition, _timestamp(now), previous_status,
                 current["status"], previous_severity, current["severity"], event.id, event.event_key,
                 current["correlation_key"], current["resource_type"], current["resource_id"],
                 current["resource_name"], current["project_path"], event.event_type.value),
            )
        return self.get_incident(incident_id)

    def acknowledge(self, incident_id: int, acknowledged_at: datetime) -> Incident | None:
        self._assert_writable()
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM incidents WHERE id = ? AND status = ?", (incident_id, IncidentStatus.OPEN.value)).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE incidents SET status = ?, updated_at = ? WHERE id = ?",
                    (IncidentStatus.ACKNOWLEDGED.value, _timestamp(acknowledged_at), incident_id),
                )
                connection.execute(
                    """INSERT INTO incident_transitions
                    (incident_id, incident_key, transition, occurred_at, previous_status, current_status,
                     previous_severity, current_severity, correlation_key, resource_type, resource_id, resource_name, project_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (incident_id, row["incident_key"], "incident_acknowledged", _timestamp(acknowledged_at),
                     row["status"], IncidentStatus.ACKNOWLEDGED.value, row["severity"], row["severity"],
                     row["correlation_key"], row["resource_type"], row["resource_id"], row["resource_name"], row["project_path"]),
                )
        return self.get_incident(incident_id)

    def get_incidents(self, incident_filter: IncidentFilter) -> list[Incident]:
        conditions: list[str] = []
        values: list[object] = []
        if incident_filter.status:
            conditions.append("status = ?")
            values.append(incident_filter.status.value)
        if incident_filter.severity:
            conditions.append("severity = ?")
            values.append(incident_filter.severity.value)
        if incident_filter.resource_id:
            conditions.append("resource_id = ?")
            values.append(incident_filter.resource_id)
        if incident_filter.start:
            conditions.append("started_at >= ?")
            values.append(_timestamp(incident_filter.start))
        if incident_filter.end:
            conditions.append("started_at <= ?")
            values.append(_timestamp(incident_filter.end))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(incident_filter.limit)
        with self._connection() as connection:
            rows = connection.execute(f"SELECT * FROM incidents{where} ORDER BY updated_at DESC, id DESC LIMIT ?", values).fetchall()
        return [self._incident(row) for row in rows]

    def get_incident(self, incident_id: int) -> Incident | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return self._incident(row) if row is not None else None

    def delete_old_resolved(self, cutoff: datetime) -> int:
        self._assert_writable()
        cutoff_ts = _timestamp(cutoff)
        with self._connection() as connection:
            ids = [row["id"] for row in connection.execute(
                "SELECT id FROM incidents WHERE status = ? AND updated_at < ?",
                (IncidentStatus.RESOLVED.value, cutoff_ts),
            ).fetchall()]
            for incident_id in ids:
                connection.execute("DELETE FROM incident_events WHERE incident_id = ?", (incident_id,))
            cursor = connection.execute(
                "DELETE FROM incidents WHERE status = ? AND updated_at < ?",
                (IncidentStatus.RESOLVED.value, _timestamp(cutoff)),
            )
            return max(0, cursor.rowcount)

    def _incident(self, row: sqlite3.Row) -> Incident:
        with self._connection() as connection:
            event_ids = [item["event_id"] for item in connection.execute(
                "SELECT event_id FROM incident_events WHERE incident_id = ? ORDER BY attached_at ASC",
                (row["id"],),
            ).fetchall()]
        events = tuple(event for event_id in event_ids if (event := self.event_repository.get_event(event_id)) is not None)
        from aipm.models.events import ResourceRef, ResourceType
        return Incident(
            id=int(row["id"]),
            incident_key=row["incident_key"],
            title=row["title"],
            severity=Severity(row["severity"]),
            status=IncidentStatus(row["status"]),
            started_at=_datetime(row["started_at"]),
            updated_at=_datetime(row["updated_at"]),
            resolved_at=_datetime(row["resolved_at"]) if row["resolved_at"] is not None else None,
            resource=ResourceRef(ResourceType(row["resource_type"]), row["resource_id"], row["resource_name"], row["project_path"]),
            correlation_key=row["correlation_key"],
            summary=row["summary"],
            events=events,
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
                connection.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError:
                pass
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _max_severity(current: str, incoming: str) -> str:
    rank = {Severity.INFO.value: 1, Severity.WARNING.value: 2, Severity.HIGH.value: 3, Severity.CRITICAL.value: 4}
    return incoming if rank[incoming] > rank[current] else current


def _timestamp(value: datetime) -> int:
    return int(_utc(value).timestamp())


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Incident timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)
