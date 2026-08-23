from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from aipm.core.exceptions import AIPMError
from aipm.models.mission_control_evidence import HistoricalPoint
from aipm.models.history import (
    ContainerHistoryPoint,
    HistoricalRun,
    HostHistoryPoint,
    ProjectHistoryPoint,
    SampleRunRecord,
    TunnelHistoryPoint,
)
from aipm.repositories.readonly import require_read_only_filesystem


RETENTION_BATCH_SIZE = 5000
_LOCK_RETRY_LIMIT = 3
_MIN_BATCH_LIMIT = 100
_RETENTION_BATCH_SLEEP_SECONDS = 0.05


SCHEMA = """
CREATE TABLE IF NOT EXISTS sample_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at INTEGER NOT NULL,
    host_available INTEGER NOT NULL,
    docker_available INTEGER NOT NULL,
    projects_available INTEGER NOT NULL,
    tunnel_state TEXT NOT NULL,
    duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS host_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES sample_runs(id) ON DELETE CASCADE,
    sampled_at INTEGER NOT NULL,
    hostname TEXT,
    cpu_percent REAL,
    load_one REAL,
    load_five REAL,
    load_fifteen REAL,
    memory_total_gb REAL,
    memory_used_gb REAL,
    memory_available_gb REAL,
    memory_percent REAL,
    swap_total_gb REAL,
    swap_used_gb REAL,
    swap_percent REAL,
    disk_total_gb REAL,
    disk_used_gb REAL,
    disk_free_gb REAL,
    disk_percent REAL,
    network_interfaces INTEGER,
    network_established INTEGER,
    available INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS container_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES sample_runs(id) ON DELETE CASCADE,
    sampled_at INTEGER NOT NULL,
    container_id TEXT NOT NULL,
    container_name TEXT NOT NULL,
    image TEXT,
    state TEXT,
    health TEXT,
    stack TEXT,
    restart_count INTEGER,
    cpu_percent REAL,
    memory_used_mb REAL,
    memory_limit_mb REAL,
    memory_percent REAL,
    stats_available INTEGER NOT NULL,
    resource_sampled_at INTEGER,
    resource_status TEXT NOT NULL DEFAULT 'never_sampled',
    resource_age_seconds INTEGER
);

CREATE TABLE IF NOT EXISTS project_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES sample_runs(id) ON DELETE CASCADE,
    sampled_at INTEGER NOT NULL,
    name TEXT NOT NULL,
    path TEXT,
    branch TEXT,
    has_git INTEGER NOT NULL,
    has_compose INTEGER NOT NULL,
    dirty INTEGER,
    ahead INTEGER,
    behind INTEGER
);

CREATE TABLE IF NOT EXISTS tunnel_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES sample_runs(id) ON DELETE CASCADE,
    sampled_at INTEGER NOT NULL,
    state TEXT NOT NULL,
    source TEXT NOT NULL,
    systemd TEXT,
    local_containers TEXT
);

CREATE INDEX IF NOT EXISTS idx_sample_runs_sampled_at ON sample_runs(sampled_at);
CREATE INDEX IF NOT EXISTS idx_host_samples_sampled_at ON host_samples(sampled_at);
CREATE INDEX IF NOT EXISTS idx_host_samples_run_id ON host_samples(run_id);
CREATE INDEX IF NOT EXISTS idx_container_samples_identity_time ON container_samples(container_id, sampled_at);
CREATE INDEX IF NOT EXISTS idx_container_samples_name_time ON container_samples(container_name, sampled_at);
CREATE INDEX IF NOT EXISTS idx_container_samples_sampled_at ON container_samples(sampled_at);
CREATE INDEX IF NOT EXISTS idx_container_samples_run_id ON container_samples(run_id);
CREATE INDEX IF NOT EXISTS idx_project_samples_name_time ON project_samples(name, sampled_at);
CREATE INDEX IF NOT EXISTS idx_project_samples_sampled_at ON project_samples(sampled_at);
CREATE INDEX IF NOT EXISTS idx_project_samples_run_id ON project_samples(run_id);
CREATE INDEX IF NOT EXISTS idx_tunnel_samples_sampled_at ON tunnel_samples(sampled_at);
CREATE INDEX IF NOT EXISTS idx_tunnel_samples_run_id ON tunnel_samples(run_id);

CREATE TABLE IF NOT EXISTS resource_sample_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at INTEGER NOT NULL,
    duration_ms INTEGER,
    status TEXT NOT NULL,
    error_code TEXT,
    container_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_resource_sample_runs_sampled_at ON resource_sample_runs(sampled_at);

CREATE TABLE IF NOT EXISTS container_resource_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_run_id INTEGER NOT NULL REFERENCES resource_sample_runs(id) ON DELETE CASCADE,
    sampled_at INTEGER NOT NULL,
    container_id TEXT NOT NULL,
    container_name TEXT NOT NULL,
    cpu_percent REAL,
    memory_used_mb REAL,
    memory_limit_mb REAL,
    memory_percent REAL,
    available INTEGER NOT NULL,
    error_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_container_resource_samples_identity_time ON container_resource_samples(container_id, sampled_at);
CREATE INDEX IF NOT EXISTS idx_container_resource_samples_name_time ON container_resource_samples(container_name, sampled_at);
CREATE INDEX IF NOT EXISTS idx_container_resource_samples_sampled_at ON container_resource_samples(sampled_at);
CREATE INDEX IF NOT EXISTS idx_container_resource_samples_resource_run_id ON container_resource_samples(resource_run_id);
"""


