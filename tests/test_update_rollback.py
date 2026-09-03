from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from aipm.models.health import HealthState
from aipm.models.health_report import HealthReport
from aipm.models.project import Project
from aipm.models.rollback import RestoreResult
from aipm.models.update import UpdateRisk, UpdatePlan
from aipm.services.backup.engine import BackupEngine
from aipm.services.project.service import ProjectService
from aipm.services.update.audit import AuditService
from aipm.services.update.engine import UpdateEngine
from aipm.services.update.rollback import RollbackManager


class FakeProjectService(ProjectService):
    def __init__(self, project: Project):
        self.project = project

    def get_project(self, name: str) -> Project:
        assert name == self.project.name
        return self.project


class FakeGitService:
    def prepare_update(self, project):
        class Plan:
            proceed = True
            approval_required = True
            stash_required = False
            pull_required = False
            git = None
            risk = UpdateRisk.MEDIUM
            reasons: list[str] = []
            actions: list[str] = []
            snapshot_required = True
            estimated_restart = True

        return Plan()


class FakeHealthEngine:
    def __init__(self):
        self.report = HealthReport(project="demo", score=100, state=HealthState.HEALTHY)

    def analyze(self, project):
        return self.report


class FakeCompose:
    def up(self, *args, **kwargs):
        raise AssertionError("compose must not be used when start_services.py exists")


class RecordingRollbackManager(RollbackManager):
    def __init__(self):
        super().__init__()
        self.calls: list[Path] = []

    def restore(self, archive_path, project):
        self.calls.append(Path(archive_path))
        return super().restore(archive_path, project)


class NoopRollbackManager(RollbackManager):
    def __init__(self, result: RestoreResult):
        super().__init__()
        self.result = result
        self.calls: list[Path] = []

    def restore(self, archive_path, project):
        self.calls.append(Path(archive_path))
        return self.result


def make_project(root: Path, name: str = "demo") -> Project:
    project_path = root / name
    (project_path / "app").mkdir(parents=True)
    (project_path / "config.txt").write_text("v1", encoding="utf-8")
    (project_path / "app" / "main.py").write_text("print('v1')\n", encoding="utf-8")
    (project_path / "start_services.py").write_text("print('ok')\n", encoding="utf-8")
    return Project(name=name, path=str(project_path))


def write_tar(path: Path, members: list[tuple[str, bytes]], symlinks: list[tuple[str, str]] = ()):
    with tarfile.open(path, "w:gz") as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for name, linkname in symlinks:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = linkname
            archive.addfile(info)


def healthy_plan(project: Project, *, dry_run: bool, approval_required: bool = True) -> UpdatePlan:
    return UpdatePlan(
        project=project.name,
        project_path=project.path,
        dry_run=dry_run,
        proceed=True,
        approval_required=approval_required,
        risk=UpdateRisk.MEDIUM,
        actions=["Create a configuration safety snapshot"],
        snapshot_required=True,
        estimated_restart=True,
        health_before=HealthReport(project=project.name, score=100, state=HealthState.HEALTHY),
    )


class FixedPlanner:
    def __init__(self, plan: UpdatePlan):
        self._plan = plan

    def plan(self, name, dry_run=False):
        return self._plan


def failing_runner(command, **kwargs):
    return SimpleNamespace(returncode=1, stdout="", stderr="boom")


def test_restore_plan_reports_files_without_writing(tmp_path: Path):
    project = make_project(tmp_path)
    backup = BackupEngine(tmp_path / "backups")
    archive = backup.create_snapshot(project)
    (Path(project.path) / "config.txt").write_text("v2", encoding="utf-8")

    result = RollbackManager().restore_plan(archive.archive_path, project)

    assert result.attempted is False
    assert result.success is True
    assert result.restored == ["app/main.py", "config.txt", "start_services.py"]
    assert result.left_in_place == []
    assert (Path(project.path) / "config.txt").read_text(encoding="utf-8") == "v2"


def test_restore_reverts_files_to_snapshot_state(tmp_path: Path):
    project = make_project(tmp_path)
    backup = BackupEngine(tmp_path / "backups")
    archive = backup.create_snapshot(project)
    (Path(project.path) / "config.txt").write_text("v2", encoding="utf-8")
    (Path(project.path) / "app" / "main.py").write_text("print('v2')\n", encoding="utf-8")

    result = RollbackManager().restore(archive.archive_path, project)

    assert result.success is True
    assert result.restored == ["app/main.py", "config.txt", "start_services.py"]
    assert (Path(project.path) / "config.txt").read_text(encoding="utf-8") == "v1"
    assert (Path(project.path) / "app" / "main.py").read_text(encoding="utf-8") == "print('v1')\n"


def test_restore_leaves_post_snapshot_files_in_place(tmp_path: Path):
    project = make_project(tmp_path)
    backup = BackupEngine(tmp_path / "backups")
    archive = backup.create_snapshot(project)
    (Path(project.path) / "extra.txt").write_text("new", encoding="utf-8")
    (Path(project.path) / "config.txt").write_text("v2", encoding="utf-8")

    result = RollbackManager().restore(archive.archive_path, project)

    assert result.success is True
    assert result.left_in_place == ["extra.txt"]
    assert (Path(project.path) / "extra.txt").read_text(encoding="utf-8") == "new"
    assert (Path(project.path) / "config.txt").read_text(encoding="utf-8") == "v1"


