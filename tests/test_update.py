from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aipm.core.exceptions import UpdateError
from aipm.models.git_update_plan import GitUpdatePlan
from aipm.models.health import HealthState
from aipm.models.health_report import HealthReport
from aipm.models.project import Project, ProjectCapabilities
from aipm.models.update import UpdatePlan, UpdateRisk
from aipm.services.update.audit import AuditService
from aipm.services.update.engine import UpdateEngine
from aipm.services.update.planner import UpdatePlanner


class FakeProjectService:
    def __init__(self, project: Project):
        self.project = project

    def get_project(self, name: str) -> Project:
        assert name == self.project.name
        return self.project


class FakeGitService:
    def prepare_update(self, project):
        return GitUpdatePlan(
            proceed=True,
            stash_required=False,
            fetch_required=False,
            pull_required=False,
            review_required=False,
            rollback_required=False,
        )

    def repository(self, project):
        return project.git

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FakeHealthEngine:
    def __init__(self, report: HealthReport | None = None):
        self.report = report or HealthReport(project="demo", score=100, state=HealthState.HEALTHY)

    def analyze(self, project):
        return self.report


class FakeBackup:
    def __init__(self, root: Path):
        self.root = root
        self.created = False

    def create_snapshot(self, project):
        self.created = True
        path = self.root / "snapshot.tar.gz"
        path.write_bytes(b"snapshot")
        return SimpleNamespace(archive_path=path)


class FakeCompose:
    def __init__(self):
        self.calls = []

    def up(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def healthy_plan(project: Project, *, dry_run: bool, approval_required: bool = True) -> UpdatePlan:
    return UpdatePlan(
        project=project.name,
        project_path=project.path,
        dry_run=dry_run,
        proceed=True,
        approval_required=approval_required,
        risk=UpdateRisk.MEDIUM,
        actions=["Create a configuration safety snapshot", "Run the project start_services.py orchestration script"],
        estimated_restart=True,
        health_before=HealthReport(project=project.name, score=100, state=HealthState.HEALTHY),
    )


def test_update_planner_builds_side_effect_free_compose_plan(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    project = Project(
        name="demo",
        path=str(project_path),
        capabilities=ProjectCapabilities(has_compose=True),
        compose_files=[str(project_path / "compose.yaml")],
    )

    plan = UpdatePlanner(
        project_service=FakeProjectService(project),
        git_service=FakeGitService(),
        health_engine=FakeHealthEngine(),
    ).plan("demo", dry_run=True)

    assert plan.proceed is True
    assert plan.dry_run is True
    assert plan.approval_required is False
    assert plan.snapshot_required is True
    assert any("Compose" in action for action in plan.actions)


def test_dry_run_does_not_snapshot_or_execute(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    project = Project(name="demo", path=str(project_path))
    backup = FakeBackup(tmp_path / "backups")
    backup.root.mkdir()
    compose = FakeCompose()
    audit = AuditService(tmp_path / "audit")
    plan = healthy_plan(project, dry_run=True, approval_required=False)

    class FixedPlanner:
        def plan(self, name, dry_run=False):
            return plan

    engine = UpdateEngine(
        project_service=FakeProjectService(project),
        git_service=FakeGitService(),
        backup_engine=backup,
        compose_provider=compose,
        health_engine=FakeHealthEngine(),
        planner=FixedPlanner(),
        audit_service=audit,
    )

    result = engine.execute_update("demo", dry_run=True)

    assert result.outcome == "planned"
    assert backup.created is False
    assert compose.calls == []
    assert list((tmp_path / "audit").glob("*.json"))


def test_update_requires_explicit_approval(tmp_path: Path):
    project = Project(name="demo", path=str(tmp_path / "demo"))
    project_path = Path(project.path)
    project_path.mkdir()
    backup = FakeBackup(tmp_path / "backups")
    backup.root.mkdir()
    audit = AuditService(tmp_path / "audit")
    plan = healthy_plan(project, dry_run=False, approval_required=True)

    class FixedPlanner:
        def plan(self, name, dry_run=False):
            return plan

    engine = UpdateEngine(
        project_service=FakeProjectService(project),
        git_service=FakeGitService(),
        backup_engine=backup,
        compose_provider=FakeCompose(),
        health_engine=FakeHealthEngine(),
        planner=FixedPlanner(),
        audit_service=audit,
    )

    with pytest.raises(UpdateError, match="Explicit approval"):
        engine.execute_update("demo")

    assert backup.created is False
    assert list((tmp_path / "audit").glob("*.json"))


def test_approved_update_executes_custom_runtime_and_audits_success(tmp_path: Path):
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / "start_services.py").write_text("print('ok')\n", encoding="utf-8")
    project = Project(name="demo", path=str(project_path))
    backup = FakeBackup(tmp_path / "backups")
    backup.root.mkdir()
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    class FixedPlanner:
        def plan(self, name, dry_run=False):
            return healthy_plan(project, dry_run=False, approval_required=True)

    engine = UpdateEngine(
        project_service=FakeProjectService(project),
        git_service=FakeGitService(),
        backup_engine=backup,
        compose_provider=FakeCompose(),
        health_engine=FakeHealthEngine(),
        planner=FixedPlanner(),
        audit_service=AuditService(tmp_path / "audit"),
        runner=runner,
    )

    result = engine.execute_update("demo", approve=True)

    assert result.outcome == "success"
    assert backup.created is True
    assert calls
    assert calls[0][0][-1] == str(project_path / "start_services.py")
