from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from aipm.models.config import TelemetryConfig
from aipm.models.container import Container
from aipm.models.cpu import CpuInfo
from aipm.models.disk import DiskInfo
from aipm.models.history import SampleResult
from aipm.models.host import HostInfo
from aipm.models.memory import MemoryInfo
from aipm.models.project import Project, ProjectCapabilities
from aipm.models.system import SystemSummary
from aipm.models.telemetry import (
    ContainerSnapshot,
    DashboardSnapshot,
    DockerSnapshot,
    HostSnapshot,
    NetworkStats,
    ProjectInventorySnapshot,
    ProjectSnapshot,
    ResourceStats,
    SwapStats,
    TunnelSnapshot,
)
from aipm.services.telemetry.runner import TelemetryRunner
from aipm.services.telemetry.sampler import TelemetrySampler
from aipm.mappers.telemetry_history import TelemetryHistoryMapper


UTC = timezone.utc
NOW = datetime(2026, 8, 16, tzinfo=UTC)


def dashboard_snapshot():
    system = SystemSummary(
        host=HostInfo("host", "Linux", "kernel", "x86_64", "3.12"),
        cpu=CpuInfo(1, 2, 10.0),
        memory=MemoryInfo(4.0, 1.0, 3.0, 25.0),
        disk=DiskInfo(20.0, 5.0, 15.0, 25.0),
    )
    container = Container(
        id="id-1",
        name="app",
        image="app:latest",
        state="running",
        health="healthy",
        ports=[],
        labels={},
        stack="stack",
        created=NOW,
    )
    project = Project(name="demo", path="/srv/demo", capabilities=ProjectCapabilities(has_git=True, has_compose=True))
    return DashboardSnapshot(
        generated_at=NOW,
        host=HostSnapshot(system, SwapStats(1.0, 0.1, 10.0), 1.0, 0.5, 0.25, 100, NetworkStats(1, 2)),
        docker=DockerSnapshot(True, "healthy", (ContainerSnapshot(container, ResourceStats(5.0, 10.0, 100.0, 10.0), 2, "2026-08-16T00:00:00Z"),)),
        projects=ProjectInventorySnapshot(True, "healthy", ("/srv",), (ProjectSnapshot(project),)),
        tunnel=TunnelSnapshot("healthy", "docker", ("cloudflared",), "active"),
    )


class FakeTelemetry:
    def __init__(self, snapshot=None, error=None):
        self.value = snapshot or dashboard_snapshot()
        self.error = error
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.value

    def fast_snapshot(self):
        if self.error:
            raise self.error
        return self.value


class FakeRepository:
    def __init__(self, error=None):
        self.error = error
        self.saved = []
        self.deleted = []

    def initialize(self):
        pass

    def save_sample(self, run, host, containers, projects, tunnel):
        if self.error:
            raise self.error
        self.saved.append((run, host, containers, projects, tunnel))
        return 42

    def delete_older_than(self, cutoff):
        self.deleted.append(cutoff)
        return 3

    def close(self):
        pass


def test_sampler_persists_mapped_snapshot_without_retention(tmp_path):
    repository = FakeRepository()
    config = TelemetryConfig(database_path=str(tmp_path / "telemetry.db"), retention_days=1)
    sampler = TelemetrySampler(FakeTelemetry(), TelemetryHistoryMapper(), repository, config, clock=lambda: NOW, monotonic=lambda: 1.0)
    result = sampler.sample_once()
    assert result.run_id == 42
    assert result.host_rows == 1
    assert result.container_rows == 1
    assert result.project_rows == 1
    assert result.tunnel_rows == 1
    assert result.retention_deleted == 0
    assert repository.saved[0][0].sampled_at == NOW
    assert repository.deleted == []


def test_fast_sampling_does_not_run_retention(tmp_path):
    repository = FakeRepository()
    config = TelemetryConfig(database_path=str(tmp_path / "telemetry.db"), retention_days=1)
    sampler = TelemetrySampler(FakeTelemetry(), TelemetryHistoryMapper(), repository, config, clock=lambda: NOW, monotonic=lambda: 1.0)
    result = sampler.sample_fast_once()
    assert result.error is None
    assert repository.deleted == []


