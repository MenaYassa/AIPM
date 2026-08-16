from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
import time

from aipm.models.config import TelemetryConfig
from aipm.models.telemetry import ContainerSnapshot, ResourceStats, TelemetryError, TelemetryFreshness, FreshnessStatus
from aipm.services.telemetry.coordinator import TelemetrySamplingCoordinator
from aipm.providers.docker.provider import DockerProvider
from aipm.services.docker.service import DockerService
from aipm.services.telemetry.docker import DockerTelemetryService
from aipm.services.telemetry.runner import TelemetryRunner


UTC = timezone.utc


class Container:
    short_id = "abc123"
    id = "abc123full"
    name = "app"
    image = SimpleNamespace(tags=["example/app:latest"])
    labels = {}
    ports = {}
    attrs = {"State": {"Status": "running", "RestartCount": 2, "StartedAt": "2026-08-16T00:00:00Z", "Health": {"Status": "healthy"}}, "Config": {"Labels": {}}}


class Provider:
    def __init__(self):
        self.containers = [Container()]
        self.stats_calls = 0
        self.aggregate_calls = 0

    def list_containers(self):
        return self.containers

    def stats(self, _container):
        self.stats_calls += 1
        return {"cpu_stats": {}, "memory_stats": {}}

    def stats_all(self, timeout_seconds=15):
        self.aggregate_calls += 1
        return {"app": {"cpu_percent": 12.3, "memory_used_mb": 42.0, "memory_limit_mb": 100.0, "memory_percent": 42.0}}


def test_fast_snapshot_never_calls_per_container_stats():
    provider = Provider()
    service = DockerTelemetryService(DockerService(provider=provider), clock=lambda: datetime(2026, 8, 16, 12, 0, tzinfo=UTC))
    snapshot = service.fast_snapshot()
    assert provider.stats_calls == 0
    assert provider.aggregate_calls == 0
    assert snapshot.containers[0].resources.freshness.status is FreshnessStatus.NEVER_SAMPLED


def test_slow_refresh_uses_one_aggregate_operation_and_updates_cache():
    provider = Provider()
    service = DockerTelemetryService(DockerService(provider=provider), clock=lambda: datetime(2026, 8, 16, 12, 0, tzinfo=UTC), monotonic=lambda: 1.0)
    result = service.refresh_resources(timeout_seconds=15)
    assert result.status == "healthy"
    assert provider.aggregate_calls == 1
    assert provider.stats_calls == 0
    fast = service.fast_snapshot(now=datetime(2026, 8, 16, 12, 0, 10, tzinfo=UTC))
    assert fast.containers[0].resources.cpu_percent == 12.3
    assert fast.containers[0].resources.freshness.status is FreshnessStatus.FRESH
    assert fast.containers[0].resources.freshness.age_seconds == 10


def test_freshness_transitions_are_explicit():
    sampled = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    fresh = TelemetryFreshness.from_sample(sampled, now=sampled + timedelta(seconds=10), max_age_seconds=60)
    stale = TelemetryFreshness.from_sample(sampled, now=sampled + timedelta(seconds=61), max_age_seconds=60)
    unavailable = TelemetryFreshness.from_sample(sampled, now=sampled + timedelta(seconds=10), max_age_seconds=60, available=False, error=TelemetryError("X", "unavailable"))
    never = TelemetryFreshness.never_sampled(60)
    assert fresh.status is FreshnessStatus.FRESH
    assert stale.status is FreshnessStatus.STALE
    assert unavailable.status is FreshnessStatus.UNAVAILABLE
    assert never.status is FreshnessStatus.NEVER_SAMPLED


def test_aggregate_provider_parses_one_docker_stats_output(monkeypatch):
    class Completed:
        stdout = '{"Name":"app","CPUPerc":"12.3%","MemUsage":"42MiB / 100MiB","MemPerc":"42.0%"}\n'

    monkeypatch.setattr("aipm.providers.docker.provider.subprocess.run", lambda *args, **kwargs: Completed())
    provider = object.__new__(DockerProvider)
    result = provider.stats_all(timeout_seconds=15)
    assert result["app"] == {"cpu_percent": 12.3, "memory_used_mb": 42.0, "memory_limit_mb": 100.0, "memory_percent": 42.0}


def test_85_second_slow_resource_operation_does_not_delay_fast_loop():
    gate = Event()
    fast_calls: list[float] = []

    class Sampler:
        def __init__(self):
            self.coordinator = None

        def refresh_resource_once(self):
            # Represents the measured ~85-second operation in a scaled deterministic test.
            gate.wait(timeout=5)

        def refresh_project_once(self):
            return None

        def sample_fast_once(self):
            fast_calls.append(time.monotonic())
            if len(fast_calls) >= 4:
                self.coordinator.request_stop()
            return SimpleNamespace(error=None)

    sampler = Sampler()
    config = TelemetryConfig(interval_seconds=0.01, resource_interval_seconds=0.01, project_interval_seconds=10, resource_timeout_seconds=15)
    coordinator = TelemetrySamplingCoordinator(sampler, config, sleeper=lambda seconds: time.sleep(min(seconds, 0.003)))
    sampler.coordinator = coordinator
    started = time.monotonic()
    coordinator.run()
    elapsed = time.monotonic() - started
    assert len(fast_calls) >= 4
    assert elapsed < 0.5
    assert not gate.is_set()


