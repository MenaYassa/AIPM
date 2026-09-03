from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from aipm.core.decorators import cli_handler
from aipm.models.update import UpdateAudit, UpdatePlan
from aipm.services.update.engine import UpdateEngine


class UpdateCapability:
    """Presentation layer for the update command.

    Owns all Rich rendering for the update flow: the plan shown for operator
    review before execution, and the typed outcome after it. The engine
    itself stays presentation-free.
    """

    def __init__(self, engine: UpdateEngine | None = None, console: Console | None = None):
        self.engine = engine or UpdateEngine()
        self.console = console or Console()

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

    def render_outcome(self, audit: UpdateAudit) -> None:
        if audit.outcome == "planned":
            self.console.print(f"[green]Dry-run complete; no state was changed.[/green] Audit: {audit.audit_path}")
            return
        if audit.outcome == "success":
            if audit.verification is not None and audit.verification.warnings:
                self.console.print(
                    f"[yellow]Update verified with warnings:[/yellow] {len(audit.verification.warnings)} "
                    "warning-level finding(s); no rollback required."
                )
            self.console.print(
                f"[bold green]Update completed and health verification passed.[/bold green] Audit: {audit.audit_path}"
            )
            return
        if audit.restore is not None:
            if audit.restore.success:
                self.console.print(
                    "[yellow]Update failed; project files were restored from the pre-update snapshot.[/yellow] "
                    f"Files created after the snapshot were left in place: {len(audit.restore.left_in_place)}."
                )
            else:
                self.console.print(f"[red]Automatic restore failed:[/red] {audit.restore.error}")

    @cli_handler(action_name="aipm update")
    def run(self, project_name: str, *, dry_run: bool = False, approve: bool = False) -> UpdateAudit:
        """Plan, render for review, and (when approved) execute an update."""
        plan = self.engine.plan_update(project_name, dry_run=dry_run)
        self.render_plan(plan)
        audit = self.engine.execute_update(project_name, dry_run=dry_run, approve=approve, plan=plan)
        self.render_outcome(audit)
        return audit
