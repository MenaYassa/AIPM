from typing import Any

from aipm.models.compose import ComposeStatus
from aipm.models.compose_intelligence import ComposeProjectObservation
from aipm.models.project import Project
from aipm.providers.compose.provider import ComposeProvider


class ComposeService:

    def __init__(self, provider: ComposeProvider | None = None, intelligence: Any | None = None):
        self.provider = provider or ComposeProvider()
        self.intelligence = intelligence

    def status(self, project: Project) -> ComposeStatus:

        containers = self.provider.ps(project)

        running = sum(
            1 for c in containers
            if c.state == "running"
        )

        stopped = sum(
            1 for c in containers
            if c.state in (
                "exited",
                "dead",
                "created",
            )
        )

        restarting = sum(
            1 for c in containers
            if c.state == "restarting"
        )

        unhealthy = sum(
            1 for c in containers
            if c.health == "unhealthy"
        )

        return ComposeStatus(
            project_name=project.name,
            compose_files=project.compose_files,
            containers=containers,
            running=running,
            stopped=stopped,
            restarting=restarting,
            unhealthy=unhealthy,
        )

    def observe(self, project: Project, *, query_registries: bool = False) -> ComposeProjectObservation:
        """Perform a complete, read-only service and image observation."""
        if self.intelligence is not None:
            return self.intelligence.observe(project, query_registries=query_registries)
        from aipm.services.compose.intelligence import ComposeIntelligenceService

        intelligence = ComposeIntelligenceService(compose_provider=self.provider)
        return intelligence.observe(project, query_registries=query_registries)