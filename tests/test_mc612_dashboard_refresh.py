"""MC-6.12: dashboard project freshness follows persisted telemetry samples without restarts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aipm.capabilities.dashboard.api import DashboardApi
from aipm.models.history import ProjectHistoryPoint
from aipm.services.telemetry.project import ProjectTelemetryService

UTC = timezone.utc


class _FakeLogger:
    def __init__(self):
        self.exceptions: list[str] = []

    def exception(self, message, exc_info=None):
        self.exceptions.append(message)


class _FakeTelemetry:
    def __init__(self):
        self.logger = _FakeLogger()

    def fast_snapshot(self):
        return object()


class _PointProjectService:
    class _App:
        class _Config:
            class _Discovery:
                search_paths = ["/srv"]

            discovery = _Discovery()

        config = _Config()

    def __init__(self):
        self.app = self._App()


def _point(at: datetime, name: str, branch: str):
    return ProjectHistoryPoint(at, name, f"/srv/{name}", branch, True, False, False, 0, 0)


def test_overview_refreshes_project_history_then_throttles():
    calls: list[float] = []
    now = [100.0]
    api = DashboardApi(
        telemetry=_FakeTelemetry(),
        mapper=SimpleNamespace(to_response=lambda snapshot: {"snapshot": snapshot}),
        project_history_refresher=lambda: calls.append(now[0]),
        project_history_refresh_interval_seconds=60.0,
        clock=lambda: now[0],
    )

    api.overview()
    api.overview()
    assert len(calls) == 1

    now[0] += 61.0
    api.overview()
    assert len(calls) == 2


def test_overview_swallows_history_refresh_failure():
    attempts: list[int] = []

    def failing_refresh():
        attempts.append(1)
        raise RuntimeError("history unavailable")

    telemetry = _FakeTelemetry()
    api = DashboardApi(
        telemetry=telemetry,
        mapper=SimpleNamespace(to_response=lambda snapshot: {"snapshot": snapshot}),
        project_history_refresher=failing_refresh,
        clock=lambda: 0.0,
    )

    response = api.overview()

    assert "snapshot" in response
    assert telemetry.logger.exceptions == ["Project telemetry history refresh unavailable"]

    api.overview()
    assert len(attempts) == 1


class _FakeRepository:
    instances: list["_FakeRepository"] = []

    def __init__(self, database_path, *, read_only=False):
        self.database_path = database_path
        self.read_only = read_only
        self.closed = False
        _FakeRepository.instances.append(self)

    def get_latest_project_samples(self):
        return [_point(datetime.now(UTC), "alpha", "main")]

    def get_latest_resource_samples(self):
        return []

    def close(self):
        self.closed = True


class _StubTelemetryConfig:
    def __init__(self, database_path: str, enabled: bool):
        self.enabled = enabled
        self.database_path = database_path
        self.resource_stale_after_seconds = 180
        self.project_interval_seconds = 60


class _StubConfig:
    def __init__(self, database_path: str, enabled: bool):
        self.telemetry = _StubTelemetryConfig(database_path, enabled)
        self.discovery = SimpleNamespace(search_paths=["/srv"])


class _StubApplication:
    def __init__(self, database_path: str, enabled: bool = True):
        self.system = object()
        self.docker = object()
        self.logger = _FakeLogger()
        self.config = _StubConfig(database_path, enabled)


def test_from_application_wires_history_refresher(monkeypatch, tmp_path):
    _FakeRepository.instances = []
    monkeypatch.setattr("aipm.capabilities.dashboard.api.SQLiteHistoryRepository", _FakeRepository)
    application = _StubApplication(str(tmp_path / "telemetry.db"))

    api = DashboardApi.from_application(application)

    assert api._project_history_refresher is not None
    assert len(_FakeRepository.instances) == 1
    snapshot = api.telemetry.projects.cached_snapshot()
    assert snapshot.available is True
    assert len(snapshot.projects) == 1

    api._project_history_refresher()

    assert len(_FakeRepository.instances) == 2
    assert all(repository.closed for repository in _FakeRepository.instances)


def test_from_application_skips_refresher_when_telemetry_disabled(monkeypatch, tmp_path):
    _FakeRepository.instances = []
    monkeypatch.setattr("aipm.capabilities.dashboard.api.SQLiteHistoryRepository", _FakeRepository)
    application = _StubApplication(str(tmp_path / "telemetry.db"), enabled=False)

    api = DashboardApi.from_application(application)

    assert api._project_history_refresher is None
    assert _FakeRepository.instances == []


def test_hydrated_projects_keep_freshness_advancing_with_rows():
    service = ProjectTelemetryService(_PointProjectService(), stale_after_seconds=180)
    first = datetime.now(UTC) - timedelta(seconds=30)
    service.hydrate_projects([_point(first, "alpha", "main")])
    assert service.cached_snapshot().freshness.status.value == "fresh"

    later = datetime.now(UTC)
    service.hydrate_projects([_point(later, "alpha", "main")])
    refreshed = service.cached_snapshot()
    assert refreshed.freshness.status.value == "fresh"
    assert refreshed.freshness.age_seconds is not None and refreshed.freshness.age_seconds < 30