def test_restore_skips_out_of_scope_members(tmp_path: Path):
    project = make_project(tmp_path)
    archive_path = tmp_path / "backups" / "crafted.tar.gz"
    archive_path.parent.mkdir()
    write_tar(
        archive_path,
        members=[
            ("demo/config.txt", b"v1"),
            ("demo/.git/config", b"[core]"),
            ("demo/.venv/lib.py", b"x = 1"),
        ],
    )

    result = RollbackManager().restore(archive_path, project)

    assert result.success is True
    assert result.restored == ["config.txt"]
    assert sorted(result.skipped) == ["demo/.git/config", "demo/.venv/lib.py"]
    assert not (Path(project.path) / ".git").exists()
    assert (Path(project.path) / "config.txt").read_text(encoding="utf-8") == "v1"


def test_restore_refuses_traversal_members(tmp_path: Path):
    project = make_project(tmp_path)
    archive_path = tmp_path / "backups" / "malicious.tar.gz"
    archive_path.parent.mkdir()
    write_tar(
        archive_path,
        members=[
            ("demo/config.txt", b"v1"),
            ("demo/../../escaped.txt", b"pwned"),
        ],
    )

    result = RollbackManager().restore(archive_path, project)

    assert result.success is False
    assert "escapes the project directory" in result.error
    assert result.restored == []
    assert not (tmp_path / "escaped.txt").exists()
    assert (Path(project.path) / "config.txt").read_text(encoding="utf-8") == "v1"


def test_restore_refuses_symlink_members(tmp_path: Path):
    project = make_project(tmp_path)
    archive_path = tmp_path / "backups" / "symlink.tar.gz"
    archive_path.parent.mkdir()
    write_tar(
        archive_path,
        members=[("demo/config.txt", b"v1")],
        symlinks=[("demo/link.txt", "/etc/passwd")],
    )

    result = RollbackManager().restore(archive_path, project)

    assert result.success is False
    assert "Refusing non-regular archive member" in result.error
    assert not (Path(project.path) / "link.txt").exists()
    assert (Path(project.path) / "config.txt").read_text(encoding="utf-8") == "v1"


def test_restore_missing_archive_fails_closed(tmp_path: Path):
    project = make_project(tmp_path)

    result = RollbackManager().restore(tmp_path / "backups" / "missing.tar.gz", project)

    assert result.success is False
    assert result.restored == []
    assert "not found" in result.error


def build_engine(tmp_path: Path, project: Project, runner, rollback_manager=None, backup: BackupEngine | None = None):
    return UpdateEngine(
        project_service=FakeProjectService(project),
        git_service=FakeGitService(),
        backup_engine=backup or BackupEngine(tmp_path / "backups"),
        compose_provider=FakeCompose(),
        health_engine=FakeHealthEngine(),
        planner=FixedPlanner(healthy_plan(project, dry_run=False, approval_required=True)),
        audit_service=AuditService(tmp_path / "audit"),
        rollback_manager=rollback_manager or RollbackManager(),
        runner=runner,
    )


def test_failed_update_restores_from_snapshot_and_audits(tmp_path: Path):
    project = make_project(tmp_path)

    def mutating_runner(command, **kwargs):
        project_path = Path(project.path)
        (project_path / "config.txt").write_text("v2", encoding="utf-8")
        (project_path / "extra.txt").write_text("new", encoding="utf-8")
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    engine = build_engine(tmp_path, project, mutating_runner)

    with pytest.raises(Exception, match="Step 'Custom runtime rebuild' failed"):
        engine.execute_update("demo", approve=True)

    assert (Path(project.path) / "config.txt").read_text(encoding="utf-8") == "v1"
    assert (Path(project.path) / "extra.txt").exists()
    audits = list((tmp_path / "audit").glob("*.json"))
    assert len(audits) == 1
    payload = json.loads(audits[0].read_text(encoding="utf-8"))
    assert payload["outcome"] == "failed"
    assert "Step 'Custom runtime rebuild' failed" in payload["error"]
    assert payload["restore"]["success"] is True
    assert "config.txt" in payload["restore"]["restored"]
    assert "extra.txt" in payload["restore"]["left_in_place"]


def test_no_restore_on_dry_run_or_approval_denied(tmp_path: Path):
    project = make_project(tmp_path)
    backup = BackupEngine(tmp_path / "backups")
    rollback = RecordingRollbackManager()

    dry_engine = build_engine(
        tmp_path,
        project,
        failing_runner,
        rollback_manager=rollback,
        backup=backup,
    )
    dry_engine.planner = FixedPlanner(healthy_plan(project, dry_run=True, approval_required=False))
    dry_audit = dry_engine.execute_update("demo", dry_run=True)
    assert dry_audit.restore is None

    approved_engine = build_engine(
        tmp_path,
        project,
        failing_runner,
        rollback_manager=rollback,
        backup=backup,
    )
    with pytest.raises(Exception, match="Explicit approval is required"):
        approved_engine.execute_update("demo")

    assert rollback.calls == []


def test_restore_failure_does_not_mask_original_error(tmp_path: Path):
    project = make_project(tmp_path)
    (Path(project.path) / "config.txt").write_text("v2", encoding="utf-8")
    rollback = NoopRollbackManager(
        RestoreResult(attempted=True, success=False, error="Snapshot archive not found")
    )
    engine = build_engine(tmp_path, project, failing_runner, rollback_manager=rollback)

    with pytest.raises(Exception, match="Step 'Custom runtime rebuild' failed"):
        engine.execute_update("demo", approve=True)

    assert len(rollback.calls) == 1
    assert (Path(project.path) / "config.txt").read_text(encoding="utf-8") == "v2"
    audits = list((tmp_path / "audit").glob("*.json"))
    payload = json.loads(audits[0].read_text(encoding="utf-8"))
    assert payload["outcome"] == "failed"
    assert payload["restore"]["success"] is False
    assert payload["restore"]["error"] == "Snapshot archive not found"