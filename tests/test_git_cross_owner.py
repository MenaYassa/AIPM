"""MC-6.13 C6.6: exact-path safe.directory Git observation across owners.

Validates that:
1. Same-owner repositories succeed normally.
2. Cross-owner repositories succeed via exact-path safe.directory scoping.
3. GitPython operations (init, cat-file, diff, status, refs) all receive safe.directory.
4. Another repository path does NOT become trusted (exact-path isolation).
5. Wildcard safe.directory is strictly forbidden.
6. Bounded and normal repository observations remain behaviorally consistent.
7. Unrelated paths remain untrusted and fail closed.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import git
from git.exc import GitCommandError
import pytest

from aipm.models.project import Project, ProjectCapabilities
from aipm.providers.git import provider as provider_module
from aipm.providers.git.provider import GitError, GitProvider
from tests.update_fixtures import make_repo

PROVIDER_PATH = Path(provider_module.__file__)


def _simulate_cross_owner_git(target_path: str):
    """Return a monkeypatched git.cmd.Git.execute that enforces exact-path safe.directory.

    Any command lacking safe.directory=<target_path> in command line arguments,
    persistent options, or environment variables fails with Git's real code 128
    dubious ownership error.
    """
    orig_execute = git.cmd.Git.execute
    resolved_target = str(Path(target_path).resolve())

    def dubious_execute(self, command, *args, **kwargs):
        has_trust = False
        target_token = f"safe.directory={resolved_target}"

        # 1. Check command line arguments
        if isinstance(command, (list, tuple)):
            for arg in command:
                if target_token in str(arg):
                    has_trust = True
                    break

        # 2. Check persistent git options on the Git instance
        if not has_trust:
            persistent = getattr(self, "_persistent_git_options", [])
            for arg in persistent:
                if target_token in str(arg):
                    has_trust = True
                    break

        # 3. Check environment variables on the Git instance
        if not has_trust:
            env = getattr(self, "_environment", {})
            for k, v in env.items():
                if k.startswith("GIT_CONFIG_VALUE") and str(v) == resolved_target:
                    has_trust = True
                    break

        # 4. Check inline env passed to execute
        if not has_trust:
            inline_env = kwargs.get("env") or {}
            for k, v in inline_env.items():
                if k.startswith("GIT_CONFIG_VALUE") and str(v) == resolved_target:
                    has_trust = True
                    break

        if not has_trust:
            raise GitCommandError(
                command,
                128,
                b"",
                f"fatal: detected dubious ownership in repository at '{resolved_target}'".encode("utf-8"),
            )

        return orig_execute(self, command, *args, **kwargs)

    return dubious_execute


def test_same_owner_repository(tmp_path):
    project = make_repo(tmp_path, name="alpha")
    provider = GitProvider()

    snapshot = provider.repository(project)

    assert snapshot.exists is True
    assert snapshot.branch == "main"
    assert snapshot.current_sha is not None
    assert snapshot.remote_sha is not None
    assert snapshot.remote_url is not None
    assert snapshot.dirty is False


def test_exact_path_safe_directory_configuration(tmp_path):
    project = make_repo(tmp_path, name="alpha")
    resolved_path = str(Path(project.path).resolve())

    repo = GitProvider._open_repo(project.path)

    assert repo.git._persistent_git_options == ["-c", f"safe.directory={resolved_path}"]
    assert repo.git._environment["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert repo.git._environment["GIT_CONFIG_VALUE_0"] == resolved_path
    assert repo.git._environment["GIT_CONFIG_COUNT"] == "1"
    assert repo.git._environment["GIT_OPTIONAL_LOCKS"] == "0"


def test_cross_owner_repository_succeeds(monkeypatch, tmp_path):
    project = make_repo(tmp_path, name="cross_owner_target")
    repo_path = str(Path(project.path).resolve())

    # Add an untracked file and modify a tracked file to verify full snapshot extraction
    untracked_file = Path(repo_path) / "untracked.txt"
    untracked_file.write_text("new file", encoding="utf-8")
    config_file = Path(repo_path) / "config.txt"
    config_file.write_text("modified config", encoding="utf-8")

    # Enforce cross-owner dubious ownership: git commands fail without safe.directory
    monkeypatch.setattr(git.cmd.Git, "execute", _simulate_cross_owner_git(repo_path))

    provider = GitProvider()
    snapshot = provider.repository(project)

    assert snapshot.exists is True
    assert snapshot.branch == "main"
    assert snapshot.current_sha is not None
    assert snapshot.remote_sha is not None
    assert snapshot.dirty is True
    assert "untracked.txt" in snapshot.untracked_files
    assert "config.txt" in snapshot.modified_files


def test_another_repository_path_not_trusted(monkeypatch, tmp_path):
    project_a = make_repo(tmp_path, name="repo_a")
    project_b = make_repo(tmp_path, name="repo_b")
    path_a = str(Path(project_a.path).resolve())
    path_b = str(Path(project_b.path).resolve())

    # Open repo_a: only path_a is configured in its persistent options/env
    repo_a = GitProvider._open_repo(project_a.path)
    assert f"safe.directory={path_a}" in str(repo_a.git._persistent_git_options)
    assert f"safe.directory={path_b}" not in str(repo_a.git._persistent_git_options)

    # Open repo_b: only path_b is configured
    repo_b = GitProvider._open_repo(project_b.path)
    assert f"safe.directory={path_b}" in str(repo_b.git._persistent_git_options)
    assert f"safe.directory={path_a}" not in str(repo_b.git._persistent_git_options)

    # Cross-owner enforcement: only path_a is trusted by policy
    monkeypatch.setattr(git.cmd.Git, "execute", _simulate_cross_owner_git(path_a))

    provider = GitProvider()
    snap_a = provider.repository(project_a)
    snap_b = provider.repository(project_b)

    assert snap_a.exists is True
    assert snap_b.exists is False  # path_b refused due to dubious ownership


def test_no_wildcard_safe_directory():
    # Attempting to open a path containing '*' must be rejected immediately
    with pytest.raises(GitError, match="Wildcard safe.directory is forbidden"):
        GitProvider._open_repo("/*")

    with pytest.raises(GitError, match="Wildcard safe.directory is forbidden"):
        GitProvider._open_repo("/home/ubuntu/*")

    with pytest.raises(GitError, match="Wildcard safe.directory is forbidden"):
        GitProvider._run_bounded_git("/*", ("status",), timeout_seconds=1.0, output_limit=1024)

    # Source inspection guarantees no wildcard configuration exists in provider code
    text = PROVIDER_PATH.read_text(encoding="utf-8")
    assert "safe.directory=*" not in text
    assert "safe.directory = *" not in text


def test_bounded_and_normal_observation_behaviorally_consistent(tmp_path):
    project = make_repo(tmp_path, name="consistent_target")
    repo_path = str(Path(project.path).resolve())

    untracked_file = Path(repo_path) / "scratch.txt"
    untracked_file.write_text("hello", encoding="utf-8")

    provider = GitProvider()
    normal_snap = provider.repository(project)
    bounded_snap = provider.repository_bounded(project, timeout_seconds=5.0, max_items=100)

    assert normal_snap.exists is True
    assert bounded_snap.exists is True
    assert normal_snap.branch == bounded_snap.branch
    assert normal_snap.current_sha == bounded_snap.current_sha
    assert normal_snap.dirty == bounded_snap.dirty
    assert normal_snap.untracked_files == bounded_snap.untracked_files


def test_cross_owner_bounded_and_normal_consistent(monkeypatch, tmp_path):
    project = make_repo(tmp_path, name="cross_owner_consistent")
    repo_path = str(Path(project.path).resolve())

    # Mock cross-owner enforcement for both bounded (Popen) and normal (GitPython)
    monkeypatch.setattr(git.cmd.Git, "execute", _simulate_cross_owner_git(repo_path))

    orig_popen = provider_module.subprocess.Popen

    def safe_bounded_popen(cmd, *args, **kwargs):
        target_token = f"safe.directory={repo_path}"
        if not any(target_token in str(c) for c in cmd):
            raise GitCommandError(cmd, 128, b"", b"dubious ownership")
        return orig_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(provider_module.subprocess, "Popen", safe_bounded_popen)

    provider = GitProvider()
    normal = provider.repository(project)
    bounded = provider.repository_bounded(project, timeout_seconds=5.0, max_items=100)

    assert normal.exists is True
    assert bounded.exists is True
    assert normal.branch == bounded.branch == "main"
    assert normal.current_sha == bounded.current_sha
    assert normal.dirty == bounded.dirty == False


def test_unrelated_path_remains_untrusted(monkeypatch, tmp_path):
    project = make_repo(tmp_path, name="untrusted_candidate")
    repo_path = str(Path(project.path).resolve())

    # Enforce that only a different path '/srv/other' is trusted; target repo is untrusted
    monkeypatch.setattr(git.cmd.Git, "execute", _simulate_cross_owner_git("/srv/other"))

    provider = GitProvider()
    snapshot = provider.repository(project)

    # Fails closed to empty repository snapshot
    assert snapshot.exists is False
    assert snapshot.branch is None
    assert snapshot.current_sha is None


def test_safe_repo_configuration_failure_fails_closed(monkeypatch, tmp_path):
    project = make_repo(tmp_path, name="broken_wrapper")

    # If git.cmd wrapper is absent or corrupted, _configure_safe_repo raises GitError
    class BadRepo:
        git = None

    monkeypatch.setattr(provider_module.git, "Repo", lambda path: BadRepo())

    provider = GitProvider()
    # repository() must fail closed to empty repository snapshot rather than escaping
    snapshot = provider.repository(project)
    assert snapshot.exists is False

    # _repo() must fail closed by raising GitError
    with pytest.raises(GitError, match="Cannot establish exact-path Git trust"):
        provider._repo(project)
