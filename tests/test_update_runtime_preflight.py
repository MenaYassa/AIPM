"""Runtime-action coverage: static preflight of the Compose runtime in the planner.

Read-only and deterministic: every test parses compose files from tmp_path or
uses deterministic fakes; no Docker daemon, no subprocess, no network, and no
dependence on /home/ubuntu or any real production repository.
"""
from __future__ import annotations

from pathlib import Path

from aipm.models.health import HealthState
from aipm.models.health_report import HealthReport
from aipm.models.project import Project, ProjectCapabilities
from aipm.models.update import UpdateRisk
from aipm.services.update.planner import UpdatePlanner


class FixedProjectService:
    def __init__(self, project: Project):
        self.project = project

    def get_project(self, name: str) -> Project:
        assert name == self.project.name
        return self.project


class FixedGitService:
    def prepare_update(self, project):
        raise AssertionError("prepare_update must not run for projects without Git capability")

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FixedHealthEngine:
    def analyze(self, project):
        return HealthReport(project="demo", score=100, state=HealthState.HEALTHY)


def planner_for(project: Project) -> UpdatePlanner:
    return UpdatePlanner(
        project_service=FixedProjectService(project),
        git_service=FixedGitService(),
        health_engine=FixedHealthEngine(),
    )


def compose_project(tmp_path: Path, files: dict[str, str]) -> Project:
    project_path = tmp_path / "demo"
    project_path.mkdir(exist_ok=True)
    compose_files = []
    for filename, content in files.items():
        target = project_path / filename
        target.write_text(content, encoding="utf-8")
        compose_files.append(str(target))
    return Project(
        name="demo",
        path=str(project_path),
        capabilities=ProjectCapabilities(has_compose=True),
        compose_files=compose_files,
    )


def test_planner_lists_compose_services_in_the_runtime_action(tmp_path: Path):
    project = compose_project(
        tmp_path,
        {"compose.yaml": "services:\n  web:\n    image: nginx\n  db:\n    image: postgres\n"},
    )
    plan = planner_for(project).plan("demo", dry_run=True)
    assert plan.proceed is True
    runtime = [action for action in plan.actions if "Compose services" in action]
    assert runtime == ["Rebuild and start the project Compose services (db, web)"]


def test_planner_blocks_when_compose_file_is_missing(tmp_path: Path):
    project = compose_project(tmp_path, {})
    project.compose_files = [str(Path(project.path) / "compose.yaml")]  # declared but never written
    plan = planner_for(project).plan("demo", dry_run=True)
    assert plan.proceed is False
    assert plan.risk is UpdateRisk.BLOCKED
    assert any("Declared Compose file is missing" in reason for reason in plan.reasons)
    assert any("could not be analyzed" in reason for reason in plan.reasons)


def test_planner_blocks_when_compose_file_is_invalid_yaml(tmp_path: Path):
    project = compose_project(tmp_path, {"compose.yaml": "services: [unclosed\n"})
    plan = planner_for(project).plan("demo", dry_run=True)
    assert plan.proceed is False
    assert plan.risk is UpdateRisk.BLOCKED
    assert any("not readable YAML" in reason for reason in plan.reasons)
    assert any("could not be analyzed" in reason for reason in plan.reasons)


def test_planner_blocks_when_compose_document_is_not_a_mapping(tmp_path: Path):
    project = compose_project(tmp_path, {"compose.yaml": "- just\n- a\n- list\n"})
    plan = planner_for(project).plan("demo", dry_run=True)
    assert plan.proceed is False
    assert plan.risk is UpdateRisk.BLOCKED
    assert any("not a Compose document" in reason for reason in plan.reasons)


def test_planner_notes_when_no_services_are_declared(tmp_path: Path):
    project = compose_project(tmp_path, {"compose.yaml": "services: {}\n"})
    plan = planner_for(project).plan("demo", dry_run=True)
    assert plan.proceed is True
    assert any("no services declared" in action for action in plan.actions)
    assert any("define no services" in reason for reason in plan.reasons)


def test_planner_aggregates_services_across_multiple_compose_files(tmp_path: Path):
    project = compose_project(
        tmp_path,
        {
            "compose.yaml": "services:\n  web:\n    image: nginx\n",
            "compose.override.yaml": "services:\n  worker:\n    image: worker\n",
        },
    )
    plan = planner_for(project).plan("demo", dry_run=True)
    assert plan.proceed is True
    runtime = [action for action in plan.actions if "Compose services" in action]
    assert runtime == ["Rebuild and start the project Compose services (web, worker)"]


def test_planner_blocks_compose_capable_project_without_declared_files(tmp_path: Path):
    project = compose_project(tmp_path, {})
    project.compose_files = []
    plan = planner_for(project).plan("demo", dry_run=True)
    assert plan.proceed is False
    assert plan.risk is UpdateRisk.BLOCKED
    assert any("declares no Compose files" in reason for reason in plan.reasons)


def test_planner_compose_preflight_never_mutates_the_project(tmp_path: Path):
    project = compose_project(tmp_path, {"compose.yaml": "services:\n  web:\n    image: nginx\n"})
    before = {path: path.read_text(encoding="utf-8") for path in Path(project.path).iterdir() if path.is_file()}
    planner_for(project).plan("demo", dry_run=True)
    after = {path: path.read_text(encoding="utf-8") for path in Path(project.path).iterdir() if path.is_file()}
    assert before == after
