from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aipm.core.exceptions import AIPMError
from aipm.dashboard.server import create_app
from aipm.models.config import AIPMConfig, DiscoveryConfig, TelemetryConfig
from aipm.repositories.events.sqlite import SQLiteEventRepository
from aipm.repositories.incidents.sqlite import SQLiteIncidentRepository
from aipm.repositories.notifications.sqlite import SQLiteNotificationRepository
from aipm.repositories.readonly import ReadOnlyFilesystemError
from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository


UTC_NOW = datetime.now(UTC)


def db_fingerprint(path: Path) -> dict[str, object]:
    files = {}
    for candidate in sorted(path.parent.glob(path.name + "*")):
        stat = candidate.stat()
        files[candidate.name] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        }
    with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as connection:
        schema = connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    return {
        "files": files,
        "schema": schema,
        "journal_mode": journal_mode,
        "tables": tables,
    }


@contextmanager
def filesystem_read_only(database: Path):
    directory = database.parent
    directory_mode = directory.stat().st_mode & 0o777
    tracked = {directory: directory_mode}
    for candidate in sorted(directory.glob(database.name + "*")):
        tracked[candidate] = candidate.stat().st_mode & 0o777
    for candidate in tracked:
        if candidate != directory:
            os.chmod(candidate, 0o444)
    os.chmod(directory, 0o555)
    try:
        yield
    finally:
        os.chmod(directory, directory_mode)
        for candidate, mode in tracked.items():
            if candidate != directory and candidate.exists():
                os.chmod(candidate, mode)


def seeded_database(path: Path) -> None:
    history = SQLiteHistoryRepository(path)
    history.save_resource_sample(UTC_NOW, [], duration_ms=0, status="healthy")
    SQLiteNotificationRepository(path)


def test_read_only_constructors_do_not_create_missing_paths(tmp_path: Path) -> None:
    constructors = (
        SQLiteHistoryRepository,
        SQLiteEventRepository,
        SQLiteIncidentRepository,
        SQLiteNotificationRepository,
    )
    for constructor in constructors:
        database = tmp_path / f"{constructor.__name__}.db"
        with pytest.raises(FileNotFoundError):
            constructor(database, read_only=True)
        assert not database.exists()


def test_read_only_constructors_reject_unprotected_existing_paths(tmp_path: Path) -> None:
    database = tmp_path / "unprotected.db"
    seeded_database(database)
    with pytest.raises(ReadOnlyFilesystemError):
        SQLiteHistoryRepository(database, read_only=True)


def test_read_only_constructors_do_not_initialize_or_migrate(tmp_path: Path) -> None:
    database = tmp_path / "empty.db"
    sqlite3.connect(database).close()
    before = db_fingerprint(database)

    with filesystem_read_only(database):
        repositories = (
            SQLiteHistoryRepository(database, read_only=True),
            SQLiteEventRepository(database, read_only=True),
            SQLiteIncidentRepository(database, read_only=True),
            SQLiteNotificationRepository(database, read_only=True),
        )
        for repository in repositories:
            with pytest.raises(sqlite3.OperationalError):
                if isinstance(repository, SQLiteHistoryRepository):
                    repository.get_runs(None, 10)
                elif isinstance(repository, SQLiteEventRepository):
                    repository.get_events(SimpleNamespace(status=None, severity=None, event_type=None, resource_type=None, resource_id=None, start=None, end=None, limit=10))
                elif isinstance(repository, SQLiteIncidentRepository):
                    repository.get_incidents(SimpleNamespace(status=None, severity=None, resource_id=None, start=None, end=None, limit=10))
                else:
                    repository.schema_version()
        assert db_fingerprint(database) == before


def test_read_only_connection_rejects_dml_and_ddl(tmp_path: Path) -> None:
    database = tmp_path / "seeded.db"
    seeded_database(database)
    with filesystem_read_only(database):
        before = db_fingerprint(database)
        repository = SQLiteHistoryRepository(database, read_only=True)

        with pytest.raises(sqlite3.OperationalError):
            with repository._connection() as connection:
                connection.execute("CREATE TABLE should_not_exist (id INTEGER)")
        with pytest.raises(sqlite3.OperationalError):
            with repository._connection() as connection:
                connection.execute("INSERT INTO sample_runs (sampled_at, host_available, docker_available, projects_available, tunnel_state) VALUES (1, 1, 1, 1, 'unknown')")
        with pytest.raises(sqlite3.OperationalError):
            with repository._connection() as connection:
                connection.execute("UPDATE sample_runs SET tunnel_state = 'changed'")
        with pytest.raises(sqlite3.OperationalError):
            with repository._connection() as connection:
                connection.execute("DELETE FROM sample_runs")

        assert db_fingerprint(database) == before


