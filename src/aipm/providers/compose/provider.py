# src/aipm/providers/compose/provider.py

from python_on_whales import DockerClient
from python_on_whales.exceptions import DockerException
from aipm.core.exceptions import ProviderError
from aipm.models.project import Project
from aipm.mappers.compose import ComposeMapper
from aipm.mappers.docker import DockerMapper
from aipm.models.container import Container

class ComposeError(ProviderError):
    pass

class ComposeProvider:
    def _get_client(self, project: Project) -> DockerClient:
        """Creates a scoped client for a specific project."""
        if not project.compose_files:
            raise ComposeError(f"Project '{project.name}' has no compose files defined.")
            
        # Change 'compose_project_dir' to 'compose_project_directory'
        return DockerClient(
            compose_project_directory=project.path, 
            compose_files=project.compose_files
        )

    def ps(self, project: Project) -> list[Container]:
        """Returns runtime containers belonging to this compose project."""

        from docker import from_env

        client = from_env()

        containers = client.containers.list(
            all=True,
            filters={
                "label": f"com.docker.compose.project={project.name}"
            },
        )

        return [
            DockerMapper.container(container)
            for container in containers
        ]

    def up(self, project: Project, detach: bool = True):
        try:
            client = self._get_client(project)
            client.compose.up(detach=detach)
        except DockerException as e:
            raise ComposeError(f"Failed to bring up {project.name}: {e}")

    def down(self, project: Project):
        try:
            client = self._get_client(project)
            client.compose.down()
        except DockerException as e:
            raise ComposeError(f"Failed to tear down {project.name}: {e}")

    def pull(self, project: Project):
        try:
            client = self._get_client(project)
            client.compose.pull()
        except DockerException as e:
            raise ComposeError(f"Failed to pull images for {project.name}: {e}")