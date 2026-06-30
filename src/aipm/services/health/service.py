from aipm.models.project import Project
from aipm.models.health import HealthCheckResult, HealthState
from aipm.providers.compose.provider import ComposeProvider

class HealthService:
    def __init__(self):
        self.compose = ComposeProvider()

    def check_project(self, project: Project) -> list[HealthCheckResult]:
        """Runs a suite of health checks on a specific project."""
        results = []
        
        # 1. Check Git State (If it's dirty, it's degraded because updates are blocked)
        if project.capabilities.has_git:
            if project.git_dirty:
                results.append(HealthCheckResult(
                    component="Git Repository",
                    state=HealthState.DEGRADED,
                    message="Uncommitted changes detected. Auto-updates will be blocked."
                ))
            else:
                results.append(HealthCheckResult(
                    component="Git Repository",
                    state=HealthState.HEALTHY,
                    message="Clean working directory."
                ))

        # 2. Check Compose Services
        if project.capabilities.has_compose:
            try:
                services = self.compose.ps(project)
                if not services:
                    results.append(HealthCheckResult(
                        component="Compose Stack",
                        state=HealthState.UNKNOWN,
                        message="Project is currently down or no services exist."
                    ))
                else:
                    for s in services:
                        if s.state == "running":
                            state = HealthState.HEALTHY
                        elif s.state in ["exited", "dead", "restarting"]:
                            state = HealthState.CRITICAL
                        else:
                            state = HealthState.UNKNOWN
                            
                        results.append(HealthCheckResult(
                            component=f"Service: {s.name}",
                            state=state,
                            message=f"Container state is '{s.state}'"
                        ))
            except Exception as e:
                results.append(HealthCheckResult(
                    component="Compose Stack",
                    state=HealthState.CRITICAL,
                    message=f"Failed to query services: {e}"
                ))
                
        return results