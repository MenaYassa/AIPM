"""P0 Git transaction safety: typed transaction, stash preservation, exact conflicts.

Uses disposable temporary Git repositories (tmp_path) or explicit fakes; never
depends on /home/ubuntu or any real production repository.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aipm.core.exceptions import GitTransactionError, UpdateError
from aipm.models.git import GitRepository
from aipm.models.git_transaction import GitTransactionResult
from aipm.models.health import HealthState
from aipm.models.health_report import HealthReport
from aipm.models.project import Project, ProjectCapabilities
from aipm.models.update import UpdatePlan, UpdateRisk
from aipm.models.verification import UpdateVerification, UpdateVerificationStatus
from aipm.services.backup.engine import BackupEngine
from aipm.services.project.service import ProjectService
from aipm.services.update.audit import AuditService
from aipm.services.update.engine import UpdateEngine
from aipm.services.update.git_transaction import GitTransactionRunner
from aipm.services.update.rollback import RollbackManager


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def make_repo(root: Path, name: str = "demo") -> Project:
    """A disposable repository with one remote and one committed file."""
    remote = root / "remote.git"
    remote.mkdir()
    git("init", "--bare", "-b", "main", cwd=remote)
    work = root / name
    work.mkdir()
    git("init", "-b", "main", cwd=work)
    git("config", "user.email", "aipm@test", cwd=work)
    git("config", "user.name", "AIPM Test", cwd=work)
    (work / "config.txt").write_text("v1\n", encoding="utf-8")
    git("add", "config.txt", cwd=work)
    git("commit", "-m", "initial", cwd=work)
    git("remote", "add", "origin", str(remote), cwd=work)
    git("push", "-u", "origin", "main", cwd=work)
    return Project(name=name, path=str(work), capabilities=ProjectCapabilities(has_git=True))


def make_remote_commit(project: Project, filename: str, content: str) -> None:
    """Commit directly on the bare remote so the work repo is strictly behind."""
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=project.path, check=True, capture_output=True, text=True
    ).stdout.strip()
    scratch = Path(project.path).parent / "remote-scratch"
    scratch.mkdir(exist_ok=True)
    git("clone", "--quiet", remote, str(scratch / "clone"), cwd=scratch)
    clone = scratch / "clone"
    git("config", "user.email", "aipm@test", cwd=clone)
    git("config", "user.name", "AIPM Test", cwd=clone)
    (clone / filename).write_text(content, encoding="utf-8")
    git("add", filename, cwd=clone)
    git("commit", "-m", f"remote {filename}", cwd=clone)
    git("push", "origin", "main", cwd=clone)


class FakeProjectService(ProjectService):
    def __init__(self, project: Project):
        self.project = project

    def get_project(self, name: str) -> Project:
        assert name == self.project.name
        return self.project


class FakeCompose:
    def up(self, *args, **kwargs):
        raise AssertionError("compose must not be used when start_services.py exists")


class FixedHealthEngine:
    def __init__(self):
        self.report = HealthReport(project="demo", score=100, state=HealthState.HEALTHY)

    def analyze(self, project):
        return self.report


class FixedPlanner:
    def __init__(self, plan: UpdatePlan):
        self._plan = plan

    def plan(self, name, dry_run=False):
        return self._plan


class FakeVerifier:
    def verify_update(self, project_name, *, health_before, health_after):
        return UpdateVerification(status=UpdateVerificationStatus.SUCCESS, passed=["Demo: info"])


def ok_runner(command, **kwargs):
    return SimpleNamespace(returncode=0, stdout="ok", stderr="")


def failing_runner(command, **kwargs):
    return SimpleNamespace(returncode=1, stdout="", stderr="boom")


def plan_for(project: Project, *, stash_required=False, pull_required=False, remote=True) -> UpdatePlan:
    work = Path(project.path)
    git_repo = GitRepository(
        exists=True,
        branch="main",
        remote_url=str(work.parent / "remote.git") if remote else None,
    )
    return UpdatePlan(
        project=project.name,
        project_path=project.path,
        dry_run=False,
        proceed=True,
        approval_required=True,
        risk=UpdateRisk.MEDIUM,
        actions=["Create a configuration safety snapshot"],
        snapshot_required=True,
        estimated_restart=True,
        stash_required=stash_required,
        pull_required=pull_required,
        git=git_repo,
        health_before=HealthReport(project=project.name, score=100, state=HealthState.HEALTHY),
    )


def build_engine(tmp_path: Path, project: Project, plan: UpdatePlan, runner=ok_runner):
    work = Path(project.path)
    (work / "start_services.py").write_text("print('ok')\n", encoding="utf-8")
    return UpdateEngine(
        project_service=FakeProjectService(project),
        backup_engine=BackupEngine(tmp_path / "backups"),
        compose_provider=FakeCompose(),
        health_engine=FixedHealthEngine(),
        planner=FixedPlanner(plan),
        audit_service=AuditService(tmp_path / "audit"),
        rollback_manager=RollbackManager(),
        verifier=FakeVerifier(),
        runner=runner,
    )


# --- GitTransactionRunner: clean repository update ---------------------------


def test_runner_clean_repo_update_succeeds(tmp_path: Path):
    project = make_repo(tmp_path)
    result = GitTransactionRunner().run(
        project, stash_required=False, fetch_required=True, pull_required=False
    )
    assert result.success is True
    assert result.stashed is False
    assert result.pulled is False
    assert result.stash_applied is False
    assert result.stash_preserved is False
    assert result.conflicts == []
    assert result.errors == []


# --- dirty tracked file / untracked file are stashed (operator changes preserved)


def test_runner_stashes_dirty_and_untracked_then_applies_and_drops(tmp_path: Path):
    project = make_repo(tmp_path)
    work = Path(project.path)
    (work / "config.txt").write_text("operator edit\n", encoding="utf-8")
    (work / "notes.txt").write_text("operator note\n", encoding="utf-8")

    result = GitTransactionRunner().run(
        project, stash_required=True, fetch_required=False, pull_required=False
    )

    assert result.success is True
    assert result.stashed is True
    assert result.stash_applied is True
    assert result.stash_preserved is False
    # operator changes preserved in the working tree
    assert (work / "config.txt").read_text(encoding="utf-8") == "operator edit\n"
    assert (work / "notes.txt").read_text(encoding="utf-8") == "operator note\n"
    # stash dropped only after successful apply
    stash_list = subprocess.run(
        ["git", "stash", "list"], cwd=work, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert stash_list == ""


# --- pull ---------------------------------------------------------------------


def test_runner_pulls_when_required_and_clean(tmp_path: Path):
    project = make_repo(tmp_path)
    make_remote_commit(project, "remote.txt", "from remote\n")
    result = GitTransactionRunner().run(
        project, stash_required=False, fetch_required=True, pull_required=True
    )
    assert result.success is True
    assert result.pulled is True
    assert (Path(project.path) / "remote.txt").read_text(encoding="utf-8") == "from remote\n"


# --- conflicting target path: stash preserved, exact files reported ----------


def test_runner_conflicting_pull_target_preserves_stash_and_reports_exact_files(tmp_path: Path):
    project = make_repo(tmp_path)
    work = Path(project.path)
    # Remote changes config.txt; operator also edited it (non-critical path in
    # a scratch file name, but conflicts on apply because both sides changed it).
    make_remote_commit(project, "config.txt", "remote v2\n")
    (work / "config.txt").write_text("operator edit\n", encoding="utf-8")

    with pytest.raises(GitTransactionError) as excinfo:
        GitTransactionRunner().run(
            project, stash_required=True, fetch_required=True, pull_required=True
        )

    result = excinfo.value.result
    assert isinstance(result, GitTransactionResult)
    assert result.success is False
    assert result.stashed is True
    assert result.stash_preserved is True
    assert result.stash_applied is False
    assert result.conflicts == ["config.txt"]
    assert any("stash apply failed" in error for error in result.errors)
    # the stash is preserved for manual recovery
    stash_list = subprocess.run(
        ["git", "stash", "list"], cwd=work, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert stash_list != ""
    # operator content is still recoverable from the stash
    stash_show = subprocess.run(
        ["git", "stash", "show", "-p", "stash@{0}"], cwd=work, check=True, capture_output=True, text=True
    ).stdout
    assert "operator edit" in stash_show
    assert "config.txt" in str(excinfo.value)


# --- git command failure (fetch/pull) ----------------------------------------


def test_runner_pull_failure_returns_typed_result_and_preserves_stash(tmp_path: Path):
    project = make_repo(tmp_path)
    work = Path(project.path)
    (work / "config.txt").write_text("operator edit\n", encoding="utf-8")
    # break the remote URL so fetch fails
    git("remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"), cwd=work)

    with pytest.raises(GitTransactionError) as excinfo:
        GitTransactionRunner().run(
            project, stash_required=True, fetch_required=True, pull_required=True
        )

    result = excinfo.value.result
    assert result.success is False
    assert result.stashed is True
    assert result.stash_preserved is True
    assert result.pulled is False
    assert result.conflicts == []
    assert any("fetch/pull failed" in error for error in result.errors)
    stash_list = subprocess.run(
        ["git", "stash", "list"], cwd=work, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert stash_list != ""


def test_runner_stash_failure_returns_typed_result(tmp_path: Path):
    project = make_repo(tmp_path)

    class ExplodingStashService:
        def stash(self, project, message):
            raise RuntimeError("stash exploded")

        def fetch(self, project):
            raise AssertionError("fetch must not run after stash failure")

        def pull(self, project):
            raise AssertionError("pull must not run after stash failure")

        def apply_stash(self, project):
            raise AssertionError("apply must not run after stash failure")

        def drop_stash(self, project):
            raise AssertionError("drop must not run after stash failure")

        def conflicted_files(self, project):
            raise AssertionError("conflict enumeration must not run after stash failure")

    with pytest.raises(GitTransactionError) as excinfo:
        GitTransactionRunner(git_service=ExplodingStashService()).run(
            project, stash_required=True, fetch_required=False, pull_required=False
        )

    result = excinfo.value.result
    assert result.success is False
    assert result.stashed is False
    assert result.errors == ["stash failed: stash exploded"]


def test_runner_conflict_enumeration_failure_does_not_mask_apply_error(tmp_path: Path):
    project = make_repo(tmp_path)

    class FailingApplyAndConflicts:
        def stash(self, project, message):
            pass

        def fetch(self, project):
            pass

        def pull(self, project):
            pass

        def apply_stash(self, project):
            raise RuntimeError("apply exploded")

        def drop_stash(self, project):
            raise AssertionError("drop must not run after apply failure")

        def conflicted_files(self, project):
            raise RuntimeError("enumeration exploded")

    with pytest.raises(GitTransactionError) as excinfo:
        GitTransactionRunner(git_service=FailingApplyAndConflicts()).run(
            project, stash_required=True, fetch_required=False, pull_required=False
        )

    result = excinfo.value.result
    assert result.success is False
    assert result.stash_preserved is True
    assert result.conflicts == []
    assert any("stash apply failed" in error for error in result.errors)
    # the apply error is not masked by the enumeration failure
    assert "apply exploded" in str(excinfo.value)


# --- engine integration: git failure enters rollback, audit carries the result


def test_engine_git_failure_restores_snapshot_and_audits_transaction(tmp_path: Path):
    project = make_repo(tmp_path)
    work = Path(project.path)
    make_remote_commit(project, "config.txt", "remote v2\n")
    (work / "config.txt").write_text("operator edit\n", encoding="utf-8")
    git("add", "config.txt", cwd=work)  # ensure the dirty file is tracked-changed

    engine = build_engine(tmp_path, project, plan_for(project, stash_required=True, pull_required=True))

    with pytest.raises(UpdateError, match="Git transaction failed applying the safety stash"):
        engine.execute_update("demo", approve=True)

    payload = json.loads(next(iter((tmp_path / "audit").glob("*.json"))).read_text(encoding="utf-8"))
    assert payload["outcome"] == "failed"
    assert payload["git_transaction"]["success"] is False
    assert payload["git_transaction"]["stash_preserved"] is True
    assert payload["git_transaction"]["conflicts"] == ["config.txt"]
    assert any("stash apply failed" in error for error in payload["git_transaction"]["errors"])
    # rollback ran: project files restored from the snapshot
    assert payload["restore"]["success"] is True
    # operator change preserved in the safety stash, not discarded
    stash_list = subprocess.run(
        ["git", "stash", "list"], cwd=work, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert stash_list != ""


def test_engine_success_audits_git_transaction(tmp_path: Path):
    project = make_repo(tmp_path)
    engine = build_engine(tmp_path, project, plan_for(project))

    audit = engine.execute_update("demo", approve=True)

    assert audit.outcome == "success"
    assert audit.git_transaction is not None
    assert audit.git_transaction.success is True
    payload = json.loads(next(iter((tmp_path / "audit").glob("*.json"))).read_text(encoding="utf-8"))
    assert payload["git_transaction"]["success"] is True
    assert payload["git_transaction"]["stashed"] is False
    assert payload["git_transaction"]["pulled"] is False
    assert payload["git_transaction"]["stash_applied"] is False
    assert payload["git_transaction"]["stash_preserved"] is False


def test_engine_verification_failure_still_audits_git_transaction(tmp_path: Path):
    project = make_repo(tmp_path)

    class FailingVerifier:
        def verify_update(self, project_name, *, health_before, health_after):
            return UpdateVerification(
                status=UpdateVerificationStatus.FAILURE,
                failures=["Demo: container down (critical)"],
            )

    engine = build_engine(tmp_path, project, plan_for(project))
    engine.verifier = FailingVerifier()

    with pytest.raises(UpdateError, match="Post-update verification failed"):
        engine.execute_update("demo", approve=True)

    payload = json.loads(next(iter((tmp_path / "audit").glob("*.json"))).read_text(encoding="utf-8"))
    # audit preserves execution, git-transaction, verification, and restore distinctly
    assert payload["outcome"] == "failed"
    assert payload["git_transaction"]["success"] is True
    assert payload["verification"]["status"] == "failure"
    assert payload["restore"]["success"] is True


def test_engine_runtime_failure_audits_completed_transaction(tmp_path: Path):
    project = make_repo(tmp_path)
    engine = build_engine(tmp_path, project, plan_for(project), runner=failing_runner)

    with pytest.raises(UpdateError, match="Custom runtime rebuild"):
        engine.execute_update("demo", approve=True)

    payload = json.loads(next(iter((tmp_path / "audit").glob("*.json"))).read_text(encoding="utf-8"))
    assert payload["outcome"] == "failed"
    assert payload["git_transaction"]["success"] is True  # git phases succeeded before runtime failure


def test_engine_no_git_project_audits_completed_transaction(tmp_path: Path):
    work = tmp_path / "plain"
    work.mkdir()
    (work / "start_services.py").write_text("print('ok')\n", encoding="utf-8")
    project = Project(name="demo", path=str(work))
    engine = build_engine(
        tmp_path,
        project,
        UpdatePlan(
            project="demo",
            project_path=str(work),
            dry_run=False,
            proceed=True,
            approval_required=True,
            risk=UpdateRisk.MEDIUM,
            actions=["Create a configuration safety snapshot"],
            snapshot_required=True,
            estimated_restart=True,
            git=None,
            health_before=HealthReport(project="demo", score=100, state=HealthState.HEALTHY),
        ),
    )

    audit = engine.execute_update("demo", approve=True)

    assert audit.outcome == "success"
    assert audit.git_transaction is not None
    assert audit.git_transaction.success is True
    assert audit.git_transaction.stashed is False


# --- destructive-command refusal (source-level contract) ----------------------


def test_git_transaction_introduces_no_destructive_commands():
    from aipm.services.update import git_transaction as module

    source = module.__dict__["GitTransactionRunner"].run.__code__.co_consts
    text = " ".join(str(const) for const in source)
    for forbidden in ("clean", "reset --hard", "checkout --", "checkout .", "restore ."):
        assert forbidden not in text, forbidden
