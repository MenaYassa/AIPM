from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from aipm.models.events import EventType, ResourceRef, ResourceType
from aipm.models.finding import Severity
from aipm.models.incidents import IncidentStatus
from aipm.models.notifications import (
    DeliveryStatus,
    IncidentTransition,
    Notification,
    NotificationFilter,
    NotificationStatus,
    NotificationTrigger,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS incident_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_incident_transitions_projected ON incident_transitions(id);
CREATE TABLE IF NOT EXISTS notification_projection_runs (
    transition_id INTEGER PRIMARY KEY,
    projected_at INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT NOT NULL UNIQUE,
    transition_id INTEGER NOT NULL,
    incident_id INTEGER NOT NULL,
    event_id INTEGER,
    policy_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    resource_name TEXT,
    project_path TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    next_attempt_at INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    suppressed_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_status_due ON notifications(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_notifications_incident_created ON notifications(incident_id, created_at);
CREATE TABLE IF NOT EXISTS notification_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL,
    status TEXT NOT NULL,
    provider_request_key TEXT NOT NULL UNIQUE,
    provider_message_id TEXT,
    created_at INTEGER NOT NULL,
    last_attempt_at INTEGER,
    next_attempt_at INTEGER,
    lease_until INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notification_deliveries_due ON notification_deliveries(status, next_attempt_at, lease_until);
CREATE TABLE IF NOT EXISTS notification_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL REFERENCES notification_deliveries(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    outcome TEXT NOT NULL,
    retryable INTEGER NOT NULL,
    provider_status_code INTEGER,
    error_code TEXT,
    error_message TEXT,
    UNIQUE(delivery_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS notification_dedup (
    scope_key TEXT PRIMARY KEY,
    last_notified_at INTEGER,
    window_started_at INTEGER,
    window_count INTEGER NOT NULL DEFAULT 0,
    suppressed_count INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
"""


class SQLiteNotificationRepository:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).expanduser()
        self.initialize()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(SCHEMA)

    def add_transition(self, transition: IncidentTransition) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """INSERT INTO incident_transitions
                (incident_id, incident_key, transition, occurred_at, previous_status, current_status,
                 previous_severity, current_severity, event_id, source_event_key, correlation_key,
                 resource_type, resource_id, resource_name, project_path, event_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (transition.incident_id, transition.incident_key, transition.transition.value,
                 _timestamp(transition.occurred_at), _enum_value(transition.previous_status),
                 transition.current_status.value, _enum_value(transition.previous_severity),
                 transition.current_severity.value, transition.event_id, transition.source_event_key,
                 transition.correlation_key, transition.resource.resource_type.value,
                 transition.resource.identifier, transition.resource.name, transition.resource.project_path,
                 transition.event_type.value if transition.event_type else None),
            )
            return int(cursor.lastrowid)

    def get_unprojected_transitions(self, limit: int = 100) -> list[IncidentTransition]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT t.* FROM incident_transitions t
                LEFT JOIN notification_projection_runs p ON p.transition_id = t.id
                WHERE p.transition_id IS NULL ORDER BY t.id LIMIT ?""", (limit,)
            ).fetchall()
        return [self._transition(row) for row in rows]

    def mark_projected(self, transition_id: int, status: str = "projected") -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO notification_projection_runs (transition_id, projected_at, status) VALUES (?, ?, ?)",
                (transition_id, _timestamp(datetime.now(timezone.utc)), status),
            )

    def create_notification(self, *, identity_key: str, transition: IncidentTransition, policy_id: str, channel_id: str, status: NotificationStatus, title: str, body: str, suppressed_reason: str | None = None) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO notifications
                (identity_key, transition_id, incident_id, event_id, policy_id, channel_id, trigger,
                 status, severity, resource_type, resource_id, resource_name, project_path, title, body, created_at, suppressed_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (identity_key, transition.id, transition.incident_id, transition.event_id, policy_id, channel_id,
                 transition.transition.value, status.value, transition.current_severity.value,
                 transition.resource.resource_type.value, transition.resource.identifier, transition.resource.name,
                 transition.resource.project_path, title, body, _timestamp(datetime.now(timezone.utc)), suppressed_reason),
            )
            return int(cursor.lastrowid or connection.execute("SELECT id FROM notifications WHERE identity_key = ?", (identity_key,)).fetchone()[0])

    def create_delivery(self, notification_id: int, channel_id: str, provider_request_key: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO notification_deliveries (notification_id, channel_id, status, provider_request_key, created_at) VALUES (?, ?, ?, ?, ?)",
                (notification_id, channel_id, DeliveryStatus.PENDING.value, provider_request_key, _timestamp(datetime.now(timezone.utc))),
            )
            return int(cursor.lastrowid or connection.execute("SELECT id FROM notification_deliveries WHERE provider_request_key = ?", (provider_request_key,)).fetchone()[0])

    def claim_due(self, now: datetime, lease_seconds: int = 60) -> tuple[int, Notification] | None:
        now_ts = _timestamp(now)
        with self._connection() as connection:
            row = connection.execute(
                """SELECT d.id AS delivery_id, n.* FROM notification_deliveries d
                JOIN notifications n ON n.id = d.notification_id
                WHERE (d.status = ? OR (d.status = ? AND d.lease_until < ?))
                  AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= ?)
                  AND n.status IN (?, ?)
                ORDER BY COALESCE(d.next_attempt_at, n.created_at), d.id LIMIT 1""",
                (DeliveryStatus.PENDING.value, DeliveryStatus.SENDING.value, now_ts, now_ts,
                 NotificationStatus.PENDING.value, NotificationStatus.UNKNOWN.value),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE notification_deliveries SET status = ?, lease_until = ?, last_attempt_at = ?, attempt_count = attempt_count + 1 WHERE id = ?",
                (DeliveryStatus.SENDING.value, now_ts + lease_seconds, now_ts, row["delivery_id"]),
            )
            connection.execute("UPDATE notifications SET status = ?, attempt_count = attempt_count + 1 WHERE id = ?", (NotificationStatus.SENDING.value, row["id"]))
        return int(row["delivery_id"]), self._notification(row)

    def finish_delivery(self, delivery_id: int, result: DeliveryStatus, *, retryable: bool, provider_message_id: str | None = None, provider_status_code: int | None = None, error_code: str | None = None, error_message: str | None = None, next_attempt_at: datetime | None = None) -> None:
        now = datetime.now(timezone.utc)
        with self._connection() as connection:
            row = connection.execute("SELECT notification_id, attempt_count FROM notification_deliveries WHERE id = ?", (delivery_id,)).fetchone()
            if row is None:
                return
            notification_status = NotificationStatus.SENT if result is DeliveryStatus.SENT else NotificationStatus.UNKNOWN if result is DeliveryStatus.UNKNOWN else NotificationStatus.PENDING if retryable else NotificationStatus.FAILED
            connection.execute("UPDATE notification_deliveries SET status = ?, provider_message_id = ?, lease_until = NULL, next_attempt_at = ? WHERE id = ?", (result.value, provider_message_id, _timestamp(next_attempt_at) if next_attempt_at else None, delivery_id))
            connection.execute("UPDATE notifications SET status = ?, next_attempt_at = ? WHERE id = ?", (notification_status.value, _timestamp(next_attempt_at) if next_attempt_at else None, row["notification_id"]))
            connection.execute("INSERT INTO notification_attempts (delivery_id, attempt_number, started_at, finished_at, outcome, retryable, provider_status_code, error_code, error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (delivery_id, row["attempt_count"], _timestamp(now), _timestamp(now), result.value, int(retryable), provider_status_code, error_code, _safe_error(error_message)))

    def get_notifications(self, notification_filter: NotificationFilter) -> list[Notification]:
        conditions, values = [], []
        if notification_filter.status:
            conditions.append("n.status = ?"); values.append(notification_filter.status.value)
        if notification_filter.incident_id is not None:
            conditions.append("n.incident_id = ?"); values.append(notification_filter.incident_id)
        if notification_filter.channel_id:
            conditions.append("n.channel_id = ?"); values.append(notification_filter.channel_id)
        if not notification_filter.include_suppressed:
            conditions.append("n.status != ?"); values.append(NotificationStatus.SUPPRESSED.value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        values.append(max(1, min(notification_filter.limit, 500)))
        with self._connection() as connection:
            rows = connection.execute(f"SELECT n.* FROM notifications n{where} ORDER BY n.created_at DESC, n.id DESC LIMIT ?", values).fetchall()
        return [self._notification(row) for row in rows]

    def get_notification(self, notification_id: int) -> Notification | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone()
        return self._notification(row) if row else None

    def _transition(self, row: sqlite3.Row) -> IncidentTransition:
        return IncidentTransition(int(row["id"]), int(row["incident_id"]), row["incident_key"], NotificationTrigger(row["transition"]), _datetime(row["occurred_at"]), IncidentStatus(row["previous_status"]) if row["previous_status"] else None, IncidentStatus(row["current_status"]), Severity(row["previous_severity"]) if row["previous_severity"] else None, Severity(row["current_severity"]), row["event_id"], row["source_event_key"], row["correlation_key"], ResourceRef(ResourceType(row["resource_type"]), row["resource_id"], row["resource_name"], row["project_path"]), EventType(row["event_type"]) if row["event_type"] else None)

    def _notification(self, row: sqlite3.Row) -> Notification:
        return Notification(int(row["id"]), row["identity_key"], int(row["incident_id"]), row["event_id"], int(row["transition_id"]), row["policy_id"], row["channel_id"], NotificationTrigger(row["trigger"]), NotificationStatus(row["status"]), Severity(row["severity"]), ResourceRef(ResourceType(row["resource_type"]), row["resource_id"], row["resource_name"], row["project_path"]), row["title"], row["body"], _datetime(row["created_at"]), _datetime(row["next_attempt_at"]) if row["next_attempt_at"] else None, int(row["attempt_count"]), row["suppressed_reason"])

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback(); raise
        finally:
            connection.close()


def identity_key(policy_id: str, channel_id: str, transition: IncidentTransition) -> str:
    raw = f"{policy_id}|{channel_id}|{transition.incident_id}|{transition.transition.value}|{transition.id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def provider_key(notification_id: int, channel_id: str) -> str:
    return hashlib.sha256(f"notification|{notification_id}|{channel_id}".encode()).hexdigest()


def _enum_value(value: object | None) -> str | None:
    return getattr(value, "value", value) if value is not None else None


def _safe_error(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace("\n", " ")[:500]


def _timestamp(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("Notification timestamps must be timezone-aware.")
    return int(value.astimezone(timezone.utc).timestamp())


def _datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value, timezone.utc)
