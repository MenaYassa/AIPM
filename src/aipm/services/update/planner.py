from __future__ import annotations

from pathlib import Path

import yaml

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

    def _analyze_compose_runtime(self, compose_files: list[str]) -> tuple[list[str], list[str]]:
        """Statically preflight the Compose runtime action.

        Read-only and deterministic: parses the declared Compose files from
        disk (no Docker daemon, no external processes, no network) and
        reports the services a rebuild would affect. Returns (actions,
        reasons). Missing and unparseable files fail closed into blocking
        reasons.
        """
        actions: list[str] = []
        reasons: list[str] = []
        services: list[str] = []
        parseable = 0
        for compose_file in compose_files:
            path = Path(compose_file)
            if not path.is_file():
                reasons.append(f"Declared Compose file is missing: {compose_file}")
                continue
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                reasons.append(f"Declared Compose file is not readable YAML: {compose_file}: {exc}")
                continue
            if not isinstance(document, dict):
                reasons.append(f"Declared Compose file is not a Compose document: {compose_file}")
                continue
            parseable += 1
            raw_services = document.get("services")
            if isinstance(raw_services, dict):
                services.extend(name for name in raw_services if isinstance(name, str))

        if not parseable:
            reasons.append("No declared Compose file could be analyzed; the Compose runtime action is not covered.")
            actions.append("No Compose runtime action is available")
            return actions, reasons

        if services:
            unique = sorted(dict.fromkeys(services))
            actions.append(f"Rebuild and start the project Compose services ({', '.join(unique)})")
        else:
            actions.append("Rebuild and start the project Compose services (no services declared)")
            reasons.append("Declared Compose files define no services; nothing will be rebuilt.")
        return actions, reasons

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
            if not project.compose_files:
                proceed = False
                risk = UpdateRisk.BLOCKED
                reasons.append(
                    "Project is marked as Compose-capable but declares no Compose files; "
                    "the runtime action cannot be planned."
                )
            else:
                runtime_actions, runtime_reasons = self._analyze_compose_runtime(project.compose_files)
                actions.extend(runtime_actions)
                reasons.extend(runtime_reasons)
                if any("not covered" in reason for reason in runtime_reasons):
                    proceed = False
                    risk = UpdateRisk.BLOCKED
                    reasons.append("Compose runtime action could not be analyzed; manual review is required.")
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