def test_retention_cleanup_is_dedicated_and_reports_metrics(tmp_path):
    repository = FakeRepository()
    config = TelemetryConfig(database_path=str(tmp_path / "telemetry.db"), retention_days=1)
    sampler = TelemetrySampler(FakeTelemetry(), TelemetryHistoryMapper(), repository, config, clock=lambda: NOW, monotonic=lambda: 1.0)
    result = sampler.cleanup_retention()
    assert result.error is None
    assert result.deleted_rows == 3
    assert result.duration_ms == 0
    assert repository.deleted[0].tzinfo is UTC


def test_disabled_sampler_does_not_collect_or_write(tmp_path):
    telemetry = FakeTelemetry()
    repository = FakeRepository()
    config = TelemetryConfig(enabled=False, database_path=str(tmp_path / "telemetry.db"))
    result = TelemetrySampler(telemetry, TelemetryHistoryMapper(), repository, config, clock=lambda: NOW).sample_once()
    assert result.skipped is True
    assert telemetry.calls == 0
    assert repository.saved == []


def test_sampler_failure_is_safe_and_logged_by_caller():
    result = TelemetrySampler(
        FakeTelemetry(error=RuntimeError("host/docker failure")),
        TelemetryHistoryMapper(),
        FakeRepository(),
        TelemetryConfig(),
        clock=lambda: NOW,
    ).sample_once()
    assert result.error == "Telemetry sampling unavailable"
    assert result.run_id is None


def test_database_failure_does_not_mutate_infrastructure():
    telemetry = FakeTelemetry()
    result = TelemetrySampler(
        telemetry,
        TelemetryHistoryMapper(),
        FakeRepository(error=RuntimeError("db down")),
        TelemetryConfig(),
        clock=lambda: NOW,
    ).sample_once()
    assert result.error == "Telemetry sampling unavailable"
    assert telemetry.calls == 1


def test_partial_snapshot_is_mapped_without_fake_measurements():
    partial = DashboardSnapshot(
        generated_at=NOW,
        host=HostSnapshot.unavailable(__import__("aipm.models.telemetry", fromlist=["TelemetryError"]).TelemetryError("HOST", "unavailable")),
        docker=DockerSnapshot.unavailable_snapshot(__import__("aipm.models.telemetry", fromlist=["TelemetryError"]).TelemetryError("DOCKER", "unavailable")),
        projects=ProjectInventorySnapshot.unavailable_snapshot(__import__("aipm.models.telemetry", fromlist=["TelemetryError"]).TelemetryError("PROJECT", "unavailable")),
        tunnel=TunnelSnapshot("unknown", "not-detected"),
    )
    repository = FakeRepository()
    result = TelemetrySampler(
        FakeTelemetry(snapshot=partial),
        TelemetryHistoryMapper(),
        repository,
        TelemetryConfig(),
        clock=lambda: NOW,
    ).sample_once()
    assert result.error is None
    assert repository.saved[0][1].available is False
    assert repository.saved[0][2] == ()
    assert repository.saved[0][3] == ()
    assert repository.saved[0][4].state == "unknown"


def test_runner_stops_after_sigterm_request():
    class OneShotSampler:
        def __init__(self):
            self.calls = 0

        def sample_once(self):
            self.calls += 1
            return SampleResult(NOW, 1, 1, 0, 0, 1, 0)

    sampler = OneShotSampler()
    runner = TelemetryRunner(sampler, TelemetryConfig(interval_seconds=15), sleeper=lambda _seconds: runner.request_stop())
    runner.run()
    assert sampler.calls == 1


def test_sampler_contains_no_infrastructure_clients_or_mutations():
    source = "\n".join(
        Path(path).read_text()
        for path in (
            "src/aipm/services/telemetry/sampler.py",
            "src/aipm/services/telemetry/runner.py",
            "src/aipm/mappers/telemetry_history.py",
        )
    ).lower()
    for forbidden in ("psutil", "dockerprovider", "git fetch", "git pull", "systemctl", "cloudflare", "docker restart", "compose up", "compose down"):
        assert forbidden not in source
