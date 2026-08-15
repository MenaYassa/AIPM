from __future__ import annotations

from aipm.models.health import HealthCheckResult, HealthState
from aipm.models.project import Project
from aipm.providers.compose.provider import ComposeError
from aipm.services.compose.service import ComposeService


class HealthService:
    """Compatibility service for component-level health checks."""

    def __init__(self, compose: ComposeService | None = None):
        self.compose = compose or ComposeService()

    def check_project(self, project: Project) -> list[HealthCheckResult]:
        results: list[HealthCheckResult] = []
        if project.capabilities.has_git and project.git is not None:
            if project.git.conflicted_files:
                results.append(HealthCheckResult(
                    component="Git Repository",
                    state=HealthState.CRITICAL,
                    message="Unresolved merge conflicts detected.",
                    details={"files": project.git.conflicted_files},
                ))
            elif project.git.dirty:
                results.append(HealthCheckResult(
                    component="Git Repository",
                    state=HealthState.DEGRADED,
                    message="Uncommitted changes detected. Auto-updates will be blocked.",
                    details={"modified_files": project.git.modified_files, "untracked_files": project.git.untracked_files},
                ))
            else:
                results.append(HealthCheckResult(
                    component="Git Repository",
                    state=HealthState.HEALTHY,
                    message="Clean working directory.",
                ))

        if project.capabilities.has_compose:
            try:
                status = self.compose.status(project)
                if not status.containers:
                    results.append(HealthCheckResult(
                        component="Compose Stack",
                        state=HealthState.UNKNOWN,
                        message="The project is down or no containers exist.",
                    ))
                else:
                    for container in status.containers:
                        state = HealthState.HEALTHY if container.state == "running" else HealthState.CRITICAL
                        if container.health == "unhealthy":
                            state = HealthState.DEGRADED
                        results.append(HealthCheckResult(
                            component=f"Container: {container.name}",
                            state=state,
                            message=f"Container state is '{container.state}'.",
                            details={"health": container.health, "image": container.image},
                        ))
            except ComposeError as exc:
                results.append(HealthCheckResult(
                    component="Compose Stack",
                    state=HealthState.UNKNOWN,
                    message=str(exc),
                ))
        return results