from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from aipm.core.exceptions import UpdateError
from aipm.engines.health.engine import HealthEngine
from aipm.models.update import UpdateAudit, UpdatePlan
from aipm.providers.compose.provider import ComposeProvider
from aipm.services.backup.engine import BackupEngine
from aipm.services.git.service import GitService
from aipm.services.project.service import ProjectService
from aipm.services.update.audit import AuditService
from aipm.services.update.planner import UpdatePlanner


class UpdateEngine:
    """Coordinate a planned update while keeping mutation behind providers/services."""

    def __init__(
        self,
        project_service: ProjectService | None = None,
        git_service: GitService | None = None,
        backup_engine: BackupEngine | None = None,
        compose_provider: ComposeProvider | None = None,
        health_engine: HealthEngine | None = None,
        planner: UpdatePlanner | None = None,
        audit_service: AuditService | None = None,
        console: Console | None = None,
        runner: Callable = subprocess.run,
    ):
        self.console = console or Console()
        self.project_service = project_service or ProjectService()
        self.git_service = git_service or GitService()
        self.backup_engine = backup_engine or BackupEngine()
        self.compose_provider = compose_provider or ComposeProvider()
        self.health_engine = health_engine or HealthEngine()
        self.planner = planner or UpdatePlanner(
            project_service=self.project_service,
            git_service=self.git_service,
            health_engine=self.health_engine,
        )
        self.audit_service = audit_service or AuditService()
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

    def plan_update(self, project_name: str, dry_run: bool = False) -> UpdatePlan:
        return self.planner.plan(project_name, dry_run=dry_run)

    def render_plan(self, plan: UpdatePlan) -> None:
        self.console.print(
            Panel.fit(
                "\n".join(
                    [
                        f"Project       : {plan.project}",
                        f"Risk          : {plan.risk.value.upper()}",
                        f"Proceed       : {'yes' if plan.proceed else 'no'}",
                        f"Approval      : {'required' if plan.approval_required else 'not required'}",
                        f"Snapshot      : {'required' if plan.snapshot_required else 'not required'}",
                        f"Restart       : {'yes' if plan.estimated_restart else 'no'}",
                    ]
                ),
                title="Update Plan",
            )
        )
        actions = Table(title="Planned actions")
        actions.add_column("#", justify="right")
        actions.add_column("Action")
        for index, action in enumerate(plan.actions, start=1):
            actions.add_row(str(index), action)
        self.console.print(actions)
        if plan.reasons:
            reasons = Table(title="Reasons and safety notes")
            reasons.add_column("Reason")
            for reason in plan.reasons:
                reasons.add_row(reason)
            self.console.print(reasons)

    def execute_update(self, project_name: str, *, dry_run: bool = False, approve: bool = False) -> UpdateAudit:
        started_at = datetime.now(timezone.utc)
        plan = self.plan_update(project_name, dry_run=dry_run)
        self.render_plan(plan)

        if dry_run:
            audit = self._audit(plan, started_at, "dry-run", "planned")
            audit_path = self.audit_service.write(audit)
            self.console.print(f"[green]Dry-run complete; no state was changed.[/green] Audit: {audit_path}")
            return audit

        if not plan.proceed:
            audit = self._audit(plan, started_at, "execute", "blocked", error="Plan requires manual review.")
            self.audit_service.write(audit)
            raise UpdateError("Update blocked: the plan requires manual review. No state was changed.")

        if plan.approval_required and not approve:
            audit = self._audit(plan, started_at, "execute", "approval_required", error="Explicit --yes approval was not provided.")
            audit_path = self.audit_service.write(audit)
            raise UpdateError(f"Explicit approval is required. Review the plan and rerun with --yes. Audit: {audit_path}")

        project = self.project_service.get_project(plan.project)
        snapshot_path: Path | None = None
        health_after = None
        try:
            archive = self.backup_engine.create_snapshot(project)
            snapshot_path = archive.archive_path
            self.console.print(f"[green]Snapshot created:[/green] {archive.archive_path}")

            stash_created = False
            if plan.stash_required:
                self.git_service.stash(project, f"AIPM update {started_at.isoformat()}")
                stash_created = True
            try:
                if plan.git and plan.git.exists and plan.git.remote_url:
                    self.git_service.fetch(project)
                    if plan.pull_required:
                        self.git_service.pull(project)
                self._execute_runtime(project)
                if stash_created:
                    try:
                        self.git_service.apply_stash(project)
                    except Exception as exc:
                        raise UpdateError(
                            "Local safety stash could not be applied cleanly; it was preserved for manual recovery. "
                            f"Resolve the conflict before retrying: {exc}"
                        ) from exc
                    self.git_service.drop_stash(project)
            except Exception:
                raise

            refreshed = self.project_service.get_project(project.name)
            health_after = self.health_engine.analyze(refreshed)
            if health_after.critical:
                details = "; ".join(
                    f"{finding.component}: {finding.title}" for finding in health_after.findings if finding.severity.value == "critical"
                )
                raise UpdateError(
                    f"Post-update health verification failed: {details}. "
                    f"Snapshot: {archive.archive_path}"
                )

            audit = self._audit(plan, started_at, "execute", "success", snapshot_path=snapshot_path, health_after=health_after)
            audit_path = self.audit_service.write(audit)
            self.console.print(f"[bold green]Update completed and health verification passed.[/bold green] Audit: {audit_path}")
            return audit
        except Exception as exc:
            audit = self._audit(
                plan,
                started_at,
                "execute",
                "failed",
                snapshot_path=snapshot_path,
                health_after=health_after,
                error=str(exc),
            )
            audit_path = self.audit_service.write(audit)
            if isinstance(exc, UpdateError):
                raise UpdateError(f"{exc} Audit: {audit_path}") from exc
            raise UpdateError(f"Update failed: {exc}. Audit: {audit_path}") from exc

    def _execute_runtime(self, project) -> None:
        project_path = Path(project.path)
        custom_runner = project_path / "start_services.py"
        if custom_runner.is_file():
            self.console.print("[cyan]Running project start_services.py...[/cyan]")
            self.run_command([sys.executable, str(custom_runner)], cwd=project_path, step_name="Custom runtime rebuild")
        elif project.capabilities.has_compose:
            self.console.print("[cyan]Rebuilding Compose services...[/cyan]")
            self.compose_provider.up(project, detach=True, build=True, remove_orphans=True)
        else:
            raise UpdateError("Project has neither start_services.py nor a Compose configuration.")

    def _audit(
        self,
        plan: UpdatePlan,
        started_at: datetime,
        mode: str,
        outcome: str,
        *,
        snapshot_path: Path | None = None,
        health_after=None,
        error: str | None = None,
    ) -> UpdateAudit:
        return UpdateAudit(
            project=plan.project,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            mode=mode,
            outcome=outcome,
            risk=plan.risk,
            plan=plan,
            snapshot_path=snapshot_path,
            health_after=health_after,
            error=error,
        )
