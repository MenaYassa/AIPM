"""MC-6.13 C6.6: unbounded Git discovery cannot escape via persistent-cat-file EPIPE.

The V4 forensic diagnosis proved a deterministic escape path in
``GitProvider.repository()``: GitPython's persistent ``cat-file`` subprocess dies
(rc 128, dubious ownership), the first request is swallowed by
``_current_commit`` as ``ValueError``, and the second request raises
``BrokenPipeError`` out of ``_remote_commit`` (no helper tuple catches it), aborting
whole-list discovery. The fix degrades the enrichment to the empty repository
snapshot, which fail-closes downstream (``prepare_update`` -> planner BLOCKED)
instead of escaping.

Host-trust boundaries (UID 995 vs the ordinary drill fixture, executor-side
discovery of the 24-hex target, throwaway-config controlled failure) require the
disposable-host apparatus and live as Block 3' (L4) proofs; they are deliberately
NOT duplicated here. These tests are repository-local (L3).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from aipm.models.project import Project, ProjectCapabilities
from aipm.providers.git import provider as provider_module
from aipm.providers.git.provider import GitProvider
from aipm.services.project.service import ProjectService
from aipm.services.update.plan_identity import UpdatePlanIdentity
from aipm.services.update.planner import UpdatePlanner

from tests.update_fixtures import FixedProjectService, GitService, hermetic_health_engine, make_repo

REPO = Path(__file__).parents[1]
PROVIDER = REPO / "src" / "aipm" / "providers" / "git" / "provider.py"


class _DyingHead:
    """Req1 analogue: the first cat-file read dies and is swallowed."""

    is_detached = False

    @property
    def commit(self):
        raise ValueError("Cmd('cat-file') died due to broken pipe")


class _EPIPERepo:
    """Fake GitPython Repo replaying the proven V4 death sequence.

    ``head.commit`` raises ``ValueError`` (req1 EOF -- swallowed by
    ``_current_commit``); any ``refs`` access raises ``BrokenPipeError``
    (req2 ``cmd.stdin.flush()`` -- the escape no helper tuple caught).
    """

    head = _DyingHead()
    active_branch = SimpleNamespace(name="main")

    def __getattr__(self, name):
        raise BrokenPipeError("cat-file subprocess died (dubious ownership)")


def _patch_repo_construction(monkeypatch, degraded_name: str | None):
    """Patch the ``git.Repo`` construction seam.

    Repositories named ``degraded_name`` construct into the EPIPE sequence;
    every other path keeps the real constructor. ``None`` degrades all.
    """
    real_repo = provider_module.git.Repo

    def fake_repo(path):
        if degraded_name is None or Path(path).name == degraded_name:
            return _EPIPERepo()
        return real_repo(path)

    monkeypatch.setattr(provider_module.git, "Repo", fake_repo)


def _stub_app(root: str) -> SimpleNamespace:
    discovery = SimpleNamespace(
        search_paths=[root],
        ignore_dirs={".git", ".venv", "__pycache__", "node_modules"},
        follow_symlinks=False,
        max_depth=2,
    )
    return SimpleNamespace(config=SimpleNamespace(discovery=discovery), logger=None)


def test_trusted_repository_succeeds(tmp_path):
    project = make_repo(tmp_path, name="demo")

    snapshot = GitProvider().repository(project)

    assert snapshot.exists is True
    assert snapshot.branch == "main"
    assert snapshot.current_sha is not None
    assert snapshot.remote_sha is not None
    assert snapshot.remote_url is not None
    assert snapshot.dirty is False


def test_dubious_ownership_epipe_degrades_instead_of_escaping(monkeypatch, tmp_path):
    project = make_repo(tmp_path, name="demo")
    _patch_repo_construction(monkeypatch, None)

    snapshot = GitProvider().repository(project)

    assert snapshot.exists is False
    assert snapshot.branch is None
    assert snapshot.current_sha is None
    assert snapshot.remote_sha is None
    assert snapshot.remote_url is None
    assert snapshot.dirty is False


def test_direct_remote_commit_epipe_degrades(monkeypatch, tmp_path):
    project = make_repo(tmp_path, name="demo")
    provider = GitProvider()

    def broken_remote_commit(repo, branch):
        raise BrokenPipeError("cmd.stdin.flush() after cat-file death")

    monkeypatch.setattr(provider, "_remote_commit", broken_remote_commit)

    snapshot = provider.repository(project)

    assert snapshot.exists is False
    assert snapshot.current_sha is None


def test_degraded_snapshot_fail_closed_in_planner(monkeypatch, tmp_path):
    project = make_repo(tmp_path, name="demo")
    _patch_repo_construction(monkeypatch, None)

    git_service = GitService()
    planner = UpdatePlanner(
        FixedProjectService(project, git_service),
        git_service=git_service,
        health_engine=hermetic_health_engine(),
    )

    plan = planner.plan(project.name)

    assert plan.proceed is False
    assert plan.risk.value == "blocked"
    reasons = " ".join(plan.reasons)
    assert "Project is not a Git repository." in reasons
    assert "Git state requires manual review before an update can proceed." in reasons


def test_discovery_continues_past_degrading_candidate(monkeypatch, tmp_path):
    make_repo(tmp_path, name="zebra")
    make_repo(tmp_path, name="alpha")
    _patch_repo_construction(monkeypatch, "alpha")

    service = ProjectService(app=_stub_app(str(tmp_path / "projects")))

    projects = service.discover()

    names = sorted(project.name for project in projects)
    assert names == ["alpha", "zebra"]
    degraded = next(project for project in projects if project.name == "alpha")
    assert degraded.capabilities.has_git is True
    assert degraded.git is not None
    assert degraded.git.exists is False
    healthy = next(project for project in projects if project.name == "zebra")
    assert healthy.git is not None
    assert healthy.git.exists is True
    assert healthy.git.current_sha is not None


def test_no_safe_directory_injection_in_repository_path():
    text = PROVIDER.read_text(encoding="utf-8")
    repository_body = text.split("def repository(self, project: Project)", 1)[1]
    repository_body = repository_body.split("def _terminate_process", 1)[0]

    assert "safe.directory" not in repository_body
    assert "safe.directory" in text  # the bounded telemetry path retains its own per-path trust scope
    assert "safe.directory=*" not in text
    assert "safe.directory = *" not in text


def test_unrelated_git_failures_retain_existing_behavior():
    provider = GitProvider()
    project = Project(name="missing", path="/nonexistent/path", capabilities=ProjectCapabilities(has_git=True))

    snapshot = provider.repository(project)

    assert snapshot.exists is False


def test_plan_identity_deterministic_double_plan(tmp_path):
    project = make_repo(tmp_path, name="demo")
    git_service = GitService()
    planner = UpdatePlanner(
        FixedProjectService(project, git_service),
        git_service=git_service,
        health_engine=hermetic_health_engine(),
    )

    first = UpdatePlanIdentity.from_plan(planner.plan(project.name))
    second = UpdatePlanIdentity.from_plan(planner.plan(project.name))

    assert first.digest() == second.digest()
    assert first.git_current_sha == second.git_current_sha
    assert first.git_current_sha is not None
