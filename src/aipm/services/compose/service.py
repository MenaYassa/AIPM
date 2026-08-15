from aipm.models.compose import ComposeStatus
from aipm.models.project import Project
from aipm.providers.compose.provider import ComposeProvider


class ComposeService:

    def __init__(self):

        self.provider = ComposeProvider()

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