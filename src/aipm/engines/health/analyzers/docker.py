from __future__ import annotations

from aipm.engines.health.analyzers.base import Analyzer
from aipm.models.finding import Finding, Severity
from aipm.models.project import Project
from aipm.providers.compose.provider import ComposeError
from aipm.services.compose.service import ComposeService


class DockerAnalyzer(Analyzer):
    def __init__(self, compose_service: ComposeService | None = None):
        self.compose = compose_service or ComposeService()

    def analyze(self, project: Project) -> list[Finding]:
        if not project.capabilities.has_compose:
            return []
        try:
            containers = self.compose.status(project).containers
        except ComposeError:
            return []

        findings: list[Finding] = []
        for container in containers:
            if container.state in {"exited", "dead"}:
                findings.append(
                    Finding(
                        code="CONTAINER_STOPPED",
                        component="Docker",
                        severity=Severity.HIGH,
                        title=f"Container '{container.name}' is stopped",
                        description=f"Container state is '{container.state}'.",
                        recommendation=f"Inspect logs with 'aipm docker logs {container.name}'.",
                        resource=container.name,
                    )
                )
            if container.health == "unhealthy":
                findings.append(
                    Finding(
                        code="CONTAINER_UNHEALTHY",
                        component="Docker",
                        severity=Severity.WARNING,
                        title=f"Container '{container.name}' is unhealthy",
                        description="The container health check is reporting unhealthy.",
                        recommendation=f"Inspect the health check and logs for {container.name}.",
                        resource=container.name,
                    )
                )
        return findings