def test_read_only_migration_helpers_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "seeded.db"
    seeded_database(database)
    with filesystem_read_only(database):
        repository = SQLiteNotificationRepository(database, read_only=True)
        with repository._connection() as connection:
            with pytest.raises(AIPMError):
                repository._migrate(connection)
            with pytest.raises(AIPMError):
                repository._add_column(connection, "notifications", "blocked_column", "TEXT")
            with pytest.raises(AIPMError):
                repository._rebuild_legacy_tables(connection)


def test_read_only_public_write_methods_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "seeded.db"
    seeded_database(database)
    now = datetime.now(UTC)
    with filesystem_read_only(database):
        history = SQLiteHistoryRepository(database, read_only=True)
        events = SQLiteEventRepository(database, read_only=True)
        incidents = SQLiteIncidentRepository(database, read_only=True)
        notifications = SQLiteNotificationRepository(database, read_only=True)

        write_attempts = (
        (history.save_sample, (None, None, [], [], None), {}),
        (history.save_resource_sample, (now, []), {"duration_ms": 0, "status": "healthy"}),
        (history.delete_older_than, (now,), {}),
        (events.save_processed_run, (1, now, [], []), {}),
        (events.delete_old_events, (now,), {}),
        (incidents.apply_event, (None,), {"opens_incident": False, "resolves_incident": False}),
        (incidents.acknowledge, (1, now), {}),
        (incidents.delete_old_resolved, (now,), {}),
        (notifications.add_transition, (None,), {}),
        (notifications.mark_projected, (1,), {}),
        (notifications.record_suppression, (), {"identity_key_value": "x", "transition": None, "policy_id": "p", "channel_id": "c", "reason": "r"}),
        (notifications.create_decision, (), {"identity_key_value": "x", "transition": None, "policy_id": "p", "channel_id": "c", "title": "t", "body": "b", "cooldown_seconds": 0, "window_seconds": 1, "max_notifications": 1}),
        (notifications.create_notification, (), {"identity_key": "x", "transition": None, "policy_id": "p", "channel_id": "c", "status": None, "title": "t", "body": "b"}),
        (notifications.create_delivery, (1, "c", "k"), {}),
        (notifications.claim_due, (now,), {}),
        (notifications.finish_delivery, (1, None), {"retryable": False, "max_attempts": 1}),
        (notifications.retry_delivery, (1,), {}),
        (notifications.reconcile_unknown, (1,), {"delivered": False}),
        (notifications.retain, (now,), {}),
        )
        for method, args, kwargs in write_attempts:
            with pytest.raises(AIPMError):
                method(*args, **kwargs)


def test_default_read_write_constructor_and_writer_behavior_remain_intact(tmp_path: Path) -> None:
    database = tmp_path / "writer.db"
    repository = SQLiteHistoryRepository(database)
    assert repository.read_only is False
    assert database.is_file()
    run_id = repository.save_resource_sample(UTC_NOW, [], duration_ms=0, status="healthy")
    assert run_id == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM resource_sample_runs").fetchone()[0] == 1


