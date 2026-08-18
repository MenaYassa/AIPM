from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from aipm.capabilities.dashboard.project_api import DashboardProjectApi
from aipm.dashboard.server import create_app
from aipm.models.project import Project, ProjectCapabilities
from aipm.models.project_intelligence import ProjectHealthStatus
from aipm.services.project.intelligence import ProjectIntelligenceService


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src" / "aipm" / "dashboard" / "static"


@dataclass
class FakeDetail:
    id: str
    name: str
    project_key: str | None
    service_name: str | None
    image: str
    state: str
    health: str | None
    restart_count: int = 0
    resources: object | None = None
    ports: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()
    mount_kinds: tuple[str, ...] = ()
    started_at: str | None = None


class FakeProjectService:
    def __init__(self, projects: list[Project]) -> None:
        self.projects = projects
        self.app = SimpleNamespace(config=SimpleNamespace(discovery=SimpleNamespace(search_paths=["/srv/projects"])))

    def discover(self):
        return self.projects


class FakeTelemetry:
    def __init__(self, details: list[FakeDetail]) -> None:
        self.details = details

    def fast_snapshot(self, *, now):
        items = [SimpleNamespace(container=item, resources=None) for item in self.details]
        return SimpleNamespace(containers=items, state_sampled_at=now)


class FakeObservation:
    def containers(self):
        return []


def service_for(projects: list[Project], details: list[FakeDetail], compose_service=None) -> ProjectIntelligenceService:
    return ProjectIntelligenceService(FakeProjectService(projects), FakeObservation(), FakeTelemetry(details), compose_service=compose_service)


def test_grouping_is_deterministic_and_preserves_runtime_only_and_ungrouped() -> None:
    projects = [Project(name="ai-platform", path="/srv/projects/ai-platform", capabilities=ProjectCapabilities(has_compose=True), compose_files=["/srv/projects/ai-platform/compose.yml"])]
    details = [
        FakeDetail("1" * 12, "ollama", "ai-platform", "ollama", "ollama:latest", "running", "healthy"),
        FakeDetail("2" * 12, "orphan", "unknown-stack", "orphan", "orphan:latest", "running", None),
        FakeDetail("3" * 12, "unlabeled", None, None, "busybox:latest", "exited", None),
    ]
    inventory = service_for(projects, details).inventory()
    names = [item.display_name for item in inventory.projects]
    assert names == sorted(names, key=str.lower)
    matched = next(item for item in inventory.projects if item.display_name == "ai-platform")
    assert matched.confidence.value == "exact"
    assert len(matched.components) == 1
    runtime_only = next(item for item in inventory.projects if item.display_name == "unknown-stack")
    assert runtime_only.source.value == "runtime_group"
    ungrouped = next(item for item in inventory.projects if item.source.value == "ungrouped")
    assert ungrouped.health.status in {ProjectHealthStatus.UNKNOWN, ProjectHealthStatus.YELLOW, ProjectHealthStatus.RED}


def test_filtered_candidates_exclude_known_dependency_paths_but_keep_real_repositories() -> None:
    projects = [
        Project(name=".nuget", path="/srv/.nuget"),
        Project(name="flutter", path="/srv/flutter"),
        Project(name="aipm", path="/srv/aipm", capabilities=ProjectCapabilities(has_git=True)),
    ]
    inventory = service_for(projects, []).inventory(scope="applications")
    assert [item.display_name for item in inventory.projects] == []
    assert {item.display_name for item in inventory.filtered_candidates} == {".nuget", "flutter"}
    assert all(item.association_role.value == "filtered_candidate" for item in inventory.filtered_candidates)
    assert all(item.association_explanation == "Excluded because this path does not appear to be an application root." for item in inventory.filtered_candidates)
    assert [item.display_name for item in inventory.local_candidates] == ["aipm"]
    assert inventory.local_candidates[0].association_explanation == "Discovered source project without runtime association."
    filtered = service_for(projects, []).inventory(scope="filtered")
    assert {item.display_name for item in filtered.projects} == {".nuget", "flutter"}


