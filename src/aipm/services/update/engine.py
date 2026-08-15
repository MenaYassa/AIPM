from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

from rich.console import Console

from aipm.core.exceptions import UpdateError
from aipm.providers.compose.provider import ComposeProvider
from aipm.services.backup.engine import BackupEngine
from aipm.services.health.service import HealthService
from aipm.services.project.service import ProjectService


class UpdateEngine:
    def __init__(
        self,
        project_service: ProjectService | None = None,
        backup_engine: BackupEngine | None = None,
        compose_provider: ComposeProvider | None = None,
        health_service: HealthService | None = None,
        console: Console | None = None,
        runner: Callable = subprocess.run,
    ):
        self.console = console or Console()
        self.project_service = project_service or ProjectService()
        self.backup_engine = backup_engine or BackupEngine()
        self.compose_provider = compose_provider or ComposeProvider()
        self.health_service = health_service or HealthService()
        self.runner = runner

    def run_command(self, command: list[str], cwd: Path, step_name: str) -> str:
        try:
            result = self.runner(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise UpdateError(f"Step '{step_name}' could not start: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "unknown error"
            raise UpdateError(f"Step '{step_name}' failed: {detail}")
        return result.stdout.strip()

    def execute_update(self, project_name: str) -> None:
        project = self.project_service.get_project(project_name)
        project_path = Path(project.path)
        self.console.print(f"[bold magenta]Starting update for:[/bold magenta] {project.name}")

        if project.capabilities.has_git and project.git is not None:
            if project.git.conflicted_files:
                raise UpdateError("Update blocked: the project has unresolved Git conflicts.")
            if project.git.dirty:
                raise UpdateError("Update blocked: the project has uncommitted or untracked changes.")
            self.console.print(f"[dim]Git branch: {project.git.branch or 'detached HEAD'}[/dim]")
        else:
            self.console.print("[dim]Git: not present; treating this as a static Compose project.[/dim]")

        try:
            archive = self.backup_engine.create_snapshot(project)
        except Exception as exc:
            raise UpdateError(f"Pre-update snapshot failed: {exc}") from exc
        self.console.print(f"[green]Snapshot created:[/green] {archive.archive_path}")

        try:
            custom_runner = project_path / "start_services.py"
            if custom_runner.is_file():
                self.console.print("[cyan]Running project start_services.py...[/cyan]")
                self.run_command([sys.executable, str(custom_runner)], cwd=project_path, step_name="Custom runtime rebuild")
            elif project.capabilities.has_compose:
                self.console.print("[cyan]Rebuilding Compose services...[/cyan]")
                self.compose_provider.up(project, detach=True, build=True, remove_orphans=True)
            else:
                raise UpdateError("Project has neither start_services.py nor a Compose configuration.")
        except Exception as exc:
            rollback_note = f"The pre-update snapshot is available at {archive.archive_path}."
            if isinstance(exc, UpdateError):
                raise UpdateError(f"{exc} {rollback_note}") from exc
            raise UpdateError(f"Deployment failed: {exc} {rollback_note}") from exc

        refreshed = self.project_service.get_project(project.name)
        checks = self.health_service.check_project(refreshed)
        critical = [check for check in checks if check.state.value == "critical"]
        if critical:
            details = "; ".join(f"{check.component}: {check.message}" for check in critical)
            raise UpdateError(f"Post-update health verification failed: {details}. Snapshot: {archive.archive_path}")
        self.console.print("[bold green]Update completed and post-update checks passed.[/bold green]")
