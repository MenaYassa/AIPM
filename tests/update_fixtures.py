"""Shared disposable integration fixtures for the update workstream.

Every fixture is fully contained under a pytest ``tmp_path`` root: disposable
work repositories with bare remotes, and on-disk Compose project layouts. They
never point at real project directories, production databases, the real
Docker socket, or any path under /home/ubuntu, and they perform no mutation
outside their own temporary fixture tree.

The Docker-daemon boundary is explicit: these fixtures build Compose *project
layouts on disk* (deterministic service inventories for planner preflight),
never a running Compose stack. Compose runtime execution needs a Docker
daemon, which the repository test environment does not guarantee, so
integration coverage exercises the real planner and its static preflight
against the fixture files and documents that boundary instead of pretending
full Docker integration exists.
"""
from __future__ import annotations

import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

from aipm.engines.health.analyzers.git import GitAnalyzer
from aipm.engines.health.engine import HealthEngine
from aipm.models.project import Project, ProjectCapabilities
from aipm.services.backup.engine import BackupEngine
from aipm.services.git.service import GitService
from aipm.services.update.audit import AuditService
from aipm.services.update.engine import UpdateEngine
from aipm.services.update.rollback import RollbackManager
from aipm.services.update.verifier import UpdateVerifier

# Runtime scripts committed into disposable work repositories so the real
# engine executes the custom-runtime path (never the Compose/Docker path).
PASSING_RUNTIME_SCRIPT = 'print("ok")\n'
FAILING_RUNTIME_SCRIPT = 'import sys\nprint("boom", file=sys.stderr)\nsys.exit(3)\n'
# Writes its marker OUTSIDE the worktree (path supplied via the environment),
# proving the real runner executed without dirtying the repository.
MARKER_RUNTIME_SCRIPT = (
    "import os\n"
    "marker = os.environ.get('AIPM_INTEGRATION_MARKER')\n"
    "if marker:\n"
    "    with open(marker, 'w', encoding='utf-8') as handle:\n"
    "        handle.write('started')\n"
    "print('ok')\n"
)

DEFAULT_COMPOSE_DOCUMENT = "services:\n  web:\n    image: nginx:stable\n  db:\n    image: postgres:16\n"


def git(*args: str, cwd: Path) -> None:
    """Run one Git command inside a disposable fixture repository."""
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def make_repo(
    root: Path,
    name: str = "demo",
    runtime_script: str | None = PASSING_RUNTIME_SCRIPT,
) -> Project:
    """Build a disposable work repository with a bare remote under ``root``.

    The initial commit contains ``config.txt`` and the optional
    ``start_services.py`` runtime script, so the real engine executes the
    custom runtime path instead of any Compose/Docker runtime.
    """
    remote = root / "remotes" / f"{name}.git"
    remote.mkdir(parents=True)
    git("init", "--bare", "-b", "main", cwd=remote)

    work = root / "projects" / name
    work.mkdir(parents=True)
    git("init", "-b", "main", cwd=work)
    git("config", "user.email", "aipm@test", cwd=work)
    git("config", "user.name", "AIPM Test", cwd=work)
    (work / "config.txt").write_text("v1\n", encoding="utf-8")
    if runtime_script is not None:
        (work / "start_services.py").write_text(runtime_script, encoding="utf-8")
    git("add", ".", cwd=work)
    git("commit", "-m", "initial", cwd=work)
    git("remote", "add", "origin", str(remote), cwd=work)
    git("push", "-u", "origin", "main", cwd=work)
    return Project(name=name, path=str(work), capabilities=ProjectCapabilities(has_git=True))