def test_runtime_first_scope_separates_local_candidates_without_dropping_them() -> None:
    project = Project(name="local-only", path="/srv/local-only", capabilities=ProjectCapabilities(has_git=True))
    details = [FakeDetail("1" * 12, "runtime", "runtime-stack", "runtime", "app:latest", "running", "healthy")]
    service = service_for([project], details)
    applications = service.inventory(scope="applications")
    assert [item.association_role.value for item in applications.projects] == ["runtime_only"]
    assert [item.display_name for item in applications.local_candidates] == ["local-only"]
    local = service.inventory(scope="local")
    assert [item.display_name for item in local.projects] == ["local-only"]
    assert local.inventory_scope.value == "local"


def test_compose_name_identity_is_required_for_exact_association(tmp_path) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("name: canonical-platform\nservices:\n  app:\n    image: app:latest\n", encoding="utf-8")
    project = Project(name="directory-name", path=str(tmp_path), capabilities=ProjectCapabilities(has_compose=True), compose_files=[str(compose_file)])
    details = [FakeDetail("1" * 12, "app", "canonical-platform", "app", "app:latest", "running", "healthy")]
    associated = service_for([project], details).inventory(scope="applications").projects[0]
    assert associated.association_role.value == "associated_local"
    assert associated.confidence.value == "exact"
    assert associated.local_project_name == "directory-name"

    mismatch = service_for([project], [FakeDetail("2" * 12, "app", "other-platform", "app", "app:latest", "running", "healthy")]).inventory(scope="applications")
    assert mismatch.projects[0].association_role.value == "runtime_only"
    assert mismatch.local_candidates[0].display_name == "directory-name"


def test_compose_status_is_the_only_compose_operation_used() -> None:
    class ReadOnlyCompose:
        def __init__(self):
            self.calls = 0

        def status(self, project):
            self.calls += 1
            return SimpleNamespace(running=1, stopped=0, restarting=0, unhealthy=0)

        def up(self, *args, **kwargs):
            raise AssertionError("Compose mutation must never be reached")

        def down(self, *args, **kwargs):
            raise AssertionError("Compose mutation must never be reached")

    compose = ReadOnlyCompose()
    project = Project(name="platform", path="/srv/platform", capabilities=ProjectCapabilities(has_compose=True), compose_files=["/srv/platform/compose.yml"])
    inventory = service_for([project], [FakeDetail("1" * 12, "service", "platform", "service", "app:latest", "running", "healthy")], compose).inventory()
    assert compose.calls == 1
    assert inventory.projects[0].compose["status"] == "observed"
    assert inventory.projects[0].compose["running"] == 1


def test_health_requires_evidence_and_missing_health_checks_are_warning() -> None:
    projects = [Project(name="platform", path="/srv/platform", capabilities=ProjectCapabilities(has_compose=True), compose_files=["/srv/platform/compose.yml"])]
    details = [FakeDetail("1" * 12, "service", "platform", "service", "app:latest", "running", None)]
    health = service_for(projects, details).inventory().projects[0].health
    assert health.status is ProjectHealthStatus.YELLOW
    assert health.counts["missing_health_check"] == 1
    assert any(item.code == "MISSING_HEALTH_CHECKS" and item.message == "1 containers missing health checks" for item in health.evidence)


def test_health_evidence_aggregates_repeated_missing_checks() -> None:
    project = Project(name="platform", path="/srv/platform", capabilities=ProjectCapabilities(has_compose=True), compose_files=["/srv/platform/compose.yml"])
    details = [FakeDetail(str(index) * 12, f"service-{index}", "platform", f"service-{index}", "app:latest", "running", None) for index in range(1, 9)]
    health = service_for([project], details).inventory().projects[0].health
    assert health.counts["running"] == 8
    assert health.counts["healthy"] == 0
    assert health.counts["missing_health_check"] == 8
    assert [item.code for item in health.evidence].count("MISSING_HEALTH_CHECKS") == 1
    assert any(item.message == "8 containers missing health checks" for item in health.evidence)
    assert not any(item.code == "MISSING_HEALTH_CHECK" for item in health.evidence)


