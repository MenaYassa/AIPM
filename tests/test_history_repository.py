import os
import sqlite3
from datetime import datetime, timedelta, timezone

from aipm.models.history import (
    ContainerHistoryPoint,
    HostHistoryPoint,
    ProjectHistoryPoint,
    SampleRunRecord,
    TunnelHistoryPoint,
)
from aipm.repositories.events.sqlite import SCHEMA as EVENTS_SCHEMA
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


def _index_names(path):
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def _retention_plan(path, table):
    with sqlite3.connect(path) as connection:
        return " ".join(str(row[-1]) for row in connection.execute(f"EXPLAIN QUERY PLAN DELETE FROM {table} WHERE sampled_at < ?", (0,)).fetchall())


def _dependency_plan(path, child_table, child_column):
    with sqlite3.connect(path) as connection:
        return " ".join(
            str(row[-1])
            for row in connection.execute(
                f"EXPLAIN QUERY PLAN SELECT 1 FROM {child_table} AS child WHERE child.{child_column} = ?",
                (1,),
            ).fetchall()
        )


def _table_counts(path):
    with sqlite3.connect(path) as connection:
        return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("sample_runs", "host_samples", "container_samples", "project_samples", "tunnel_samples", "resource_sample_runs", "container_resource_samples")}


def test_fresh_schema_creates_retention_indexes_and_uses_them(tmp_path):
    path = tmp_path / "telemetry.db"
    SQLiteHistoryRepository(path)
    expected = {
        "idx_container_samples_sampled_at",
        "idx_project_samples_sampled_at",
        "idx_container_resource_samples_sampled_at",
        "idx_host_samples_run_id",
        "idx_container_samples_run_id",
        "idx_project_samples_run_id",
        "idx_tunnel_samples_run_id",
        "idx_container_resource_samples_resource_run_id",
    }
    assert expected <= _index_names(path)
    for table, index in (
        ("container_samples", "idx_container_samples_sampled_at"),
        ("project_samples", "idx_project_samples_sampled_at"),
        ("container_resource_samples", "idx_container_resource_samples_sampled_at"),
    ):
        plan = _retention_plan(path, table)
        assert index in plan
        assert f"SCAN {table}" not in plan


def test_fresh_schema_uses_child_lookup_indexes_for_retention_dependencies(tmp_path):
    path = tmp_path / "telemetry.db"
    SQLiteHistoryRepository(path)
    expected = (
        ("host_samples", "run_id", "idx_host_samples_run_id"),
        ("container_samples", "run_id", "idx_container_samples_run_id"),
        ("project_samples", "run_id", "idx_project_samples_run_id"),
        ("tunnel_samples", "run_id", "idx_tunnel_samples_run_id"),
        ("container_resource_samples", "resource_run_id", "idx_container_resource_samples_resource_run_id"),
    )
    indexes = _index_names(path)
    for table, column, index in expected:
        assert index in indexes
        plan = _dependency_plan(path, table, column)
        assert index in plan
        assert f"SCAN {table}" not in plan
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_existing_schema_migration_is_idempotent_and_preserves_rows(tmp_path):
    path = tmp_path / "telemetry.db"
    repository = SQLiteHistoryRepository(path)
    at = datetime(2026, 8, 16, tzinfo=UTC)
    run, host, containers, projects, tunnel = sample_rows(at)
    repository.save_sample(run, host, containers, projects, tunnel)
    before = _table_counts(path)
    with sqlite3.connect(path) as connection:
        for index in (
            "idx_container_samples_sampled_at",
            "idx_project_samples_sampled_at",
            "idx_container_resource_samples_sampled_at",
            "idx_host_samples_run_id",
            "idx_container_samples_run_id",
            "idx_project_samples_run_id",
            "idx_tunnel_samples_run_id",
            "idx_container_resource_samples_resource_run_id",
        ):
            connection.execute(f"DROP INDEX {index}")
    assert not {
        "idx_container_samples_sampled_at",
        "idx_project_samples_sampled_at",
        "idx_container_resource_samples_sampled_at",
        "idx_host_samples_run_id",
        "idx_container_samples_run_id",
        "idx_project_samples_run_id",
        "idx_tunnel_samples_run_id",
        "idx_container_resource_samples_resource_run_id",
    } <= _index_names(path)

    SQLiteHistoryRepository(path)
    migrated = _table_counts(path)
    assert migrated == before
    assert {
        "idx_container_samples_sampled_at",
        "idx_project_samples_sampled_at",
        "idx_container_resource_samples_sampled_at",
        "idx_host_samples_run_id",
        "idx_container_samples_run_id",
        "idx_project_samples_run_id",
        "idx_tunnel_samples_run_id",
        "idx_container_resource_samples_resource_run_id",
    } <= _index_names(path)
    SQLiteHistoryRepository(path)
    assert _table_counts(path) == before
    for table in ("container_samples", "project_samples", "container_resource_samples"):
        assert f"SCAN {table}" not in _retention_plan(path, table)
    for table, column, index in (
        ("host_samples", "run_id", "idx_host_samples_run_id"),
        ("container_samples", "run_id", "idx_container_samples_run_id"),
        ("project_samples", "run_id", "idx_project_samples_run_id"),
        ("tunnel_samples", "run_id", "idx_tunnel_samples_run_id"),
        ("container_resource_samples", "resource_run_id", "idx_container_resource_samples_resource_run_id"),
    ):
        plan = _dependency_plan(path, table, column)
        assert index in plan
        assert f"SCAN {table}" not in plan