def test_legacy_runner_keeps_synchronous_sample_path():
    class LegacySampler:
        def __init__(self):
            self.calls = 0

        def sample_once(self):
            self.calls += 1
            return SimpleNamespace(error=None)

    sampler = LegacySampler()
    config = TelemetryConfig(sampling_mode="legacy", interval_seconds=1)
    runner = TelemetryRunner(sampler, config, sleeper=lambda _seconds: runner.request_stop())
    runner.run()
    assert sampler.calls == 1


def test_sparse_resource_history_persists_and_queries(tmp_path):
    from aipm.models.history import ContainerHistoryPoint
    from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository

    repository = SQLiteHistoryRepository(tmp_path / "mc.db")
    sampled = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    point = ContainerHistoryPoint(sampled_at=sampled, container_id="abc", container_name="app", image=None, state=None, health=None, stack=None, restart_count=None, cpu_percent=12.0, memory_used_mb=42.0, memory_limit_mb=100.0, memory_percent=42.0, stats_available=True, resource_sampled_at=sampled, resource_status="fresh", resource_age_seconds=0)
    run_id = repository.save_resource_sample(sampled, [point], duration_ms=2100, status="healthy")
    assert run_id == 1
    rows = repository.get_resource_history("app", sampled - timedelta(seconds=1), sampled + timedelta(seconds=1), 10)
    assert rows[0].cpu_percent == 12.0
    assert rows[0].resource_status == "fresh"


def test_dashboard_mapper_exposes_stale_resource_freshness():
    from aipm.mappers.dashboard import DashboardResponseMapper
    from aipm.models.container import Container
    from aipm.models.telemetry import DockerSnapshot

    container = Container(id="abc", name="app", image="x", state="running", health="healthy", ports=[], labels={}, stack=None, created=datetime.now(UTC))
    freshness = TelemetryFreshness(datetime(2026, 8, 16, 12, 0, tzinfo=UTC), 181, FreshnessStatus.STALE, 180)
    snapshot = DockerSnapshot(available=True, status="healthy", containers=(ContainerSnapshot(container=container, resources=ResourceStats(cpu_percent=1.0, available=True, freshness=freshness)),))
    response = DashboardResponseMapper()._docker(SimpleNamespace(docker=snapshot))
    assert response["containers"][0]["stats"]["status"] == "stale"
    assert response["containers"][0]["stats"]["age_seconds"] == 181


def test_slow_resource_slot_is_single_flight_and_records_skips():
    gate = Event()
    started = []

    class Sampler:
        def refresh_resource_once(self):
            started.append("resource")
            gate.wait(timeout=0.15)

        def refresh_project_once(self):
            return None

        def sample_fast_once(self):
            return SimpleNamespace(error=None)

    sampler = Sampler()
    config = TelemetryConfig(interval_seconds=0.02, resource_interval_seconds=0.01, project_interval_seconds=10)
    coordinator = TelemetrySamplingCoordinator(sampler, config, sleeper=lambda seconds: time.sleep(min(seconds, 0.003)))
    worker = __import__("threading").Thread(target=coordinator.run, daemon=True)
    worker.start()
    time.sleep(0.06)
    coordinator.request_stop()
    gate.set()
    worker.join(timeout=1)
    assert started == ["resource"]
    assert coordinator.resource_slot.skipped_count > 0


def test_slow_slot_marks_timeout_without_starting_a_second_worker():
    gate = Event()
    from aipm.services.telemetry.coordinator import _SingleFlightSlot

    slot = _SingleFlightSlot("resource")
    assert slot.start(lambda: gate.wait(timeout=0.2), timeout_seconds=0.01)
    time.sleep(0.03)
    assert slot.state().last_status == "timeout"
    assert not slot.start(lambda: None, timeout_seconds=1)
    gate.set()


def test_legacy_container_schema_migrates_without_losing_rows(tmp_path):
    import sqlite3
    from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository

    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
    CREATE TABLE sample_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, sampled_at INTEGER NOT NULL, host_available INTEGER NOT NULL, docker_available INTEGER NOT NULL, projects_available INTEGER NOT NULL, tunnel_state TEXT NOT NULL, duration_ms INTEGER);
    CREATE TABLE container_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, sampled_at INTEGER NOT NULL, container_id TEXT NOT NULL, container_name TEXT NOT NULL, image TEXT, state TEXT, health TEXT, stack TEXT, restart_count INTEGER, cpu_percent REAL, memory_used_mb REAL, memory_limit_mb REAL, memory_percent REAL, stats_available INTEGER NOT NULL);
    INSERT INTO sample_runs VALUES (1, 1786881600, 1, 1, 1, 'healthy', 100);
    INSERT INTO container_samples VALUES (1, 1, 1786881600, 'abc', 'app', 'x', 'running', 'healthy', 'stack', 0, 1.0, 2.0, 10.0, 20.0, 1);
    """)
    connection.commit()
    connection.close()
    repository = SQLiteHistoryRepository(path)
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(container_samples)")}
    assert {"resource_sampled_at", "resource_status", "resource_age_seconds"} <= columns
    rows = repository.get_container_history("app", None, None, 10)
    assert rows[0].container_id == "abc"
    assert rows[0].cpu_percent == 1.0
