from __future__ import annotations

from pathlib import Path

from aipm.engines.health.engine import HealthEngine
from aipm.models.update import UpdatePlan, UpdateRisk
from aipm.services.git.service import GitService
from aipm.services.project.service import ProjectService


class UpdatePlanner:
    """Build an update plan without changing project or host state."""

    def __init__(
        self,
        project_service: ProjectService | None = None,
        git_service: GitService | None = None,
        health_engine: HealthEngine | None = None,
    ):
        self.project_service = project_service or ProjectService()
        self.git_service = git_service or GitService()
        self.health_engine = health_engine or HealthEngine()

    def plan(self, project_name: str, dry_run: bool = False) -> UpdatePlan:
        project = self.project_service.get_project(project_name)
        project_path = Path(project.path)
        reasons: list[str] = []
        actions: list[str] = ["Create a configuration safety snapshot"]
        risk = UpdateRisk.LOW
        proceed = True
        stash_required = False
        pull_required = False
        git_snapshot = project.git

        health_before = self.health_engine.analyze(project)
        if health_before.critical:
            risk = UpdateRisk.HIGH
            reasons.append(f"Health-before report contains {health_before.critical} critical finding(s).")
        elif health_before.high:
            risk = max_risk(risk, UpdateRisk.MEDIUM)
            reasons.append(f"Health-before report contains {health_before.high} high-severity finding(s).")

        if project.capabilities.has_git:
            git_plan = self.git_service.prepare_update(project)
            git_snapshot = self.git_service.repository(project)
            stash_required = git_plan.stash_required
            pull_required = git_plan.pull_required
            reasons.extend(git_plan.reasons)
            if git_plan.review_required or not git_plan.proceed:
                proceed = False
                risk = UpdateRisk.BLOCKED
                reasons.append("Git state requires manual review before an update can proceed.")
            if git_plan.fetch_required:
                actions.append("Fetch the configured Git remote")
            if stash_required:
                actions.append("Create a named safety stash for non-critical local changes")
            if pull_required:
                actions.append("Pull the remote tracking branch")

        custom_runner = project_path / "start_services.py"
        if custom_runner.is_file():
            actions.append("Run the project start_services.py orchestration script")
        elif project.capabilities.has_compose:
            actions.append("Rebuild and start the project Compose services")
        else:
            proceed = False
            risk = UpdateRisk.BLOCKED
            reasons.append("Project has neither start_services.py nor a Compose configuration.")

        if project.capabilities.has_compose or custom_runner.is_file():
            actions.append("Verify health after the update")
            risk = max_risk(risk, UpdateRisk.MEDIUM)
        else:
            actions.append("No runtime action is available")

        return UpdatePlan(
            project=project.name,
            project_path=str(project_path),
            dry_run=dry_run,
            proceed=proceed,
            approval_required=proceed and not dry_run,
            risk=risk,
            reasons=deduplicate(reasons),
            actions=deduplicate(actions),
            snapshot_required=True,
            estimated_restart=project.capabilities.has_compose or custom_runner.is_file(),
            stash_required=stash_required,
            pull_required=pull_required,
            git=git_snapshot,
            health_before=health_before,
        )


def max_risk(current: UpdateRisk, candidate: UpdateRisk) -> UpdateRisk:
    order = {
        UpdateRisk.LOW: 0,
        UpdateRisk.MEDIUM: 1,
        UpdateRisk.HIGH: 2,
        UpdateRisk.BLOCKED: 3,
    }
    return candidate if order[candidate] > order[current] else current


def deduplicate(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