def test_real_dashboard_gets_preserve_seeded_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = tmp_path / "dashboard.db"
    seeded_database(database)
    project_root = tmp_path / "projects"
    project_root.mkdir()

    host = SimpleNamespace(
        hostname="test-host",
        os="Linux",
        kernel="test-kernel",
        architecture="x86_64",
        python="3.12",
    )
    system = SimpleNamespace(
        summary=lambda: SimpleNamespace(
            host=host,
            cpu=SimpleNamespace(physical_cores=1, logical_cores=2, usage_percent=1.0),
            memory=SimpleNamespace(total_gb=4.0, used_gb=1.0, available_gb=3.0, percent=25.0),
            disk=SimpleNamespace(total_gb=20.0, used_gb=5.0, free_gb=15.0, percent=25.0),
        )
    )
    docker = SimpleNamespace(provider=SimpleNamespace(list_containers=lambda: []))
    config = AIPMConfig(
        discovery=DiscoveryConfig(search_paths=[str(project_root)]),
        telemetry=TelemetryConfig(database_path=str(database)),
    )
    application = SimpleNamespace(
        config=config,
        logger=logging.getLogger("mc51-test"),
        system=system,
        docker=docker,
    )
    monkeypatch.setattr(
        "aipm.services.telemetry.tunnel.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="unknown\n", returncode=3),
    )

    with filesystem_read_only(database):
        before = db_fingerprint(database)
        client = TestClient(create_app(application=application))
        paths = (
            "/healthz",
            "/",
            "/api/overview",
            "/api/services",
            "/api/events",
            "/api/events/1",
            "/api/incidents",
            "/api/incidents/1",
            "/api/notifications",
            "/api/notifications/1",
            "/api/notification-channels",
            "/api/notification-policies",
            "/api/notification-metrics",
            "/api/history/host?range=1h&limit=10",
            "/api/history/containers?range=1h&limit=10",
            "/api/history/container-resources?range=1h&limit=10",
            "/api/history/projects?range=1h&limit=10",
            "/api/history/tunnel?range=1h&limit=10",
        )
        for path in paths:
            response = client.get(path)
            assert response.status_code == 200, path

        assert db_fingerprint(database) == before
        with sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == before["journal_mode"]
            assert connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall() == before["tables"]


def _active_wal_source_with_snapshot(tmp_path: Path) -> tuple[Path, Path, sqlite3.Connection]:
    source = tmp_path / "active-wal-source.db"
    snapshot = tmp_path / "dashboard-snapshot.db"
    writable = SQLiteHistoryRepository(source)
    writable.close()
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    cursor = writer.execute(
        "INSERT INTO sample_runs (sampled_at, host_available, docker_available, projects_available, tunnel_state, duration_ms) VALUES (?, 1, 1, 1, 'healthy', 1)",
        (int(UTC_NOW.timestamp()),),
    )
    writer.execute(
        "INSERT INTO host_samples (run_id, sampled_at, available) VALUES (?, ?, 1)",
        (int(cursor.lastrowid), int(UTC_NOW.timestamp())),
    )
    writer.commit()
    wal = source.with_name(source.name + "-wal")
    assert wal.is_file() and wal.stat().st_size > 0
    writer.execute(f"VACUUM INTO '{snapshot}'")
    return source, snapshot, writer


def test_active_wal_mode_ro_requires_filesystem_read_only_boundary(tmp_path: Path) -> None:
    source, _snapshot, writer = _active_wal_source_with_snapshot(tmp_path)
    try:
        with filesystem_read_only(source):
            before = db_fingerprint(source)
            repository = SQLiteHistoryRepository(source, read_only=True)
            assert [item.id for item in repository.get_runs(None, 10)] == [1]
            with pytest.raises(sqlite3.OperationalError):
                with repository._connection() as connection:
                    connection.execute("INSERT INTO sample_runs (sampled_at, host_available, docker_available, projects_available, tunnel_state) VALUES (1, 1, 1, 1, 'blocked')")
            assert db_fingerprint(source) == before
    finally:
        writer.close()


def test_active_wal_snapshot_preserves_current_data_and_is_immutable(tmp_path: Path) -> None:
    source, snapshot, writer = _active_wal_source_with_snapshot(tmp_path)
    source_before_dashboard = db_fingerprint(source)
    try:
        with filesystem_read_only(snapshot):
            snapshot_before_dashboard = db_fingerprint(snapshot)
            repository = SQLiteHistoryRepository(snapshot, read_only=True)
            runs = repository.get_runs(None, 10)
            assert [item.id for item in runs] == [1]
            with pytest.raises(sqlite3.OperationalError):
                with repository._connection() as connection:
                    connection.execute("UPDATE sample_runs SET tunnel_state = 'blocked'")
            assert db_fingerprint(snapshot) == snapshot_before_dashboard
        assert db_fingerprint(source) == source_before_dashboard
    finally:
        writer.close()