class SQLiteHistoryRepository:
    """SQLite-backed history repository with one short-lived connection per operation."""

    def __init__(self, database_path: str | Path, *, read_only: bool = False):
        self.database_path = Path(database_path).expanduser()
        self.read_only = read_only
        if self.read_only:
            require_read_only_filesystem(self.database_path)
        else:
            self.initialize()

    def _validate_read_only_path(self) -> None:
        if not self.database_path.is_file():
            raise FileNotFoundError(f"Read-only telemetry database does not exist: {self.database_path}")

    def _assert_writable(self) -> None:
        if self.read_only:
            raise AIPMError("Telemetry repository is read-only")

    def initialize(self) -> None:
        self._assert_writable()
        if self.database_path.exists() and self.database_path.is_dir():
            raise ValueError("Telemetry database path points to a directory.")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(SCHEMA)
            self._migrate_columns(connection)
            self._ensure_retention_indexes(connection)

    def _migrate_columns(self, connection: sqlite3.Connection) -> None:
        self._assert_writable()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(container_samples)").fetchall()}
        for name, definition in (("resource_sampled_at", "INTEGER"), ("resource_status", "TEXT NOT NULL DEFAULT 'never_sampled'"), ("resource_age_seconds", "INTEGER")):
            if name not in columns:
                connection.execute(f"ALTER TABLE container_samples ADD COLUMN {name} {definition}")

    def _ensure_retention_indexes(self, connection: sqlite3.Connection) -> None:
        self._assert_writable()
        for statement in (
            "CREATE INDEX IF NOT EXISTS idx_container_samples_sampled_at ON container_samples(sampled_at)",
            "CREATE INDEX IF NOT EXISTS idx_project_samples_sampled_at ON project_samples(sampled_at)",
            "CREATE INDEX IF NOT EXISTS idx_container_resource_samples_sampled_at ON container_resource_samples(sampled_at)",
            "CREATE INDEX IF NOT EXISTS idx_host_samples_run_id ON host_samples(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_container_samples_run_id ON container_samples(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_project_samples_run_id ON project_samples(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_tunnel_samples_run_id ON tunnel_samples(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_container_resource_samples_resource_run_id ON container_resource_samples(resource_run_id)",
        ):
            connection.execute(statement)

    def save_sample(
        self,
        run: SampleRunRecord,
        host: HostHistoryPoint | None,
        containers: Sequence[ContainerHistoryPoint],
        projects: Sequence[ProjectHistoryPoint],
        tunnel: TunnelHistoryPoint | None,
    ) -> int:
        self._assert_writable()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO sample_runs
                    (sampled_at, host_available, docker_available, projects_available, tunnel_state, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _timestamp(run.sampled_at),
                    int(run.host_available),
                    int(run.docker_available),
                    int(run.projects_available),
                    run.tunnel_state,
                    run.duration_ms,
                ),
            )
            run_id = int(cursor.lastrowid)
            if host is not None:
                connection.execute(
                    """
                    INSERT INTO host_samples (
                        run_id, sampled_at, hostname, cpu_percent, load_one, load_five, load_fifteen,
                        memory_total_gb, memory_used_gb, memory_available_gb, memory_percent,
                        swap_total_gb, swap_used_gb, swap_percent,
                        disk_total_gb, disk_used_gb, disk_free_gb, disk_percent,
                        network_interfaces, network_established, available
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        _timestamp(host.sampled_at),
                        host.hostname,
                        host.cpu_percent,
                        host.load_one,
                        host.load_five,
                        host.load_fifteen,
                        host.memory_total_gb,
                        host.memory_used_gb,
                        host.memory_available_gb,
                        host.memory_percent,
                        host.swap_total_gb,
                        host.swap_used_gb,
                        host.swap_percent,
                        host.disk_total_gb,
                        host.disk_used_gb,
                        host.disk_free_gb,
                        host.disk_percent,
                        host.network_interfaces,
                        host.network_established,
                        int(host.available),
                    ),
                )
            connection.executemany(
                """
                INSERT INTO container_samples (
                    run_id, sampled_at, container_id, container_name, image, state, health, stack,
                    restart_count, cpu_percent, memory_used_mb, memory_limit_mb, memory_percent, stats_available,
                    resource_sampled_at, resource_status, resource_age_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        _timestamp(item.sampled_at),
                        item.container_id,
                        item.container_name,
                        item.image,
                        item.state,
                        item.health,
                        item.stack,
                        item.restart_count,
                        item.cpu_percent,
                        item.memory_used_mb,
                        item.memory_limit_mb,
                        item.memory_percent,
                        int(item.stats_available),
                        _timestamp(item.resource_sampled_at) if item.resource_sampled_at else None,
                        item.resource_status,
                        item.resource_age_seconds,
                    )
                    for item in containers
                ],
            )
            connection.executemany(
                """
                INSERT INTO project_samples (
                    run_id, sampled_at, name, path, branch, has_git, has_compose, dirty, ahead, behind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        _timestamp(item.sampled_at),
                        item.name,
                        item.path,
                        item.branch,
                        int(item.has_git),
                        int(item.has_compose),
                        None if item.dirty is None else int(item.dirty),
                        item.ahead,
                        item.behind,
                    )
                    for item in projects
                ],
            )
            if tunnel is not None:
                connection.execute(
                    """
                    INSERT INTO tunnel_samples (run_id, sampled_at, state, source, systemd, local_containers)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        _timestamp(tunnel.sampled_at),
                        tunnel.state,
                        tunnel.source,
                        tunnel.systemd,
                        ",".join(tunnel.local_containers),
                    ),
                )
            return run_id

    def save_resource_sample(self, sampled_at: datetime, containers: Sequence[ContainerHistoryPoint], *, duration_ms: int | None, status: str, error_code: str | None = None) -> int:
        self._assert_writable()
        with self._connection() as connection:
            cursor = connection.execute("INSERT INTO resource_sample_runs (sampled_at, duration_ms, status, error_code, container_count) VALUES (?, ?, ?, ?, ?)", (_timestamp(sampled_at), duration_ms, status, error_code, len(containers)))
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """INSERT INTO container_resource_samples
                (resource_run_id, sampled_at, container_id, container_name, cpu_percent, memory_used_mb, memory_limit_mb, memory_percent, available, error_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(run_id, _timestamp(item.resource_sampled_at or sampled_at), item.container_id, item.container_name, item.cpu_percent, item.memory_used_mb, item.memory_limit_mb, item.memory_percent, int(item.stats_available), None if item.stats_available else "RESOURCE_UNAVAILABLE") for item in containers],
            )
            return run_id

    def get_latest_resource_samples(self) -> list[ContainerHistoryPoint]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM container_resource_samples ORDER BY sampled_at DESC, id DESC").fetchall()
        latest: dict[str, ContainerHistoryPoint] = {}
        for row in rows:
            if row["container_id"] not in latest:
                sampled_at = _datetime(row["sampled_at"])
                latest[row["container_id"]] = ContainerHistoryPoint(sampled_at=sampled_at, container_id=row["container_id"], container_name=row["container_name"], image=None, state=None, health=None, stack=None, restart_count=None, cpu_percent=row["cpu_percent"], memory_used_mb=row["memory_used_mb"], memory_limit_mb=row["memory_limit_mb"], memory_percent=row["memory_percent"], stats_available=bool(row["available"]), resource_sampled_at=sampled_at, resource_status="fresh" if row["available"] else "unavailable", resource_age_seconds=0)
        return list(latest.values())

    def get_resource_history(self, name: str | None, start: datetime | None, end: datetime | None, limit: int) -> list[ContainerHistoryPoint]:
        extra = "container_name = ?" if name else None
        values = [name] if name else []
        rows = self._query("SELECT * FROM container_resource_samples", start, end, limit, extra, values)
        return [ContainerHistoryPoint(sampled_at=_datetime(row["sampled_at"]), container_id=row["container_id"], container_name=row["container_name"], image=None, state=None, health=None, stack=None, restart_count=None, cpu_percent=row["cpu_percent"], memory_used_mb=row["memory_used_mb"], memory_limit_mb=row["memory_limit_mb"], memory_percent=row["memory_percent"], stats_available=bool(row["available"]), resource_sampled_at=_datetime(row["sampled_at"]), resource_status="fresh" if row["available"] else "unavailable", resource_age_seconds=0) for row in rows]

    def get_runs(self, after_id: int | None, limit: int) -> list[HistoricalRun]:
        conditions = "WHERE id > ?" if after_id is not None else ""
        values = [after_id] if after_id is not None else []
        values.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM sample_runs {conditions} ORDER BY id ASC LIMIT ?",
                values,
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def get_run(self, run_id: int) -> HistoricalRun | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM sample_runs WHERE id = ?", (run_id,)).fetchone()
        return _run_from_row(row) if row is not None else None

    def get_previous_run(self, run_id: int) -> HistoricalRun | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM sample_runs WHERE id < ? ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
        return _run_from_row(row) if row is not None else None

    def get_host_history(self, start: datetime | None, end: datetime | None, limit: int) -> list[HostHistoryPoint]:
        rows = self._query("SELECT * FROM host_samples", start, end, limit)
        return [
            HostHistoryPoint(
                sampled_at=_datetime(row["sampled_at"]),
                hostname=row["hostname"],
                cpu_percent=row["cpu_percent"],
                load_one=row["load_one"],
                load_five=row["load_five"],
                load_fifteen=row["load_fifteen"],
                memory_total_gb=row["memory_total_gb"],
                memory_used_gb=row["memory_used_gb"],
                memory_available_gb=row["memory_available_gb"],
                memory_percent=row["memory_percent"],
                swap_total_gb=row["swap_total_gb"],
                swap_used_gb=row["swap_used_gb"],
                swap_percent=row["swap_percent"],
                disk_total_gb=row["disk_total_gb"],
                disk_used_gb=row["disk_used_gb"],
                disk_free_gb=row["disk_free_gb"],
                disk_percent=row["disk_percent"],
                network_interfaces=row["network_interfaces"],
                network_established=row["network_established"],
                available=bool(row["available"]),
            )
            for row in rows
        ]

    def get_latest_host_at(self, end: datetime) -> HistoricalPoint | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM host_samples WHERE sampled_at <= ? ORDER BY sampled_at DESC, id DESC LIMIT 1", (_timestamp(end),)).fetchone()
        if row is None:
            return None
        point = HostHistoryPoint(sampled_at=_datetime(row["sampled_at"]), hostname=row["hostname"], cpu_percent=row["cpu_percent"], load_one=row["load_one"], load_five=row["load_five"], load_fifteen=row["load_fifteen"], memory_total_gb=row["memory_total_gb"], memory_used_gb=row["memory_used_gb"], memory_available_gb=row["memory_available_gb"], memory_percent=row["memory_percent"], swap_total_gb=row["swap_total_gb"], swap_used_gb=row["swap_used_gb"], swap_percent=row["swap_percent"], disk_total_gb=row["disk_total_gb"], disk_used_gb=row["disk_used_gb"], disk_free_gb=row["disk_free_gb"], disk_percent=row["disk_percent"], network_interfaces=row["network_interfaces"], network_established=row["network_established"], available=bool(row["available"]))
        return HistoricalPoint(point, int(row["run_id"]) if row["run_id"] is not None else None)

    def get_latest_container_at(self, name: str, end: datetime) -> HistoricalPoint | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM container_samples WHERE sampled_at <= ? AND container_name = ? ORDER BY sampled_at DESC, id DESC LIMIT 1", (_timestamp(end), name)).fetchone()
        return HistoricalPoint(_container_from_row(row), int(row["run_id"]) if row is not None and row["run_id"] is not None else None) if row is not None else None

    def get_latest_project_at(self, name: str, end: datetime) -> HistoricalPoint | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM project_samples WHERE sampled_at <= ? AND name = ? ORDER BY sampled_at DESC, id DESC LIMIT 1", (_timestamp(end), name)).fetchone()
        return HistoricalPoint(_project_from_row(row), int(row["run_id"]) if row is not None and row["run_id"] is not None else None) if row is not None else None

    def get_latest_tunnel_at(self, end: datetime) -> HistoricalPoint | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM tunnel_samples WHERE sampled_at <= ? ORDER BY sampled_at DESC, id DESC LIMIT 1", (_timestamp(end),)).fetchone()
        return HistoricalPoint(_tunnel_from_row(row), int(row["run_id"]) if row is not None and row["run_id"] is not None else None) if row is not None else None

    def get_containers_for_run(self, run_id: int) -> list[ContainerHistoryPoint]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM container_samples WHERE run_id = ? ORDER BY id ASC", (run_id,)).fetchall()
        return [_container_from_row(row) for row in rows]

    def get_projects_for_run(self, run_id: int) -> list[ProjectHistoryPoint]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM project_samples WHERE run_id = ? ORDER BY id ASC", (run_id,)).fetchall()
        return [_project_from_row(row) for row in rows]

    def get_tunnel_for_run(self, run_id: int) -> TunnelHistoryPoint | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM tunnel_samples WHERE run_id = ? ORDER BY id ASC LIMIT 1", (run_id,)).fetchone()
        return _tunnel_from_row(row) if row is not None else None

    def get_container_history(
        self,
        name: str | None,
        start: datetime | None,
        end: datetime | None,
        limit: int,
    ) -> list[ContainerHistoryPoint]:
        extra = ("container_name = ?", [name]) if name else (None, [])
        rows = self._query("SELECT * FROM container_samples", start, end, limit, extra[0], extra[1])
        return [
            ContainerHistoryPoint(
                sampled_at=_datetime(row["sampled_at"]),
                container_id=row["container_id"],
                container_name=row["container_name"],
                image=row["image"],
                state=row["state"],
                health=row["health"],
                stack=row["stack"],
                restart_count=row["restart_count"],
                cpu_percent=row["cpu_percent"],
                memory_used_mb=row["memory_used_mb"],
                memory_limit_mb=row["memory_limit_mb"],
                memory_percent=row["memory_percent"],
                stats_available=bool(row["stats_available"]),
                resource_sampled_at=_datetime(row["resource_sampled_at"]) if row["resource_sampled_at"] else None,
                resource_status=row["resource_status"] if row["resource_status"] else ("fresh" if row["stats_available"] else "unavailable"),
                resource_age_seconds=row["resource_age_seconds"],
            )
            for row in rows
        ]

    def get_project_history(
        self,
        name: str | None,
        start: datetime | None,
        end: datetime | None,
        limit: int,
    ) -> list[ProjectHistoryPoint]:
        condition = "name = ?" if name else None
        rows = self._query("SELECT * FROM project_samples", start, end, limit, condition, [name] if name else [])
        return [
            ProjectHistoryPoint(
                sampled_at=_datetime(row["sampled_at"]),
                name=row["name"],
                path=row["path"],
                branch=row["branch"],
                has_git=bool(row["has_git"]),
                has_compose=bool(row["has_compose"]),
                dirty=None if row["dirty"] is None else bool(row["dirty"]),
                ahead=row["ahead"],
                behind=row["behind"],
            )
            for row in rows
        ]

    def get_tunnel_history(self, start: datetime | None, end: datetime | None, limit: int) -> list[TunnelHistoryPoint]:
        rows = self._query("SELECT * FROM tunnel_samples", start, end, limit)
        return [
            TunnelHistoryPoint(
                sampled_at=_datetime(row["sampled_at"]),
                state=row["state"],
                source=row["source"],
                systemd=row["systemd"],
                local_containers=tuple(filter(None, (row["local_containers"] or "").split(","))),
            )
            for row in rows
        ]

    def delete_older_than(self, cutoff: datetime) -> int:
        """Delete stale telemetry in bounded, dependency-safe transactions."""
        self._assert_writable()
        cutoff_ts = _timestamp(cutoff)
        total = 0

        for table in ("host_samples", "container_samples", "project_samples", "tunnel_samples"):
            while True:
                deleted = self._delete_batch(table, "sampled_at < ?", (cutoff_ts,))
                total += deleted
                if deleted == 0:
                    break

        while True:
            deleted = self._delete_batch(
                "container_resource_samples",
                "resource_run_id IN (SELECT id FROM resource_sample_runs WHERE sampled_at < ?)",
                (cutoff_ts,),
            )
            total += deleted
            if deleted == 0:
                break

        while True:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    DELETE FROM resource_sample_runs
                    WHERE id IN (
                        SELECT parent.id
                        FROM resource_sample_runs AS parent
                        WHERE parent.sampled_at < ?
                          AND NOT EXISTS (
                              SELECT 1
                              FROM container_resource_samples AS child
                              WHERE child.resource_run_id = parent.id
                          )
                        LIMIT ?
                    )
                    """,
                    (cutoff_ts, RETENTION_BATCH_SIZE),
                )
                deleted = cursor.rowcount if cursor.rowcount >= 0 else 0
            total += deleted
            if deleted == 0:
                break

        with self._connection() as connection:
            has_events_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
            ).fetchone() is not None
        event_dependency_guard = ""
        if has_events_table:
            event_dependency_guard = "AND NOT EXISTS (SELECT 1 FROM events AS child WHERE child.source_run_id = parent.id)"

        sample_runs_sql = f"""
            DELETE FROM sample_runs
            WHERE id IN (
                SELECT parent.id
                FROM sample_runs AS parent
                WHERE parent.sampled_at < ?
                  AND NOT EXISTS (SELECT 1 FROM host_samples AS child WHERE child.run_id = parent.id)
                  AND NOT EXISTS (SELECT 1 FROM container_samples AS child WHERE child.run_id = parent.id)
                  AND NOT EXISTS (SELECT 1 FROM project_samples AS child WHERE child.run_id = parent.id)
                  AND NOT EXISTS (SELECT 1 FROM tunnel_samples AS child WHERE child.run_id = parent.id)
                {event_dependency_guard}
                LIMIT ?
            )
        """
        while True:
            deleted = self._execute_guarded(sample_runs_sql, (cutoff_ts,)) or 0
            total += deleted
            if deleted == 0:
                break
            time.sleep(_RETENTION_BATCH_SLEEP_SECONDS)

        return total

    def _execute_guarded(self, sql: str, params: Sequence[object]) -> int | None:
        """Run one bounded DELETE batch with lock and foreign-key guards.

        Returns the deleted row count, or ``None`` when the batch must be
        abandoned for this pass: either the write lock stayed busy after
        bounded retries, or shrinking the batch could not get past
        foreign-key-poisoned rows. Committed progress from earlier batches
        is kept. Returning instead of raising prevents retention from
        hot-looping against sibling writers on the same database.
        """
        limit = RETENTION_BATCH_SIZE
        lock_failures = 0
        while True:
            try:
                with self._connection() as connection:
                    cursor = connection.execute(sql, (*params, limit))
                    return cursor.rowcount if cursor.rowcount >= 0 else 0
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                lock_failures += 1
                if lock_failures > _LOCK_RETRY_LIMIT:
                    return None
                time.sleep(0.25 * lock_failures)
            except sqlite3.IntegrityError:
                if limit <= _MIN_BATCH_LIMIT:
                    return None
                limit //= 4

    def _delete_batch(self, table: str, predicate: str, values: tuple[object, ...]) -> int:
        sql = f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {predicate} LIMIT ?)"
        return self._execute_guarded(sql, values) or 0

    def close(self) -> None:
        """Connections are short-lived; this method exists for lifecycle symmetry."""

    def _query(
        self,
        select: str,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        extra_condition: str | None = None,
        extra_values: Sequence[object] = (),
    ) -> list[sqlite3.Row]:
        conditions: list[str] = []
        values: list[object] = []
        if start is not None:
            conditions.append("sampled_at >= ?")
            values.append(_timestamp(start))
        if end is not None:
            conditions.append("sampled_at <= ?")
            values.append(_timestamp(end))
        if extra_condition:
            conditions.append(extra_condition)
            values.extend(extra_values)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"{select}{where} ORDER BY sampled_at ASC, id ASC LIMIT ?"
        values.append(limit)
        with self._connection() as connection:
            return list(connection.execute(sql, values).fetchall())

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
                # SQLite builds/filesystems that cannot enable WAL still remain usable.
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
        raise ValueError("Telemetry timestamps must be timezone-aware UTC datetimes.")
    return int(value.astimezone(timezone.utc).timestamp())


