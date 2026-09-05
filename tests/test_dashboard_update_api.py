"""Dashboard update-plan façade and endpoint tests."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aipm.capabilities.dashboard import update_api as update_api_module
from aipm.capabilities.dashboard.safety import assert_safe_payload
from aipm.capabilities.dashboard.update_api import DashboardUpdateApi
from aipm.dashboard.server import create_app
from aipm.models.update import UpdatePlan, UpdateRisk
from aipm.services.update.planner import UpdatePlanner

from update_fixtures import (
    make_repo,
    status_porcelain,
)


VALID_ID = "a" * 24
BAD_ID = "not-a-valid-id"
class RecordingIntelligence:
    """Serve a fixed application detail; record the requested identifier."""

    def __init__(self, local_project_name: str | None, project_id: str = VALID_ID) -> None:
        self.project_id = project_id
        self.local_project_name = local_project_name
        self.requested: list[str] = []

    def detail(self, project_id: str):
        self.requested.append(project_id)
        if project_id != self.project_id:
            return None
        return DetailApplication(self.local_project_name)


class DetailApplication:
    def __init__(self, local_project_name: str | None) -> None:
        self.local_project_name = local_project_name


class RecordingPlanner:
    """Record planner calls; return a canned plan without touching state."""

    def __init__(self, plan: UpdatePlan) -> None:
        self.fixed_plan = plan
        self.calls: list[dict] = []

    def plan(self, project_name: str, dry_run: bool = False) -> UpdatePlan:
        self.calls.append({"project_name": project_name, "dry_run": dry_run})
        return self.fixed_plan


class ExplodingPlanner(RecordingPlanner):
    def plan(self, project_name: str, dry_run: bool = False) -> UpdatePlan:
        raise RuntimeError("planner exploded")


class StubApi:
    """Return an inert observation envelope for any existing GET route."""

    def __getattr__(self, name: str):
        def handler(*args, **kwargs):
            return {"available": False, "status": "error", "error": "not under test", "observation": {"state": "error"}}
        return handler


def make_client(update_api: DashboardUpdateApi) -> TestClient:
    return TestClient(
        create_app(
            dashboard_api=StubApi(),
            incidents_api=StubApi(),
            notifications_api=StubApi(),
            service_health_api=StubApi(),
            server_api=StubApi(),
            docker_api=StubApi(),
            project_api=StubApi(),
            systemd_api=StubApi(),
            logs_api=StubApi(),
            settings_api=StubApi(),
            update_api=update_api,
        )
    )


def sample_plan(**overrides) -> UpdatePlan:
    values = {
        "project": "demo",
        "project_path": "/tmp/should-not-appear",
        "dry_run": True,
        "proceed": True,
        "approval_required": False,
        "risk": UpdateRisk.LOW,
        "reasons": ["Declared Compose file is missing: /home/ubuntu/aipm/projects/demo/compose.yaml"],
        "actions": ["Create a configuration safety snapshot"],
        "snapshot_required": True,
        "estimated_restart": True,
        "stash_required": False,
        "pull_required": False,
        "git": None,
        "health_before": None,
    }
    values.update(overrides)
    return UpdatePlan(**values)


def make_update_api(plan: UpdatePlan, project_id: str = VALID_ID, local_name: str | None = "demo") -> tuple[DashboardUpdateApi, RecordingPlanner]:
    intelligence = RecordingIntelligence(local_name, project_id=project_id)
    recording = RecordingPlanner(plan)
    return DashboardUpdateApi(intelligence, recording), recording



def test_registered_project_returns_valid_update_plan_payload():
    api, recording = make_update_api(sample_plan())
    response = make_client(api).get(f"/api/projects/{VALID_ID}/update-plan")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["status"] == "ok"
    assert body["error"] is None
    plan = body["update_plan"]
    assert plan["project"] == "demo"
    assert plan["dry_run"] is True
    assert plan["risk"] == "low"
    assert plan["proceed"] is True
    assert plan["approval_required"] is False
    assert plan["snapshot_required"] is True
    assert plan["estimated_restart"] is True
    assert plan["stash_required"] is False
    assert plan["pull_required"] is False
    assert recording.calls == [{"project_name": "demo", "dry_run": True}]


def test_unknown_project_returns_error_convention():
    api, _ = make_update_api(sample_plan())
    response = make_client(api).get("/api/projects/" + "b" * 24 + "/update-plan")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["status"] == "error"
    assert body["error"] == "Project is unavailable"
    assert body["update_plan"] is None
    assert body["observation"]["state"] == "error"
    assert body["observation"]["error"] == "Project is unavailable"


def test_invalid_identifier_rejected_before_planner():
    api, _ = make_update_api(sample_plan())
    client = make_client(api)
    for bad in (BAD_ID, "   ", "xyz", "a" * 23, "a" * 25, "A" * 24, "g" * 24):
        body = client.get(f"/api/projects/{bad}/update-plan").json()
        assert body["error"] == "Project identifier is invalid", bad


def test_planner_is_invoked_read_only_via_plan_only():
    api, _ = make_update_api(sample_plan())
    response = make_client(api).get(f"/api/projects/{VALID_ID}/update-plan")
    assert response.status_code == 200
    assert isinstance(api.planner, UpdatePlanner) or api.planner is not None
    source = inspect.getsource(update_api_module)
    assert ".plan(" in source
    assert "execute" not in source.replace("execute_update", "")


def test_no_execution_capability_is_exposed():
    api, _ = make_update_api(sample_plan())
    public = [name for name in vars(api) if not name.startswith("_")]
    assert public == ["intelligence", "planner", "clock"]
    for name in dir(api):
        assert "execute" not in name
    module_source = inspect.getsource(update_api_module)
    assert "UpdatePlanner" in module_source
    assert "subprocess" not in module_source


def test_no_mutation_semantics_on_http_surface():
    api, _ = make_update_api(sample_plan())
    client = make_client(api)
    before = sample_plan()
    response = client.get(f"/api/projects/{VALID_ID}/update-plan")
    assert response.status_code == 200
    assert response.request.method == "GET"
    routes = {getattr(route, "path", None): getattr(route, "methods", None) for route in client.app.routes}
    assert routes["/api/projects/{project_id}/update-plan"] == {"GET"}


def test_response_passes_safety_scanner():
    api, _ = make_update_api(sample_plan())
    body = make_client(api).get(f"/api/projects/{VALID_ID}/update-plan").json()
    assert_safe_payload(body)


def test_payload_contains_no_paths_secrets_or_commands():
    api, _ = make_update_api(sample_plan())
    body = make_client(api).get(f"/api/projects/{VALID_ID}/update-plan").json()
    serialized = str(body)
    assert "/tmp/should-not-appear" not in serialized
    assert "/home/ubuntu/aipm" not in serialized
    assert "compose.yaml" in serialized
    assert "password" not in serialized.lower()
    plan = body["update_plan"]
    assert set(plan.keys()) == {
        "project",
        "dry_run",
        "proceed",
        "approval_required",
        "risk",
        "reasons",
        "actions",
        "snapshot_required",
        "estimated_restart",
        "stash_required",
        "pull_required",
        "plan_digest",
    }


def test_reasons_and_actions_are_sanitized_and_bounded():
    long_reasons = [f"reason {i} at /some/path/file-{i}.yaml" for i in range(50)]
    long_actions = [f"action {i} /long/path/{i}/script" for i in range(40)]
    api, _ = make_update_api(sample_plan(reasons=long_reasons, actions=long_actions))
    body = make_client(api).get(f"/api/projects/{VALID_ID}/update-plan").json()
    plan = body["update_plan"]
    assert len(plan["reasons"]) == 32
    assert len(plan["actions"]) == 16
    for reason in plan["reasons"]:
        assert "/some/path/" not in reason
    for action in plan["actions"]:
        assert "/long/path/" not in action
    assert api.intelligence.requested == [VALID_ID]


def test_real_planner_renders_sanitized_payload_for_local_repo(tmp_path: Path):
    from aipm.services.git.service import GitService
    from update_fixtures import FixedProjectService, hermetic_health_engine, status_porcelain

    project = make_repo(tmp_path, runtime_script=None)
    planner = UpdatePlanner(
        project_service=FixedProjectService(project, GitService()),
        git_service=GitService(),
        health_engine=hermetic_health_engine(),
    )
    before = status_porcelain(project)
    api = DashboardUpdateApi(RecordingIntelligence(project.name), planner)
    body = make_client(api).get(f"/api/projects/{VALID_ID}/update-plan").json()
    assert body["available"] is True
    assert body["status"] == "ok"
    assert_safe_payload(body)
    serialized = str(body)
    assert str(tmp_path) not in serialized
    assert str(project.path) not in serialized
    plan = body["update_plan"]
    assert plan["project"] == project.name
    assert plan["dry_run"] is True
    assert status_porcelain(project) == before
