import sqlite3
from datetime import datetime, timedelta, timezone

from aipm.models.history import (
    ContainerHistoryPoint,
    HostHistoryPoint,
    ProjectHistoryPoint,
    SampleRunRecord,
    TunnelHistoryPoint,
)
from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository


UTC = timezone.utc


def sample_rows(at: datetime):
    run = SampleRunRecord(at, True, True, True, "healthy", duration_ms=4)
    host = HostHistoryPoint(at, "host", 10.0, 1.0, 0.5, 0.25, 4.0, 1.0, 3.0, 25.0, 1.0, 0.1, 10.0, 20.0, 5.0, 15.0, 25.0, 1, 2, True)
    containers = [
        ContainerHistoryPoint(at, "id-1", "app", "app:latest", "running", "healthy", "stack", 2, 5.0, 10.0, 100.0, 10.0, True),
        ContainerHistoryPoint(at, "id-2", "worker", "worker:latest", "running", None, "stack", 0, None, None, None, None, False),
    ]
    projects = [ProjectHistoryPoint(at, "demo", "/srv/demo", "main", True, True, False, 0, 0)]
    tunnel = TunnelHistoryPoint(at, "healthy", "docker", "active", ("cloudflared",))
    return run, host, containers, projects, tunnel


def test_schema_save_and_query(tmp_path):
    repository = SQLiteHistoryRepository(tmp_path / "telemetry.db")
    at = datetime(2026, 8, 16, tzinfo=UTC)
    run, host, containers, projects, tunnel = sample_rows(at)
    run_id = repository.save_sample(run, host, containers, projects, tunnel)
    assert run_id == 1
    assert len(repository.get_host_history(None, None, 10)) == 1
    assert repository.get_container_history("app", None, None, 10)[0].container_id == "id-1"
    assert len(repository.get_container_history(None, None, None, 10)) == 2
    assert repository.get_project_history("demo", None, None, 10)[0].branch == "main"
    assert repository.get_tunnel_history(None, None, 10)[0].local_containers == ("cloudflared",)


def test_sqlite_pragmas_are_enabled(tmp_path):
    path = tmp_path / "telemetry.db"
    repository = SQLiteHistoryRepository(path)
    with repository._connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_corrupted_database_fails_clearly(tmp_path):
    path = tmp_path / "telemetry.db"
    path.write_text("not sqlite", encoding="utf-8")
    try:
        SQLiteHistoryRepository(path)
    except sqlite3.DatabaseError:
        pass
    else:
        raise AssertionError("corrupted database was accepted")


def test_time_range_and_limit(tmp_path):
    repository = SQLiteHistoryRepository(tmp_path / "telemetry.db")
    first = datetime(2026, 8, 16, tzinfo=UTC)
    second = first + timedelta(minutes=1)
    for at in (first, second):
        run, host, containers, projects, tunnel = sample_rows(at)
        repository.save_sample(run, host, containers, projects, tunnel)
    assert len(repository.get_host_history(first, first, 10)) == 1
    assert len(repository.get_host_history(None, None, 1)) == 1


def test_retention_uses_timestamps_and_preserves_recent_rows(tmp_path):
    repository = SQLiteHistoryRepository(tmp_path / "telemetry.db")
    old = datetime(2026, 8, 15, tzinfo=UTC)
    recent = datetime(2026, 8, 16, tzinfo=UTC)
    for at in (old, recent):
        run, host, containers, projects, tunnel = sample_rows(at)
        repository.save_sample(run, host, containers, projects, tunnel)
    deleted = repository.delete_older_than(recent)
    assert deleted >= 5
    assert len(repository.get_host_history(None, None, 10)) == 1
    assert repository.get_host_history(None, None, 10)[0].sampled_at == recent