def _datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _run_from_row(row: sqlite3.Row) -> HistoricalRun:
    return HistoricalRun(
        id=int(row["id"]),
        sampled_at=_datetime(row["sampled_at"]),
        host_available=bool(row["host_available"]),
        docker_available=bool(row["docker_available"]),
        projects_available=bool(row["projects_available"]),
        tunnel_state=row["tunnel_state"],
    )


def _container_from_row(row: sqlite3.Row) -> ContainerHistoryPoint:
    return ContainerHistoryPoint(
        sampled_at=_datetime(row["sampled_at"]),
        container_id=row["container_id"],
        container_name=row["container_name"],
        image=row["image"],
        state=row["state"],
        health=row["health"],
        stack=row["stack"],
        restart_count=row["restart_count"],
        cpu_percent=row["cpu_percent"],
        memory_used_mb=row["memory_used_mb"],
        memory_limit_mb=row["memory_limit_mb"],
        memory_percent=row["memory_percent"],
        stats_available=bool(row["stats_available"]),
        resource_sampled_at=_datetime(row["resource_sampled_at"]) if row["resource_sampled_at"] else None,
        resource_status=row["resource_status"] if "resource_status" in row.keys() and row["resource_status"] else ("fresh" if row["stats_available"] else "unavailable"),
        resource_age_seconds=row["resource_age_seconds"] if "resource_age_seconds" in row.keys() else None,
    )


def _project_from_row(row: sqlite3.Row) -> ProjectHistoryPoint:
    return ProjectHistoryPoint(
        sampled_at=_datetime(row["sampled_at"]),
        name=row["name"],
        path=row["path"],
        branch=row["branch"],
        has_git=bool(row["has_git"]),
        has_compose=bool(row["has_compose"]),
        dirty=None if row["dirty"] is None else bool(row["dirty"]),
        ahead=row["ahead"],
        behind=row["behind"],
    )


def _tunnel_from_row(row: sqlite3.Row) -> TunnelHistoryPoint:
    return TunnelHistoryPoint(
        sampled_at=_datetime(row["sampled_at"]),
        state=row["state"],
        source=row["source"],
        systemd=row["systemd"],
        local_containers=tuple(filter(None, (row["local_containers"] or "").split(","))),
    )