def test_retention_deletes_old_rows_with_indexed_plans(tmp_path):
    path = tmp_path / "telemetry.db"
    repository = SQLiteHistoryRepository(path)
    old = datetime(2026, 8, 15, tzinfo=UTC)
    recent = datetime(2026, 8, 16, tzinfo=UTC)
    for at in (old, recent):
        run, host, containers, projects, tunnel = sample_rows(at)
        repository.save_sample(run, host, containers, projects, tunnel)
    deleted = repository.delete_older_than(recent)
    assert deleted >= 5
    assert _table_counts(path)["sample_runs"] == 1
    for table in ("container_samples", "project_samples", "container_resource_samples"):
        assert f"SCAN {table}" not in _retention_plan(path, table)


def test_retention_preserves_event_referenced_sample_runs_until_event_is_removed(tmp_path):
    path = tmp_path / "telemetry.db"
    repository = SQLiteHistoryRepository(path)
    old = datetime(2026, 8, 15, tzinfo=UTC)
    recent = datetime(2026, 8, 16, tzinfo=UTC)
    run, host, containers, projects, tunnel = sample_rows(old)
    run_id = repository.save_sample(run, host, containers, projects, tunnel)

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(EVENTS_SCHEMA)
        connection.execute(
            """
            INSERT INTO events (
                event_key, occurred_at, event_type, severity, source,
                resource_type, resource_id, title, description,
                source_run_id, correlation_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "event-retention-1",
                int(old.timestamp()),
                "container_state_changed",
                "warning",
                "telemetry",
                "container",
                "id-1",
                "Retention dependency",
                "Keeps the source run referenced",
                run_id,
                "container:id-1",
                int(old.timestamp()),
            ),
        )

    deleted_before_event_removal = repository.delete_older_than(recent)
    assert deleted_before_event_removal >= 5
    counts = _table_counts(path)
    assert counts["sample_runs"] == 1
    assert counts["host_samples"] == 0
    assert counts["container_samples"] == 0
    assert counts["project_samples"] == 0
    assert counts["tunnel_samples"] == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.execute("DELETE FROM events WHERE source_run_id = ?", (run_id,))

    deleted_after_event_removal = repository.delete_older_than(recent)
    assert deleted_after_event_removal == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sample_runs").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_read_only_open_does_not_mutate_retention_schema(tmp_path):
    path = tmp_path / "telemetry.db"
    SQLiteHistoryRepository(path)
    before = _index_names(path)
    directory = path.parent
    original_directory_mode = directory.stat().st_mode & 0o777
    tracked = {candidate: candidate.stat().st_mode & 0o777 for candidate in directory.glob(path.name + "*")}
    try:
        for candidate in tracked:
            os.chmod(candidate, 0o444)
        os.chmod(directory, 0o555)
        SQLiteHistoryRepository(path, read_only=True)
        assert _index_names(path) == before
    finally:
        os.chmod(directory, original_directory_mode)
        for candidate, mode in tracked.items():
            if candidate.exists():
                os.chmod(candidate, mode)


def test_retention_removes_resource_children_by_parent_age(tmp_path):
    path = tmp_path / "telemetry.db"
    repository = SQLiteHistoryRepository(path)
    old = datetime(2026, 8, 15, tzinfo=UTC)
    recent = datetime(2026, 8, 16, tzinfo=UTC)
    point = sample_rows(old)[2][0]
    repository.save_resource_sample(old, [point], duration_ms=1, status="healthy")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE container_resource_samples SET sampled_at = ? WHERE resource_run_id = 1", (int(recent.timestamp()),))
    deleted = repository.delete_older_than(recent)
    assert deleted == 2
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM resource_sample_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM container_resource_samples").fetchone()[0] == 0


def test_retention_deletes_in_configured_batches(tmp_path, monkeypatch):
    import aipm.repositories.telemetry.sqlite as telemetry_sqlite

    path = tmp_path / "telemetry.db"
    repository = SQLiteHistoryRepository(path)
    at = datetime(2026, 8, 15, tzinfo=UTC)
    for _ in range(5):
        run, host, containers, projects, tunnel = sample_rows(at)
        repository.save_sample(run, host, containers, projects, tunnel)
    monkeypatch.setattr(telemetry_sqlite, "RETENTION_BATCH_SIZE", 2)
    deleted = repository.delete_older_than(datetime(2026, 8, 16, tzinfo=UTC))
    assert deleted >= 25
    assert _table_counts(path) == {table: 0 for table in _table_counts(path)}


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