def make_remote_commit(project: Project, filename: str, content: str) -> None:
    """Commit directly on the bare remote so the work repository diverges."""
    work = Path(project.path)
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    scratch = work.parent / "scratch" / uuid.uuid4().hex[:8]
    scratch.mkdir(parents=True)
    clone = scratch / "clone"
    git("clone", "--quiet", remote, str(clone), cwd=scratch)
    git("config", "user.email", "aipm@test", cwd=clone)
    git("config", "user.name", "AIPM Test", cwd=clone)
    (clone / filename).write_text(content, encoding="utf-8")
    git("add", filename, cwd=clone)
    git("commit", "-m", f"remote {filename}", cwd=clone)
    git("push", "origin", "main", cwd=clone)


def fetch_origin(project: Project) -> None:
    """Advance the origin tracking ref, as the engine's planned fetch would."""
    git("fetch", "origin", cwd=Path(project.path))


def rev_parse_head(project: Project) -> str:
    output = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return output.strip()


def stash_entries(project: Project) -> list[str]:
    output = subprocess.run(
        ["git", "stash", "list"],
        cwd=project.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in output.splitlines() if line.strip()]


def status_porcelain(project: Project) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=project.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def make_compose_project(
    root: Path,
    name: str = "stack",
    documents: dict[str, str] | None = None,
) -> Project:
    """Build a disposable on-disk Compose project layout (no Docker daemon)."""
    path = root / "projects" / name
    path.mkdir(parents=True)
    compose_files: list[str] = []
    for filename, content in (documents or {"compose.yaml": DEFAULT_COMPOSE_DOCUMENT}).items():
        target = path / filename
        target.write_text(content, encoding="utf-8")
        compose_files.append(str(target))
    return Project(
        name=name,
        path=str(path),
        capabilities=ProjectCapabilities(has_compose=True),
        compose_files=compose_files,
    )


class FixedProjectService:
    """Hermetic discovery seam: serve the fixture project with a live Git snapshot.

    The real ``ProjectService`` constructs an ``Application`` (config manager,
    logger) that writes real user-level state, so discovery is the single
    seam integration tests take. The Git model is refreshed through the real
    ``GitService`` on every lookup, so the planner, engine, and verifier all
    observe the repository's actual state at call time.
    """

    def __init__(self, project: Project, git_service: GitService | None = None):
        self.project = project
        self.git_service = git_service or GitService()

    def get_project(self, name: str) -> Project:
        if name != self.project.name:
            raise KeyError(f"Unknown project: {name}")
        return replace(self.project, git=self.git_service.repository(self.project))


class GuardCompose:
    """Tripwire: the custom runtime must run; Compose/Docker must not be contacted."""

    def up(self, *args, **kwargs):
        raise AssertionError(
            "Compose runtime must not be used when start_services.py exists "
            "(and no Docker daemon is guaranteed in tests)"
        )


def hermetic_health_engine() -> HealthEngine:
    """Health engine wired with only the hermetic Git analyzer.

    ``ComposeAnalyzer``/``DockerAnalyzer`` contact the real Docker socket once
    a project declares Compose capability, so integration runs exclude them;
    any other analyzer failure is contained by the engine as a warning.
    """
    return HealthEngine(analyzers=[GitAnalyzer()])


def build_engine(tmp_path: Path, project: Project) -> UpdateEngine:
    """Wire the real update pipeline with only the hermetic seams.

    Real components: ``UpdatePlanner``, ``UpdateEngine`` (real subprocess
    runner), ``GitTransactionRunner``, ``RollbackManager``,
    ``UpdateVerifier``, ``BackupEngine``, ``AuditService``, and the real
    ``GitService``/``GitProvider``. Seams: ``FixedProjectService``
    (discovery) and ``GuardCompose`` (runtime tripwire). All backups and
    audit records land under ``tmp_path``.
    """
    git_service = GitService()
    return UpdateEngine(
        project_service=FixedProjectService(project, git_service),
        git_service=git_service,
        backup_engine=BackupEngine(tmp_path / "backups"),
        compose_provider=GuardCompose(),
        health_engine=hermetic_health_engine(),
        audit_service=AuditService(tmp_path / "audit"),
        rollback_manager=RollbackManager(),
        verifier=UpdateVerifier(),
    )
