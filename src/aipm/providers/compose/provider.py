# src/aipm/providers/compose/provider.py

from python_on_whales import DockerClient
from python_on_whales.exceptions import DockerException
from aipm.core.exceptions import ProviderError
from aipm.models.project import Project
from aipm.mappers.compose import ComposeMapper

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

    def ps(self, project: Project) -> list:
        """Returns a list of mapped ComposeService objects for the project."""
        try:
            client = self._get_client(project)
            # Add all=True to mimic `docker compose ps -a`
            raw_services = client.compose.ps(all=True) 
            return [ComposeMapper.map_service(s) for s in raw_services]
        except DockerException as e:
            raise ComposeError(f"Failed to list services for {project.name}: {e}")

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