def test_stopped_or_unhealthy_components_are_red() -> None:
    projects = [Project(name="platform", path="/srv/platform", capabilities=ProjectCapabilities(has_compose=True), compose_files=["/srv/platform/compose.yml"])]
    details = [FakeDetail("1" * 12, "service", "platform", "service", "app:latest", "running", "unhealthy")]
    health = service_for(projects, details).inventory().projects[0].health
    assert health.status is ProjectHealthStatus.RED


def test_project_api_enforces_bounds_and_safe_invalid_identifiers() -> None:
    projects = [Project(name="platform", path="/srv/platform")]
    intelligence = service_for(projects, []).inventory
    api = DashboardProjectApi(service_for(projects, []))
    response = api.projects(limit=999999, status="invalid")
    assert response["available"] is False
    assert response["error"] == "Project status filter is invalid"
    invalid_scope = api.projects(scope="filesystem-everywhere")
    assert invalid_scope["available"] is False
    assert invalid_scope["error"] == "Project inventory scope is invalid"
    invalid = api.project("../../etc/passwd")
    assert invalid["available"] is False
    assert "etc/passwd" not in str(invalid)


def test_project_api_routes_are_get_only_and_safe() -> None:
    class FakeApi:
        def projects(self, **kwargs):
            return {"available": True, "status": "ok", "projects": [], "observation": {"state": "fresh"}}

        def project(self, project_id):
            return {"available": False, "status": "error", "error": "Project is unavailable"}

        def containers(self, project_id):
            return {"available": False, "status": "error", "error": "Project is unavailable", "containers": []}

        def health(self, project_id):
            return {"available": False, "status": "error", "error": "Project is unavailable"}

    app = create_app(project_api=FakeApi())
    client = TestClient(app)
    assert client.get("/api/projects?scope=applications&limit=200").status_code == 200
    assert client.get("/api/projects/000000000000000000000000").status_code == 200
    assert client.get("/api/projects/000000000000000000000000/containers").status_code == 200
    assert client.get("/api/projects/000000000000000000000000/health").status_code == 200
    assert client.post("/api/projects").status_code in {405, 404}
    assert client.get("/api/projects/000000000000000000000000").json()["error"] == "Project is unavailable"


def test_project_frontend_uses_static_module_and_inventory_contract() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    module = (STATIC / "mission-control-projects.js").read_text(encoding="utf-8")
    assert '/static/mission-control-projects.js' in html
    assert 'data-view="projects"' in html
    for marker in ("Application Inventory", "Runtime Groups", "Local Projects", "Filtered Candidates", "project-secondary-section"):
        assert marker in html
    for marker in ("projectCards", "projectDetail", "projectInventoryState", "scheduler.register('projects'"):
        assert marker in html
    for marker in ("/api/projects?scope=applications&limit=200", "/api/projects/", "/containers", "/health", "createProjectController", "runtimeGroupCards", "localProjectCards", "filteredCandidateCards", "association_role", "filtered_candidate", "association_explanation", "healthCounts", "healthEvidenceHtml", "Health evidence", "Running containers:", "Healthy containers:", "Missing health checks:", "Unknown:"):
        assert marker in module
    assert 'method="post"' not in module.lower()
    assert "fetch(" in module
    assert "docker start" not in module.lower()
    assert "git fetch" not in module.lower()


def test_project_source_does_not_expose_raw_provider_or_mutation_surface() -> None:
    source = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "src/aipm/services/project/intelligence.py",
            "src/aipm/capabilities/dashboard/project_api.py",
            "src/aipm/mappers/project_intelligence.py",
        )
    ).lower()
    assert "subprocess" not in source
    assert "docker start" not in source
    assert "docker stop" not in source
    assert "fetch()" not in source
    assert "pull()" not in source
    assert "os.environ" not in source
    assert "traceback" not in source
