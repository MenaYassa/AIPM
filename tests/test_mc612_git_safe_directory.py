"""MC-6.12: bounded Git telemetry trusts only the queried path and cannot zero out project sampling."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from aipm.models.project import Project, ProjectCapabilities
from aipm.providers.git.provider import GitError, GitDiscoveryCancelled, GitProvider
from aipm.services.project.service import ProjectService


def _fake_popen_factory(returncode: int, captured: dict):
    def fake_popen(args, **kwargs):
        captured["args"] = tuple(args)
        stdout_read, _stdout_write = os.pipe()
        stderr_read, _stderr_write = os.pipe()
        os.close(_stdout_write)
        os.close(_stderr_write)
        return SimpleNamespace(
            stdout=os.fdopen(stdout_read, "rb"),
            stderr=os.fdopen(stderr_read, "rb"),
            poll=lambda: returncode,
            wait=lambda timeout: returncode,
            kill=lambda: None,
            terminate=lambda: None,
            pid=2**30,
        )

    return fake_popen


def test_bounded_git_injects_safe_directory(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("aipm.providers.git.provider.subprocess.Popen", _fake_popen_factory(0, captured))

    output = GitProvider._run_bounded_git("/srv/demo", ("rev-parse", "--is-inside-work-tree"), timeout_seconds=5.0, output_limit=4096)

    assert captured["args"] == ("git", "-c", "safe.directory=/srv/demo", "rev-parse", "--is-inside-work-tree")
    assert output == ""


def test_bounded_git_wraps_nonzero_exit_as_git_error(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("aipm.providers.git.provider.subprocess.Popen", _fake_popen_factory(128, captured))

    with pytest.raises(GitError):
        GitProvider._run_bounded_git("/srv/demo", ("rev-parse", "HEAD"), timeout_seconds=5.0, output_limit=4096)


class _StubLogger:
    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(message)


def _stub_app(tmp_path, logger: _StubLogger) -> SimpleNamespace:
    discovery = SimpleNamespace(
        search_paths=[str(tmp_path)],
        ignore_dirs={".git", ".venv", "__pycache__", "node_modules"},
        follow_symlinks=False,
        max_depth=2,
    )
    return SimpleNamespace(config=SimpleNamespace(discovery=discovery), logger=logger)


class _FakeGitProvider:
    def __init__(self, error: Exception):
        self.error = error
        self.calls = 0

    def repository_bounded(self, project, **kwargs):
        self.calls += 1
        raise self.error


def test_discover_survives_git_enrichment_failure(tmp_path):
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / ".git").mkdir()
    logger = _StubLogger()
    service = ProjectService(app=_stub_app(tmp_path, logger))
    service.git_provider = _FakeGitProvider(GitError("bounded Git command failed"))

    projects = service.discover(bounded=True)

    assert [project.name for project in projects] == ["demo"]
    assert projects[0].git is None
    assert projects[0].capabilities.has_git is True
    assert logger.warnings and "demo" in logger.warnings[0]


def test_discover_propagates_cancellation(tmp_path):
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / ".git").mkdir()
    service = ProjectService(app=_stub_app(tmp_path, _StubLogger()))
    service.git_provider = _FakeGitProvider(GitDiscoveryCancelled("Git telemetry cancelled"))

    with pytest.raises(GitDiscoveryCancelled):
        service.discover(bounded=True)


def test_repository_bounded_tolerates_unreadable_refs(monkeypatch):
    captured: list = []

    def fake_popen(args, **kwargs):
        captured.append(tuple(args))
        cmd = args[3]
        out = b""
        rc = 0
        if cmd == "rev-parse":
            if "HEAD" in args:
                rc = 128
                out = b"fatal: ambiguous argument 'HEAD': unknown revision or path not in the working tree.\n"
            else:
                out = b"true\n"
        elif cmd == "symbolic-ref":
            rc = 128
            out = b"fatal: No such ref: HEAD\n"
        elif cmd == "status":
            out = b" M src/demo.py\n?? notes.txt\n"
        elif cmd == "log":
            rc = 128

        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        if out:
            os.write(stdout_write, out)
        os.close(stdout_write)
        os.close(stderr_write)
        return SimpleNamespace(
            stdout=os.fdopen(stdout_read, "rb"),
            stderr=os.fdopen(stderr_read, "rb"),
            poll=lambda: rc,
            wait=lambda timeout: rc,
            kill=lambda: None,
            terminate=lambda: None,
            pid=2**30,
        )

    monkeypatch.setattr("aipm.providers.git.provider.subprocess.Popen", fake_popen)

    project = Project(name="demo", path="/srv/demo", capabilities=ProjectCapabilities(has_git=True))
    repository = GitProvider().repository_bounded(project, timeout_seconds=5.0, max_items=100)

    assert repository.exists is True
    assert repository.branch is None
    assert repository.current_sha is None
    assert repository.dirty is True
    assert repository.modified_files == ["src/demo.py"]
    assert repository.untracked_files == ["notes.txt"]
    assert repository.last_commit_message is None
    assert repository.last_commit_author is None