def test_dashboard_reads_wal_consistent_snapshot_without_touching_source_or_snapshot(tmp_path: Path) -> None:
    source, snapshot, writer = _active_wal_source_with_snapshot(tmp_path)
    project_root = tmp_path / "projects"
    project_root.mkdir()
    host = SimpleNamespace(hostname="test-host", os="Linux", kernel="test-kernel", architecture="x86_64", python="3.12")
    system = SimpleNamespace(
        summary=lambda: SimpleNamespace(
            host=host,
            cpu=SimpleNamespace(physical_cores=1, logical_cores=2, usage_percent=1.0),
            memory=SimpleNamespace(total_gb=4.0, used_gb=1.0, available_gb=3.0, percent=25.0),
            disk=SimpleNamespace(total_gb=20.0, used_gb=5.0, free_gb=15.0, percent=25.0),
        )
    )
    docker = SimpleNamespace(provider=SimpleNamespace(list_containers=lambda: []))
    config = AIPMConfig(
        discovery=DiscoveryConfig(search_paths=[str(project_root)]),
        telemetry=TelemetryConfig(database_path=str(snapshot)),
    )
    application = SimpleNamespace(config=config, logger=logging.getLogger("mc511-active-wal"), system=system, docker=docker)
    source_before = db_fingerprint(source)
    try:
        with filesystem_read_only(snapshot):
            snapshot_before = db_fingerprint(snapshot)
            client = TestClient(create_app(application=application))
            for path in (
                "/api/overview",
                "/api/services",
                "/api/events",
                "/api/incidents",
                "/api/notifications",
                "/api/notification-metrics",
                "/api/history/host?range=1h&limit=10",
                "/api/history/containers?range=1h&limit=10",
                "/api/history/container-resources?range=1h&limit=10",
                "/api/history/projects?range=1h&limit=10",
                "/api/history/tunnel?range=1h&limit=10",
            ):
                assert client.get(path).status_code == 200
            assert db_fingerprint(snapshot) == snapshot_before
        assert db_fingerprint(source) == source_before
    finally:
        writer.close()


def test_dashboard_reads_active_wal_source_without_source_mutation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source, _snapshot, writer = _active_wal_source_with_snapshot(tmp_path)
    wal = source.with_name(source.name + "-wal")
    shm = source.with_name(source.name + "-shm")
    project_root = tmp_path / "projects"
    project_root.mkdir()
    host = SimpleNamespace(hostname="test-host", os="Linux", kernel="test-kernel", architecture="x86_64", python="3.12")
    system = SimpleNamespace(
        summary=lambda: SimpleNamespace(
            host=host,
            cpu=SimpleNamespace(physical_cores=1, logical_cores=2, usage_percent=1.0),
            memory=SimpleNamespace(total_gb=4.0, used_gb=1.0, available_gb=3.0, percent=25.0),
            disk=SimpleNamespace(total_gb=20.0, used_gb=5.0, free_gb=15.0, percent=25.0),
        )
    )
    docker = SimpleNamespace(provider=SimpleNamespace(list_containers=lambda: []))
    config = AIPMConfig(
        discovery=DiscoveryConfig(search_paths=[str(project_root)]),
        telemetry=TelemetryConfig(database_path=str(source)),
    )
    application = SimpleNamespace(config=config, logger=logging.getLogger("mc511-active-wal-source"), system=system, docker=docker)
    monkeypatch.setattr(
        "aipm.services.telemetry.tunnel.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="unknown\n", returncode=3),
    )
    try:
        with filesystem_read_only(source):
            before = db_fingerprint(source)
            client = TestClient(create_app(application=application))
            for path in (
                "/api/overview",
                "/api/services",
                "/api/events",
                "/api/incidents",
                "/api/notifications",
                "/api/notification-metrics",
                "/api/history/host?range=1h&limit=10",
                "/api/history/containers?range=1h&limit=10",
                "/api/history/container-resources?range=1h&limit=10",
                "/api/history/projects?range=1h&limit=10",
                "/api/history/tunnel?range=1h&limit=10",
            ):
                assert client.get(path).status_code == 200
            assert client.get("/api/history/host?range=1h&limit=10").json()["points"]
            assert db_fingerprint(source) == before
    finally:
        writer.close()


def test_dashboard_service_template_enforces_read_only_boundary() -> None:
    unit = Path(__file__).parents[1] / "ops/systemd/aipm-dashboard.service"
    text = unit.read_text(encoding="utf-8")
    assert "ExecStart=/home/ubuntu/aipm/.venv/bin/aipm dashboard --host 127.0.0.1 --port 8787" in text
    assert "ProtectSystem=strict" in text
    assert "ProtectHome=read-only" in text
    assert "ReadOnlyPaths=/home/ubuntu/aipm /home/ubuntu/.config/aipm /home/ubuntu/.local/state/aipm/telemetry" in text
    assert "ReadWritePaths=" not in text
    assert "--host 0.0.0.0" not in text
    assert "CapabilityBoundingSet=" in text
