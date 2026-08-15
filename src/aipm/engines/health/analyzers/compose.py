from __future__ import annotations

from aipm.engines.health.analyzers.base import Analyzer
from aipm.models.finding import Finding, Severity
from aipm.models.project import Project
from aipm.providers.compose.provider import ComposeError
from aipm.services.compose.service import ComposeService


class ComposeAnalyzer(Analyzer):
    def __init__(self, compose_service: ComposeService | None = None):
        self.compose = compose_service or ComposeService()

    def analyze(self, project: Project) -> list[Finding]:
        if not project.capabilities.has_compose:
            return []

        try:
            status = self.compose.status(project)
        except ComposeError as exc:
            return [
                Finding(
                    code="COMPOSE_UNAVAILABLE",
                    component="Compose",
                    severity=Severity.HIGH,
                    title="Compose runtime is unavailable",
                    description=str(exc),
                    recommendation="Start Docker and verify the Compose CLI before retrying.",
                )
            ]

        findings: list[Finding] = []
        if status.running == 0:
            findings.append(
                Finding(
                    code="COMPOSE_DOWN",
                    component="Compose",
                    severity=Severity.HIGH,
                    title="Compose stack is down",
                    description="No running containers were found for this project.",
                    recommendation="Start the stack using 'aipm compose up'.",
                )
            )
        if status.restarting:
            findings.append(
                Finding(
                    code="CONTAINERS_RESTARTING",
                    component="Compose",
                    severity=Severity.HIGH,
                    title="Restarting containers detected",
                    description=f"{status.restarting} container(s) are restarting.",
                    recommendation="Inspect logs and container health checks before updating.",
                )
            )
        if status.unhealthy:
            findings.append(
                Finding(
                    code="UNHEALTHY_CONTAINERS",
                    component="Compose",
                    severity=Severity.WARNING,
                    title="Unhealthy containers detected",
                    description=f"{status.unhealthy} unhealthy container(s).",
                    recommendation="Inspect the failing health checks and dependent services.",
                )
            )
        if status.stopped and status.running:
            findings.append(
                Finding(
                    code="STOPPED_CONTAINERS",
                    component="Compose",
                    severity=Severity.WARNING,
                    title="Stopped containers detected",
                    description=f"{status.stopped} container(s) are stopped while the stack is partially running.",
                    recommendation="Confirm that stopped services are intentionally disabled.",
                )
            )
        return findings
