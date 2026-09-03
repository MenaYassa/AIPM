"""Upstream change analysis: read-only classification of incoming remote changes.

Uses disposable temporary Git repositories (tmp_path); never contacts the
network and never depends on /home/ubuntu or any real production repository.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aipm.models.project import Project, ProjectCapabilities
from aipm.services.git.service import GitService


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


def work_path(project: Project) -> Path:
    return Path(project.path)


def test_remote_changed_files_lists_incoming_files_without_fetching(tmp_path: Path):
    project = make_repo(tmp_path)
    service = GitService()
    # No remote commits yet: analysis succeeds and reports nothing incoming.
    assert service.remote_changed_files(project) == []
    make_remote_commit(project, "remote.txt", "from remote\n")
    # The remote tip exists but the local tracking ref has not been advanced
    # by a fetch, so read-only analysis of already-fetched state sees nothing.
    assert service.remote_changed_files(project) == []
    git("fetch", "origin", cwd=work_path(project))
    assert service.remote_changed_files(project) == ["remote.txt"]


def test_remote_changed_files_returns_none_for_repo_without_remote(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    git("init", "-b", "main", cwd=plain)
    project = Project(name="plain", path=str(plain), capabilities=ProjectCapabilities(has_git=True))
    assert GitService().remote_changed_files(project) is None


def test_prepare_update_blocks_when_incoming_changes_cannot_be_analyzed(tmp_path: Path):
    project = make_repo(tmp_path)
    make_remote_commit(project, "remote.txt", "from remote\n")
    # Without a fetch the local tracking ref is behind the remote's tip, but a
    # broken remote URL makes any future fetch impossible; the three-dot diff
    # still works against the stale tracking ref, so simulate the true
    # unanalyzable case with a detached HEAD instead.
    git("checkout", "--detach", "HEAD", cwd=work_path(project))
    plan = GitService().prepare_update(project)
    assert plan.proceed is False
    assert plan.review_required is True
    assert any("could not be analyzed read-only" in reason for reason in plan.reasons)


def test_prepare_update_blocks_on_incoming_critical_file_change(tmp_path: Path):
    project = make_repo(tmp_path)
    make_remote_commit(project, "compose.yaml", "services: {}\n")
    git("fetch", "origin", cwd=work_path(project))
    plan = GitService().prepare_update(project)
    assert plan.proceed is False
    assert plan.review_required is True
    assert any("critical infrastructure files" in reason and "compose.yaml" in reason for reason in plan.reasons)


def test_prepare_update_reports_non_critical_incoming_changes_without_blocking(tmp_path: Path):
    project = make_repo(tmp_path)
    make_remote_commit(project, "docs.md", "new docs\n")
    git("fetch", "origin", cwd=work_path(project))
    plan = GitService().prepare_update(project)
    assert plan.proceed is True
    assert plan.pull_required is True
    assert any("1 file(s) will change on pull" in reason and "docs.md" in reason for reason in plan.reasons)


def test_prepare_update_with_no_incoming_changes_has_no_analysis_reason(tmp_path: Path):
    project = make_repo(tmp_path)
    git("fetch", "origin", cwd=work_path(project))
    plan = GitService().prepare_update(project)
    assert plan.proceed is True
    assert plan.pull_required is False
    assert not any("will change on pull" in reason for reason in plan.reasons)
    assert not any("could not be analyzed" in reason for reason in plan.reasons)


def test_remote_analysis_never_mutates_the_worktree(tmp_path: Path):
    project = make_repo(tmp_path)
    make_remote_commit(project, "docs.md", "new docs\n")
    git("fetch", "origin", cwd=work_path(project))
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work_path(project), check=True, capture_output=True, text=True
    ).stdout
    status_before = subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=work_path(project), check=True, capture_output=True, text=True
    ).stdout
    GitService().prepare_update(project)
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work_path(project), check=True, capture_output=True, text=True
    ).stdout
    status_after = subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=work_path(project), check=True, capture_output=True, text=True
    ).stdout
    assert head_before == head_after
    assert status_before == status_after
