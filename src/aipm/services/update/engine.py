from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from aipm.core.exceptions import GitTransactionError, UpdateError
from aipm.engines.health.engine import HealthEngine
from aipm.models.git_transaction import GitTransactionResult
from aipm.models.update import UpdateAudit, UpdatePlan
from aipm.models.rollback import RestoreResult
from aipm.models.verification import UpdateVerificationStatus
from aipm.providers.compose.provider import ComposeProvider
from aipm.services.backup.engine import BackupEngine
from aipm.services.git.service import GitService
from aipm.services.project.service import ProjectService
from aipm.services.update.audit import AuditService
from aipm.services.update.execution_contract import ExecutionContract
from aipm.services.update.git_transaction import GitTransactionRunner
from aipm.services.update.planner import UpdatePlanner
from aipm.services.update.plan_identity import UpdatePlanIdentity
from aipm.services.update.rollback import RollbackManager
from aipm.services.update.verifier import UpdateVerifier


class UpdateEngine:
    """Coordinate a planned update while keeping mutation behind providers/services.

    The engine is presentation-free: it produces typed outcomes
    (``UpdateAudit``) and structured errors; the CLI capability layer owns
    all Rich output.
    """

    def __init__(
        self,
        project_service: ProjectService | None = None,
        git_service: GitService | None = None,
        backup_engine: BackupEngine | None = None,
        compose_provider: ComposeProvider | None = None,
        health_engine: HealthEngine | None = None,
        planner: UpdatePlanner | None = None,
        audit_service: AuditService | None = None,
        rollback_manager: RollbackManager | None = None,
        verifier: UpdateVerifier | None = None,
        runner: Callable = subprocess.run,
        git_transaction: GitTransactionRunner | None = None,
    ):
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
        self.rollback_manager = rollback_manager or RollbackManager()
        self.verifier = verifier or UpdateVerifier()
        self.runner = runner
        self.git_transaction = git_transaction or GitTransactionRunner(git_service=self.git_service)

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

    def execute_update(
        self,
        project_name: str,
        *,
        dry_run: bool = False,
        approve: bool = False,
        plan: UpdatePlan | None = None,
        execution_contract: ExecutionContract | None = None,
    ) -> UpdateAudit:
        started_at = datetime.now(timezone.utc)
        if plan is None:
            plan = self.plan_update(project_name, dry_run=dry_run)
        else:
            self._validate_reused_plan(plan, project_name, dry_run=dry_run)

        # Composed control-plane path: an execution contract binds the plan to
        # the authorized confirmation. The binding is verified BEFORE any
        # runtime mutation (snapshot, git, services, health) is attempted.
        # Fail-closed: a supplied contract that is malformed, or whose plan
        # digest does not equal this plan's canonical identity digest, stops
        # execution here. The legacy CLI path (no contract) is unchanged.
        if execution_contract is not None:
            self._validate_execution_contract(execution_contract, plan)

        if dry_run:
            audit = self._audit(plan, started_at, "dry-run", "planned")
            audit_path = self.audit_service.write(audit)
            return _attach_audit_path(audit, audit_path)

        if not plan.proceed:
            audit = self._audit(plan, started_at, "execute", "blocked", error="Plan requires manual review.")
            audit_path = self.audit_service.write(audit)
            raise UpdateError("Update blocked: the plan requires manual review. No state was changed.")

        if plan.approval_required and not approve:
            audit = self._audit(plan, started_at, "execute", "approval_required", error="Explicit --yes approval was not provided.")
            audit_path = self.audit_service.write(audit)
            raise UpdateError(f"Explicit approval is required. Review the plan and rerun with --yes. Audit: {audit_path}")

        project = self.project_service.get_project(plan.project)
        snapshot_path: Path | None = None
        health_after = None
        verification = None
        git_transaction: GitTransactionResult | None = None
        try:
            archive = self.backup_engine.create_snapshot(project)
            snapshot_path = archive.archive_path

            git_transaction = self.git_transaction.run(
                project,
                stash_required=plan.stash_required,
                fetch_required=bool(plan.git and plan.git.exists and plan.git.remote_url),
                pull_required=plan.pull_required,
            )
            self._execute_runtime(project)
            refreshed = self.project_service.get_project(project.name)
            health_after = self.health_engine.analyze(refreshed)
            verification = self.verifier.verify_update(
                project.name,
                health_before=plan.health_before,
                health_after=health_after,
            )
            if verification.status is UpdateVerificationStatus.FAILURE:
                details = "; ".join([*verification.failures, *( [verification.error] if verification.error else [] )])
                raise UpdateError(
                    f"Post-update verification failed: {details}. "
                    f"Snapshot: {archive.archive_path}"
                )

            audit = self._audit(
                plan,
                started_at,
                "execute",
                "success",
                snapshot_path=snapshot_path,
                health_after=health_after,
                verification=verification,
                git_transaction=git_transaction,
            )
            audit_path = self.audit_service.write(audit)
            return _attach_audit_path(audit, audit_path)
        except Exception as exc:
            if isinstance(exc, GitTransactionError):
                git_transaction = exc.result
            restore: RestoreResult | None = None
            if snapshot_path is not None:
                restore = self.rollback_manager.restore(snapshot_path, project)
            audit = self._audit(
                plan,
                started_at,
                "execute",
                "failed",
                snapshot_path=snapshot_path,
                health_after=health_after,
                error=str(exc),
                restore=restore,
                verification=verification,
                git_transaction=git_transaction,
            )
            audit_path = self.audit_service.write(audit)
            if isinstance(exc, UpdateError):
                raise UpdateError(f"{exc} Audit: {audit_path}{_restore_note(restore)}") from exc
            raise UpdateError(f"Update failed: {exc}. Audit: {audit_path}{_restore_note(restore)}") from exc

    def _validate_reused_plan(self, plan: UpdatePlan, project_name: str, *, dry_run: bool) -> None:
        """Fail closed when a caller-supplied plan does not match this execution.

        The operator must see and approve exactly the plan that runs: the
        plan must target the requested project and must not be a dry-run
        plan executed as a state-changing update (or vice versa).
        """
        if plan.project != project_name:
            raise UpdateError(
                f"Refusing to execute plan for project '{plan.project}' against requested project '{project_name}'."
            )
        if plan.dry_run != dry_run:
            raise UpdateError(
                "Refusing to execute a plan whose dry-run flag does not match the requested mode "
                f"(plan dry_run={plan.dry_run}, requested dry_run={dry_run})."
            )

    def _validate_execution_contract(self, contract: ExecutionContract, plan: UpdatePlan) -> None:
        """Fail closed when an execution contract does not bind this exact plan.

        Integrity/binding check only: the engine remains a runtime-mechanics
        layer and re-derives no authorization. The contract must carry the
        project identity and the authorized plan digest; the digest is
        recomputed from the plan the engine is about to execute using the
        canonical :class:`UpdatePlanIdentity` derivation, and any missing or
        mismatched value refuses execution before any runtime mutation
        (snapshot, git transaction, services, health, verification) starts.
        """
        if not isinstance(contract, ExecutionContract):
            raise UpdateError("Refusing to execute under a malformed execution contract.")
        if not contract.project_name or contract.project_name != plan.project:
            raise UpdateError(
                "Refusing to execute: the execution contract is not bound to this project "
                f"(contract project '{contract.project_name or ''}', plan project '{plan.project}')."
            )
        expected_digest = UpdatePlanIdentity.from_plan(plan).digest()
        if not contract.plan_digest:
            raise UpdateError("Refusing to execute: the execution contract carries no plan digest.")
        if contract.plan_digest != expected_digest:
            raise UpdateError(
                "Refusing to execute: the execution contract was issued for a different plan "
                f"(contract digest {contract.plan_digest}, plan digest {expected_digest})."
            )

    def _execute_runtime(self, project) -> None:
        project_path = Path(project.path)
        custom_runner = project_path / "start_services.py"
        if custom_runner.is_file():
            self.run_command([sys.executable, str(custom_runner)], cwd=project_path, step_name="Custom runtime rebuild")
        elif project.capabilities.has_compose:
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
        restore: RestoreResult | None = None,
        verification=None,
        git_transaction: GitTransactionResult | None = None,
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
            restore=restore,
            verification=verification,
            git_transaction=git_transaction,
        )


def _attach_audit_path(audit: UpdateAudit, audit_path: Path) -> UpdateAudit:
    """Return an audit carrying where its JSON record was written.

    ``UpdateAudit`` is frozen; replace is used to attach the additive
    ``audit_path`` field without mutating the written record's other fields.
    """
    return replace(audit, audit_path=audit_path)


def _restore_note(restore: RestoreResult | None) -> str:
    """Operator-facing restore outcome for failed-update errors.

    The engine no longer prints; the restore outcome of a failed update is
    appended to the raised error so the CLI surface keeps reporting it.
    """
    if restore is None:
        return ""
    if restore.success:
        return (
            "; project files were restored from the pre-update snapshot "
            f"({len(restore.left_in_place)} file(s) created after the snapshot were left in place)"
        )
    return f"; automatic restore failed: {restore.error}"
