from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from aipm.models.history import (
    ContainerHistoryPoint,
    HistoricalRun,
    HostHistoryPoint,
    ProjectHistoryPoint,
    SampleRunRecord,
    TunnelHistoryPoint,
)


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
    stats_available INTEGER NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_container_samples_identity_time ON container_samples(container_id, sampled_at);
CREATE INDEX IF NOT EXISTS idx_container_samples_name_time ON container_samples(container_name, sampled_at);
CREATE INDEX IF NOT EXISTS idx_project_samples_name_time ON project_samples(name, sampled_at);
CREATE INDEX IF NOT EXISTS idx_tunnel_samples_sampled_at ON tunnel_samples(sampled_at);
"""


class SQLiteHistoryRepository:
    """SQLite-backed history repository with one short-lived connection per operation."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).expanduser()
        self.initialize()

    def initialize(self) -> None:
        if self.database_path.exists() and self.database_path.is_dir():
            raise ValueError("Telemetry database path points to a directory.")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(SCHEMA)

    def save_sample(
        self,
        run: SampleRunRecord,
        host: HostHistoryPoint | None,
        containers: Sequence[ContainerHistoryPoint],
        projects: Sequence[ProjectHistoryPoint],
        tunnel: TunnelHistoryPoint | None,
    ) -> int:
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
                    restart_count, cpu_percent, memory_used_mb, memory_limit_mb, memory_percent, stats_available
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        cutoff_ts = _timestamp(cutoff)
        with self._connection() as connection:
            total = 0
            for table in ("host_samples", "container_samples", "project_samples", "tunnel_samples", "sample_runs"):
                cursor = connection.execute(f"DELETE FROM {table} WHERE sampled_at < ?", (cutoff_ts,))
                total += cursor.rowcount if cursor.rowcount >= 0 else 0
            return total

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
