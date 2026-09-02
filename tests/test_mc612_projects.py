"""MC-6.12: dashboard project hydration from telemetry history (read-only)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aipm.models.history import ProjectHistoryPoint, SampleRunRecord
from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository
from aipm.services.telemetry.project import ProjectTelemetryService

UTC = timezone.utc


class _FakeProjectService:
    class _App:
        class _Config:
            class _Discovery:
                search_paths = ["/srv"]

            discovery = _Discovery()

        config = _Config()

    def __init__(self):
        self.app = self._App()


def _point(at: datetime, name: str, branch: str | None, dirty: bool | None, ahead: int, behind: int, has_git: bool = True, has_compose: bool = False):
    return ProjectHistoryPoint(at, name, f"/srv/{name}", branch, has_git, has_compose, dirty, ahead, behind)


def test_get_latest_project_samples_returns_latest_per_project(tmp_path):
    repository = SQLiteHistoryRepository(tmp_path / "telemetry.db")
    at = datetime.now(UTC)
    run = SampleRunRecord(at, True, True, True, "healthy", duration_ms=1)
    later = at + timedelta(seconds=30)
    repository.save_sample(
        run,
        None,
        [],
        [_point(at, "alpha", "main", False, 0, 0), _point(at, "beta", "dev", True, 2, 1)],
        None,
    )
    repository.save_sample(
        SampleRunRecord(later, True, True, True, "healthy", duration_ms=1),
        None,
        [],
        [_point(later, "alpha", "feature", True, 3, 0)],
        None,
    )

    samples = repository.get_latest_project_samples()

    by_name = {point.name: point for point in samples}
    assert set(by_name) == {"alpha", "beta"}
    assert by_name["alpha"].branch == "feature"
    assert by_name["alpha"].dirty is True
    assert by_name["beta"].branch == "dev"
    repository.close()


def test_hydrate_projects_seeds_cache_and_degrades_to_stale():
    service = ProjectTelemetryService(_FakeProjectService(), stale_after_seconds=180)
    sampled_at = datetime.now(UTC) - timedelta(seconds=600)

    service.hydrate_projects([_point(sampled_at, "alpha", "main", False, 0, 0)])

    snapshot = service.cached_snapshot()
    assert snapshot.available is True
    assert len(snapshot.projects) == 1
    project = snapshot.projects[0].project
    assert project.name == "alpha"
    assert project.path == "/srv/alpha"
    assert project.capabilities.has_git is True
    assert project.git is not None
    assert project.git.branch == "main"
    assert project.git.dirty is False
    assert snapshot.freshness.status.value == "stale"


def test_hydrate_projects_empty_leaves_never_sampled():
    service = ProjectTelemetryService(_FakeProjectService(), stale_after_seconds=180)

    service.hydrate_projects([])

    snapshot = service.cached_snapshot()
    assert snapshot.status == "unknown"
    assert snapshot.freshness.status.value == "never_sampled"


def test_hydrate_projects_without_branch_has_no_git_model():
    service = ProjectTelemetryService(_FakeProjectService(), stale_after_seconds=180)

    service.hydrate_projects([_point(datetime.now(UTC), "plain", None, None, 0, 0, has_git=False)])

    project = service.cached_snapshot().projects[0].project
    assert project.git is None
    assert project.capabilities.has_git is False


def test_from_application_wires_project_hydration(monkeypatch, tmp_path):
    from aipm.capabilities.dashboard import api as dashboard_api_module

    hydrated: dict[str, object] = {}

    class FakeRepository:
        def __init__(self, path, read_only=False):
            pass

        def get_latest_resource_samples(self):
            return []

        def get_latest_project_samples(self):
            hydrated["projects"] = True
            return [_point(datetime.now(UTC), "alpha", "main", False, 0, 0)]

        def close(self):
            pass

    class FakeProjectsService:
        def hydrate_projects(self, points):
            hydrated["called_with"] = list(points)

    class FakeTelemetry:
        docker = type("D", (), {"hydrate_resources": staticmethod(lambda points: None)})()
        projects = FakeProjectsService()

    class FakeConfig:
        class telemetry:
            enabled = True
            database_path = tmp_path / "telemetry.db"
            resource_stale_after_seconds = 180
            project_interval_seconds = 60

        logging = type("L", (), {"file": "/tmp/unused.log"})()

    class FakeApplication:
        config = FakeConfig()
        logger = None

    monkeypatch.setattr(dashboard_api_module, "SQLiteHistoryRepository", FakeRepository)
    monkeypatch.setattr(dashboard_api_module, "ProjectTelemetryService", lambda **kwargs: FakeProjectsService())
    monkeypatch.setattr(dashboard_api_module, "DashboardTelemetryService", lambda **kwargs: FakeTelemetry())
    monkeypatch.setattr(dashboard_api_module, "ProjectService", lambda app: None)
    monkeypatch.setattr(dashboard_api_module, "HostTelemetryService", lambda **kwargs: None)
    monkeypatch.setattr(dashboard_api_module, "DockerTelemetryService", lambda **kwargs: None)
    monkeypatch.setattr(dashboard_api_module, "TunnelTelemetryService", lambda **kwargs: None)
    monkeypatch.setattr(dashboard_api_module, "handbook_routes", lambda: None)

    class FakeSystem:
        pass

    class FakeDocker:
        pass

    FakeApplication.system = FakeSystem()
    FakeApplication.docker = FakeDocker()

    dashboard_api_module.DashboardApi.from_application(FakeApplication())

    assert hydrated["projects"] is True
    assert len(hydrated["called_with"]) == 1
