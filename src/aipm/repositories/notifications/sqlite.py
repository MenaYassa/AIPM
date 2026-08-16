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
from aipm.repositories.incidents.sqlite import SQLiteIncidentRepository

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_schema_meta (
    schema_name TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    migrated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_projection_runs (
    transition_id INTEGER PRIMARY KEY REFERENCES incident_transitions(id) ON DELETE CASCADE,
    projected_at INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT NOT NULL UNIQUE,
    transition_id INTEGER NOT NULL REFERENCES incident_transitions(id) ON DELETE CASCADE,
    incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
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
    suppressed_reason TEXT,
    retry_exhausted_at INTEGER,
    manual_retry_count INTEGER NOT NULL DEFAULT 0
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
    lease_token TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_expiry_count INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS notification_suppressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT NOT NULL,
    transition_id INTEGER REFERENCES incident_transitions(id) ON DELETE CASCADE,
    incident_id INTEGER REFERENCES incidents(id) ON DELETE CASCADE,
    policy_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(identity_key, reason)
);
CREATE INDEX IF NOT EXISTS idx_notification_suppressions_created ON notification_suppressions(created_at);
CREATE TABLE IF NOT EXISTS notification_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    previous_status TEXT,
    new_status TEXT,
    actor TEXT NOT NULL,
    reason TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notification_actions_notification ON notification_actions(notification_id, created_at);
"""


class SQLiteNotificationRepository:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).expanduser()
        self.initialize()

    def initialize(self) -> None:
        SQLiteIncidentRepository(self.database_path).initialize()
        with self._connection() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)

    def _migrate(self, connection: sqlite3.Connection) -> None:
        now = _timestamp(datetime.now(timezone.utc))
        row = connection.execute("SELECT schema_version FROM notification_schema_meta WHERE schema_name = 'notifications'").fetchone()
        version = int(row[0]) if row else 0
        legacy = self._legacy_schema_needs_rebuild(connection)
        if not row:
            connection.execute("INSERT INTO notification_schema_meta (schema_name, schema_version, migrated_at) VALUES ('notifications', ?, ?)", (1 if legacy else SCHEMA_VERSION, now))
            version = 1 if legacy else SCHEMA_VERSION
        if version < 2:
            if legacy:
                self._rebuild_legacy_tables(connection)
            self._add_column(connection, "notifications", "retry_exhausted_at", "INTEGER")
            self._add_column(connection, "notifications", "manual_retry_count", "INTEGER NOT NULL DEFAULT 0")
            self._add_column(connection, "notification_deliveries", "lease_expiry_count", "INTEGER NOT NULL DEFAULT 0")
            self._add_column(connection, "notification_deliveries", "lease_token", "TEXT")
            connection.execute("UPDATE notification_schema_meta SET schema_version = ?, migrated_at = ? WHERE schema_name = 'notifications'", (SCHEMA_VERSION, now))

    @staticmethod
    def _legacy_schema_needs_rebuild(connection: sqlite3.Connection) -> bool:
        required = {"notifications": {"incident_transitions", "incidents", "events"}, "notification_projection_runs": {"incident_transitions"}}
        for table, parents in required.items():
            actual = {row["table"] for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()}
            if not parents.issubset(actual):
                return True
        return False

    @staticmethod
    def _rebuild_legacy_tables(connection: sqlite3.Connection) -> None:
        legacy_names = ("notification_attempts", "notification_deliveries", "notifications", "notification_projection_runs", "notification_dedup")
        connection.execute("PRAGMA foreign_keys = OFF")
        for table in legacy_names:
            connection.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
        connection.executescript(SCHEMA)
        connection.execute("""INSERT INTO notifications
            (id, identity_key, transition_id, incident_id, event_id, policy_id, channel_id, trigger, status, severity,
             resource_type, resource_id, resource_name, project_path, title, body, created_at, next_attempt_at,
             attempt_count, suppressed_reason)
            SELECT id, identity_key, transition_id, incident_id, event_id, policy_id, channel_id, trigger, status, severity,
             resource_type, resource_id, resource_name, project_path, title, body, created_at, next_attempt_at,
             attempt_count, suppressed_reason FROM notifications_legacy""")
        connection.execute("""INSERT INTO notification_deliveries
            (id, notification_id, channel_id, status, provider_request_key, provider_message_id, created_at,
             last_attempt_at, next_attempt_at, lease_until, attempt_count)
            SELECT id, notification_id, channel_id, status, provider_request_key, provider_message_id, created_at,
                                 last_attempt_at, next_attempt_at, lease_until, attempt_count FROM notification_deliveries_legacy""")

        connection.execute("""INSERT INTO notification_attempts
            (id, delivery_id, attempt_number, started_at, finished_at, outcome, retryable, provider_status_code, error_code, error_message)
            SELECT id, delivery_id, attempt_number, started_at, finished_at, outcome, retryable, provider_status_code, error_code, error_message FROM notification_attempts_legacy""")
        connection.execute("INSERT INTO notification_projection_runs (transition_id, projected_at, status) SELECT transition_id, projected_at, status FROM notification_projection_runs_legacy")
        connection.execute("INSERT INTO notification_dedup (scope_key, last_notified_at, window_started_at, window_count, suppressed_count, updated_at) SELECT scope_key, last_notified_at, window_started_at, window_count, suppressed_count, updated_at FROM notification_dedup_legacy")
        for table in legacy_names:
            connection.execute(f"DROP TABLE {table}_legacy")
        connection.execute("PRAGMA foreign_keys = ON")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError("MC-4.5 migration found orphaned notification records")

    @staticmethod
    def _add_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def schema_version(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT schema_version FROM notification_schema_meta WHERE schema_name = 'notifications'").fetchone()
        return int(row[0]) if row else 0

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
                WHERE p.transition_id IS NULL ORDER BY t.id LIMIT ?""", (max(1, min(limit, 500)),)
            ).fetchall()
        return [self._transition(row) for row in rows]

    def mark_projected(self, transition_id: int, status: str = "projected") -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO notification_projection_runs (transition_id, projected_at, status) VALUES (?, ?, ?)",
                (transition_id, _timestamp(datetime.now(timezone.utc)), status),
            )

    def record_suppression(self, *, identity_key_value: str, transition: IncidentTransition, policy_id: str, channel_id: str, reason: str, now: datetime | None = None) -> None:
        now_ts = _timestamp(now or datetime.now(timezone.utc))
        with self._connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO notification_suppressions
                (identity_key, transition_id, incident_id, policy_id, channel_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (identity_key_value, transition.id, transition.incident_id, policy_id, channel_id, reason, now_ts),
            )

    def create_decision(
        self,
        *,
        identity_key_value: str,
        transition: IncidentTransition,
        policy_id: str,
        channel_id: str,
        title: str,
        body: str,
        cooldown_seconds: int,
        window_seconds: int,
        max_notifications: int,
        global_window_seconds: int = 0,
        global_max_notifications: int = 0,
        now: datetime | None = None,
    ) -> tuple[int | None, str | None]:
        now_ts = _timestamp(now or datetime.now(timezone.utc))
        with self._connection() as connection:
            if connection.execute("SELECT 1 FROM notifications WHERE identity_key = ?", (identity_key_value,)).fetchone() is not None:
                self._record_suppression_tx(connection, identity_key_value, transition, policy_id, channel_id, "duplicate_identity", now_ts)
                return None, "duplicate_identity"
            if connection.execute("SELECT 1 FROM notification_suppressions WHERE identity_key = ?", (identity_key_value,)).fetchone() is not None:
                return None, "duplicate_identity"

            incident_scope = "recovery" if transition.transition is NotificationTrigger.INCIDENT_RECOVERED else "incident"
            scopes = [
                (f"{incident_scope}:{policy_id}:{channel_id}:{transition.incident_id}", cooldown_seconds, None),
                (f"channel:{channel_id}", window_seconds, max_notifications),
            ]
            if global_window_seconds > 0 and global_max_notifications > 0:
                scopes.append(("global", global_window_seconds, global_max_notifications))
            for scope_key, window_seconds_value, max_count in scopes:
                state = connection.execute("SELECT * FROM notification_dedup WHERE scope_key = ?", (scope_key,)).fetchone()
                if (scope_key.startswith("incident:") or scope_key.startswith("recovery:")) and cooldown_seconds > 0 and state and state["last_notified_at"] is not None and now_ts < int(state["last_notified_at"]) + cooldown_seconds:
                    self._record_suppression_tx(connection, identity_key_value, transition, policy_id, channel_id, "incident_cooldown", now_ts)
                    self._increment_suppressed_tx(connection, scope_key, now_ts)
                    return None, "incident_cooldown"
                if max_count is not None:
                    count = 0
                    window_start = now_ts
                    if state and state["window_started_at"] is not None and now_ts < int(state["window_started_at"]) + window_seconds_value:
                        count = int(state["window_count"])
                        window_start = int(state["window_started_at"])
                    if count >= max_count:
                        self._record_suppression_tx(connection, identity_key_value, transition, policy_id, channel_id, "rate_limit", now_ts)
                        self._increment_suppressed_tx(connection, scope_key, now_ts)
                        return None, "rate_limit"

            cursor = connection.execute(
                """INSERT OR IGNORE INTO notifications
                (identity_key, transition_id, incident_id, event_id, policy_id, channel_id, trigger,
                 status, severity, resource_type, resource_id, resource_name, project_path, title, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (identity_key_value, transition.id, transition.incident_id, transition.event_id, policy_id, channel_id,
                 transition.transition.value, NotificationStatus.PENDING.value, transition.current_severity.value,
                 transition.resource.resource_type.value, transition.resource.identifier, transition.resource.name,
                 transition.resource.project_path, title, body, now_ts),
            )
            if cursor.rowcount != 1:
                self._record_suppression_tx(connection, identity_key_value, transition, policy_id, channel_id, "duplicate_identity", now_ts)
                return None, "duplicate_identity"
            notification_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO notification_deliveries (notification_id, channel_id, status, provider_request_key, created_at) VALUES (?, ?, ?, ?, ?)",
                (notification_id, channel_id, DeliveryStatus.PENDING.value, provider_key(notification_id, channel_id), now_ts),
            )
            for scope_key, window_seconds_value, max_count in scopes:
                state = connection.execute("SELECT * FROM notification_dedup WHERE scope_key = ?", (scope_key,)).fetchone()
                if max_count is None:
                    connection.execute(
                        "INSERT INTO notification_dedup (scope_key, last_notified_at, updated_at) VALUES (?, ?, ?) ON CONFLICT(scope_key) DO UPDATE SET last_notified_at = excluded.last_notified_at, updated_at = excluded.updated_at",
                        (scope_key, now_ts, now_ts),
                    )
                else:
                    if state and state["window_started_at"] is not None and now_ts < int(state["window_started_at"]) + window_seconds_value:
                        window_start = int(state["window_started_at"])
                        count = int(state["window_count"]) + 1
                    else:
                        window_start = now_ts
                        count = 1
                    connection.execute(
                        """INSERT INTO notification_dedup (scope_key, window_started_at, window_count, updated_at)
                        VALUES (?, ?, ?, ?) ON CONFLICT(scope_key) DO UPDATE SET window_started_at = excluded.window_started_at, window_count = excluded.window_count, updated_at = excluded.updated_at""",
                        (scope_key, window_start, count, now_ts),
                    )
            return notification_id, None

    def create_notification(self, *, identity_key: str, transition: IncidentTransition, policy_id: str, channel_id: str, status: NotificationStatus, title: str, body: str, suppressed_reason: str | None = None) -> int:
        if status is NotificationStatus.SUPPRESSED:
            self.record_suppression(identity_key_value=identity_key, transition=transition, policy_id=policy_id, channel_id=channel_id, reason=suppressed_reason or "suppressed")
            return 0
        notification_id, _ = self.create_decision(identity_key_value=identity_key, transition=transition, policy_id=policy_id, channel_id=channel_id, title=title, body=body, cooldown_seconds=0, window_seconds=1, max_notifications=10**9)
        return int(notification_id or 0)

    def create_delivery(self, notification_id: int, channel_id: str, provider_request_key: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO notification_deliveries (notification_id, channel_id, status, provider_request_key, created_at) VALUES (?, ?, ?, ?, ?)",
                (notification_id, channel_id, DeliveryStatus.PENDING.value, provider_request_key, _timestamp(datetime.now(timezone.utc))),
            )
            row = connection.execute("SELECT id FROM notification_deliveries WHERE provider_request_key = ?", (provider_request_key,)).fetchone()
            return int(row[0]) if row else int(cursor.lastrowid)

    def claim_due(self, now: datetime, lease_seconds: int = 60) -> tuple[int, Notification] | None:
        now_ts = _timestamp(now)
        with self._connection() as connection:
            row = connection.execute(
                """SELECT d.id AS delivery_id, d.status AS delivery_status, d.lease_until AS delivery_lease_until, n.*
                FROM notification_deliveries d JOIN notifications n ON n.id = d.notification_id
                WHERE ((d.status = ? AND n.status = ?)
                    OR (d.status = ? AND d.lease_until < ? AND n.status = ?))
                  AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= ?)
                ORDER BY COALESCE(d.next_attempt_at, n.created_at), d.id LIMIT 1""",
                (DeliveryStatus.PENDING.value, NotificationStatus.PENDING.value,
                 DeliveryStatus.SENDING.value, now_ts, NotificationStatus.SENDING.value, now_ts),
            ).fetchone()
            if row is None:
                return None
            expected_status = row["delivery_status"]
            expired = expected_status == DeliveryStatus.SENDING.value
            if expired:
                update = connection.execute(
                    """UPDATE notification_deliveries SET status = ?, lease_until = ?, last_attempt_at = ?, attempt_count = attempt_count + 1, lease_expiry_count = lease_expiry_count + 1
                    WHERE id = ? AND status = ? AND lease_until < ?""",
                    (DeliveryStatus.SENDING.value, now_ts + lease_seconds, now_ts, row["delivery_id"], DeliveryStatus.SENDING.value, now_ts),
                )
            else:
                update = connection.execute(
                    """UPDATE notification_deliveries SET status = ?, lease_until = ?, last_attempt_at = ?, attempt_count = attempt_count + 1
                    WHERE id = ? AND status = ?""",
                    (DeliveryStatus.SENDING.value, now_ts + lease_seconds, now_ts, row["delivery_id"], DeliveryStatus.PENDING.value),
                )
            if update.rowcount != 1:
                return None
            connection.execute("UPDATE notifications SET status = ?, attempt_count = attempt_count + 1 WHERE id = ? AND status IN (?, ?)", (NotificationStatus.SENDING.value, row["id"], NotificationStatus.PENDING.value, NotificationStatus.SENDING.value))
            updated = connection.execute("SELECT * FROM notifications WHERE id = ?", (row["id"],)).fetchone()
            lease_token = hashlib.sha256(f"lease|{row['delivery_id']}|{now_ts}|{updated['attempt_count']}".encode()).hexdigest()
            token_update = connection.execute("UPDATE notification_deliveries SET lease_token = ? WHERE id = ? AND status = ? AND lease_until = ?", (lease_token, row["delivery_id"], DeliveryStatus.SENDING.value, now_ts + lease_seconds))
            if token_update.rowcount != 1:
                return None
        return int(row["delivery_id"]), self._notification(updated, lease_token=lease_token)

    def finish_delivery(self, delivery_id: int, result: DeliveryStatus, *, retryable: bool, max_attempts: int, lease_token: str | None = None, provider_message_id: str | None = None, provider_status_code: int | None = None, error_code: str | None = None, error_message: str | None = None, next_attempt_at: datetime | None = None) -> None:
        now = datetime.now(timezone.utc)
        with self._connection() as connection:
            row = connection.execute("SELECT notification_id, attempt_count FROM notification_deliveries WHERE id = ?", (delivery_id,)).fetchone()
            if row is None:
                return
            attempt_number = int(row["attempt_count"])
            exhausted = result is DeliveryStatus.FAILED and retryable and attempt_number >= max_attempts
            if result is DeliveryStatus.SENT:
                notification_status = NotificationStatus.SENT
                scheduled = None
            elif result is DeliveryStatus.UNKNOWN:
                notification_status = NotificationStatus.UNKNOWN
                scheduled = None
            elif retryable and not exhausted:
                notification_status = NotificationStatus.PENDING
                scheduled = next_attempt_at
            else:
                notification_status = NotificationStatus.FAILED
                scheduled = None
            delivery_status = DeliveryStatus.PENDING if retryable and not exhausted else result
            ownership = " AND lease_token = ?" if lease_token else ""
            ownership_values = (delivery_id, DeliveryStatus.SENDING.value, lease_token) if lease_token else (delivery_id, DeliveryStatus.SENDING.value)
            completion = connection.execute(
                f"UPDATE notification_deliveries SET status = ?, provider_message_id = ?, lease_until = NULL, lease_token = NULL, next_attempt_at = ? WHERE id = ? AND status = ?{ownership}",
                (delivery_status.value, provider_message_id, _timestamp(scheduled) if scheduled else None, *ownership_values),
            )
            if completion.rowcount != 1:
                return
            connection.execute(
                "UPDATE notifications SET status = ?, next_attempt_at = ?, retry_exhausted_at = ? WHERE id = ?",
                (notification_status.value, _timestamp(scheduled) if scheduled else None, _timestamp(now) if exhausted else None, row["notification_id"]),
            )
            connection.execute(
                """INSERT INTO notification_attempts
                (delivery_id, attempt_number, started_at, finished_at, outcome, retryable, provider_status_code, error_code, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (delivery_id, attempt_number, _timestamp(now), _timestamp(now), result.value, int(retryable), provider_status_code, error_code, _safe_error(error_message)),
            )

    def retry_delivery(self, notification_id: int, *, allow_unknown: bool = False, actor: str = "operator", reason: str = "operator retry") -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT n.status AS notification_status, n.manual_retry_count, d.id AS delivery_id, d.status AS delivery_status
                FROM notifications n JOIN notification_deliveries d ON d.notification_id = n.id WHERE n.id = ?""", (notification_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Notification not found")
            status = NotificationStatus(row["notification_status"])
            if status is NotificationStatus.UNKNOWN and not allow_unknown:
                raise ValueError("UNKNOWN delivery requires explicit provider reconciliation or confirmation")
            if status not in {NotificationStatus.FAILED, NotificationStatus.UNKNOWN}:
                raise ValueError("Only FAILED or explicitly reconciled UNKNOWN deliveries can be retried")
            if int(row["manual_retry_count"]) >= 3:
                raise ValueError("Manual retry limit reached")
            connection.execute("UPDATE notification_deliveries SET status = ?, next_attempt_at = NULL, lease_until = NULL WHERE id = ? AND status IN (?, ?)", (DeliveryStatus.PENDING.value, row["delivery_id"], DeliveryStatus.FAILED.value, DeliveryStatus.UNKNOWN.value))
            connection.execute("UPDATE notifications SET status = ?, next_attempt_at = NULL, retry_exhausted_at = NULL, manual_retry_count = manual_retry_count + 1 WHERE id = ?", (NotificationStatus.PENDING.value, notification_id))
            connection.execute("INSERT INTO notification_actions (notification_id, action, previous_status, new_status, actor, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (notification_id, "retry_requested", status.value, NotificationStatus.PENDING.value, actor, _safe_error(reason), _timestamp(datetime.now(timezone.utc))))
            return True

    def reconcile_unknown(self, notification_id: int, *, delivered: bool, actor: str = "operator", reason: str = "provider reconciliation") -> bool:
        with self._connection() as connection:
            row = connection.execute("SELECT status FROM notifications WHERE id = ?", (notification_id,)).fetchone()
            if row is None:
                raise ValueError("Notification not found")
            if row["status"] != NotificationStatus.UNKNOWN.value:
                raise ValueError("Only UNKNOWN notifications can be reconciled")
            new_status = NotificationStatus.SENT if delivered else NotificationStatus.FAILED
            delivery_status = DeliveryStatus.SENT if delivered else DeliveryStatus.FAILED
            connection.execute("UPDATE notifications SET status = ?, next_attempt_at = NULL WHERE id = ?", (new_status.value, notification_id))
            connection.execute("UPDATE notification_deliveries SET status = ?, lease_until = NULL, next_attempt_at = NULL WHERE notification_id = ?", (delivery_status.value, notification_id))
            connection.execute("INSERT INTO notification_actions (notification_id, action, previous_status, new_status, actor, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (notification_id, "reconcile_delivered" if delivered else "reconcile_not_delivered", NotificationStatus.UNKNOWN.value, new_status.value, actor, _safe_error(reason), _timestamp(datetime.now(timezone.utc))))
            return True

    def retain(self, cutoff: datetime) -> dict[str, int]:
        cutoff_ts = _timestamp(cutoff)
        result: dict[str, int] = {}
        with self._connection() as connection:
            result["attempts"] = connection.execute("DELETE FROM notification_attempts WHERE finished_at IS NOT NULL AND finished_at < ? AND delivery_id IN (SELECT d.id FROM notification_deliveries d JOIN notifications n ON n.id = d.notification_id WHERE n.status NOT IN (?, ?, ?) AND n.created_at < ? AND NOT EXISTS (SELECT 1 FROM incidents i WHERE i.id = n.incident_id AND i.status IN (?, ?)))", (cutoff_ts, NotificationStatus.PENDING.value, NotificationStatus.SENDING.value, NotificationStatus.UNKNOWN.value, cutoff_ts, IncidentStatus.OPEN.value, IncidentStatus.ACKNOWLEDGED.value)).rowcount
            result["actions"] = connection.execute("DELETE FROM notification_actions WHERE created_at < ? AND notification_id NOT IN (SELECT n.id FROM notifications n WHERE n.status IN (?, ?, ?) OR EXISTS (SELECT 1 FROM incidents i WHERE i.id = n.incident_id AND i.status IN (?, ?)))", (cutoff_ts, NotificationStatus.PENDING.value, NotificationStatus.SENDING.value, NotificationStatus.UNKNOWN.value, IncidentStatus.OPEN.value, IncidentStatus.ACKNOWLEDGED.value)).rowcount
            result["suppressions"] = connection.execute("DELETE FROM notification_suppressions WHERE created_at < ? AND NOT EXISTS (SELECT 1 FROM incidents i WHERE i.id = notification_suppressions.incident_id AND i.status IN (?, ?))", (cutoff_ts, IncidentStatus.OPEN.value, IncidentStatus.ACKNOWLEDGED.value)).rowcount
            result["notifications"] = connection.execute("DELETE FROM notifications WHERE created_at < ? AND status NOT IN (?, ?, ?) AND NOT EXISTS (SELECT 1 FROM incidents i WHERE i.id = notifications.incident_id AND i.status IN (?, ?))", (cutoff_ts, NotificationStatus.PENDING.value, NotificationStatus.SENDING.value, NotificationStatus.UNKNOWN.value, IncidentStatus.OPEN.value, IncidentStatus.ACKNOWLEDGED.value)).rowcount
            result["dedup"] = connection.execute("DELETE FROM notification_dedup WHERE updated_at < ?", (cutoff_ts,)).rowcount
        return result

    def metrics(self, now: datetime | None = None) -> dict[str, object]:
        now_ts = _timestamp(now or datetime.now(timezone.utc))
        with self._connection() as connection:
            counts = {row["status"]: int(row["count"]) for row in connection.execute("SELECT status, COUNT(*) AS count FROM notifications GROUP BY status").fetchall()}
            suppressed_count = int(connection.execute("SELECT COUNT(*) FROM notification_suppressions").fetchone()[0])
            pending_age = connection.execute("SELECT MIN(created_at) FROM notifications WHERE status = ?", (NotificationStatus.PENDING.value,)).fetchone()[0]
            unknown_age = connection.execute("SELECT MIN(created_at) FROM notifications WHERE status = ?", (NotificationStatus.UNKNOWN.value,)).fetchone()[0]
            exhausted = connection.execute("SELECT COUNT(*) FROM notifications WHERE retry_exhausted_at IS NOT NULL").fetchone()[0]
            lease_expiry = connection.execute("SELECT COALESCE(SUM(lease_expiry_count), 0) FROM notification_deliveries").fetchone()[0]
            channels = [dict(row) for row in connection.execute("SELECT channel_id, SUM(status = 'sent') AS sent, SUM(status = 'failed') AS failed, SUM(status = 'unknown') AS unknown, COUNT(*) AS total FROM notification_deliveries GROUP BY channel_id").fetchall()]
            latency = connection.execute("SELECT AVG(finished_at - started_at) FROM notification_attempts WHERE finished_at IS NOT NULL AND finished_at >= ?", (now_ts - 86400,)).fetchone()[0]
        return {"pending": counts.get("pending", 0), "sending": counts.get("sending", 0), "failed": counts.get("failed", 0), "unknown": counts.get("unknown", 0), "suppressed": suppressed_count + counts.get("suppressed", 0), "sent": counts.get("sent", 0), "oldest_pending_age_seconds": max(0, now_ts - int(pending_age)) if pending_age else None, "oldest_unknown_age_seconds": max(0, now_ts - int(unknown_age)) if unknown_age else None, "retry_exhaustion_count": int(exhausted), "lease_expiry_count": int(lease_expiry), "recent_delivery_latency_seconds": float(latency) if latency is not None else None, "channels": channels}

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

    def _record_suppression_tx(self, connection: sqlite3.Connection, identity_key_value: str, transition: IncidentTransition, policy_id: str, channel_id: str, reason: str, now_ts: int) -> None:
        connection.execute("INSERT OR IGNORE INTO notification_suppressions (identity_key, transition_id, incident_id, policy_id, channel_id, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (identity_key_value, transition.id, transition.incident_id, policy_id, channel_id, reason, now_ts))

    def _increment_suppressed_tx(self, connection: sqlite3.Connection, scope_key: str, now_ts: int) -> None:
        connection.execute("INSERT INTO notification_dedup (scope_key, suppressed_count, updated_at) VALUES (?, 1, ?) ON CONFLICT(scope_key) DO UPDATE SET suppressed_count = notification_dedup.suppressed_count + 1, updated_at = excluded.updated_at", (scope_key, now_ts))

    def _transition(self, row: sqlite3.Row) -> IncidentTransition:
        return IncidentTransition(int(row["id"]), int(row["incident_id"]), row["incident_key"], NotificationTrigger(row["transition"]), _datetime(row["occurred_at"]), IncidentStatus(row["previous_status"]) if row["previous_status"] else None, IncidentStatus(row["current_status"]), Severity(row["previous_severity"]) if row["previous_severity"] else None, Severity(row["current_severity"]), row["event_id"], row["source_event_key"], row["correlation_key"], ResourceRef(ResourceType(row["resource_type"]), row["resource_id"], row["resource_name"], row["project_path"]), EventType(row["event_type"]) if row["event_type"] else None)

    def _notification(self, row: sqlite3.Row, *, lease_token: str | None = None) -> Notification:
        return Notification(int(row["id"]), row["identity_key"], int(row["incident_id"]), row["event_id"], int(row["transition_id"]), row["policy_id"], row["channel_id"], NotificationTrigger(row["trigger"]), NotificationStatus(row["status"]), Severity(row["severity"]), ResourceRef(ResourceType(row["resource_type"]), row["resource_id"], row["resource_name"], row["project_path"]), row["title"], row["body"], _datetime(row["created_at"]), _datetime(row["next_attempt_at"]) if row["next_attempt_at"] else None, int(row["attempt_count"]), row["suppressed_reason"], lease_token)

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
