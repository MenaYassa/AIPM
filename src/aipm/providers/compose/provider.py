from __future__ import annotations

import shutil
import subprocess
from typing import Iterable

import docker

from aipm.core.exceptions import ProviderError
from aipm.mappers.docker import DockerMapper
from aipm.models.container import Container
from aipm.models.project import Project


class ComposeError(ProviderError):
    """Raised when a Compose operation cannot be completed."""


class ComposeProvider:
    """Adapter around the Docker Compose v2 CLI and Docker SDK."""

    def _compose_command(self, project: Project, *arguments: str) -> list[str]:
        if not project.compose_files:
            raise ComposeError(f"Project '{project.name}' has no Compose files defined.")
        if shutil.which("docker") is None:
            raise ComposeError("Docker CLI is not installed or is not available on PATH.")

        command = ["docker", "compose"]
        for compose_file in project.compose_files:
            command.extend(("-f", compose_file))
        command.extend(arguments)
        return command

    def _run_compose(self, project: Project, arguments: Iterable[str]) -> str:
        command = self._compose_command(project, *arguments)
        try:
            result = subprocess.run(
                command,
                cwd=project.path,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise ComposeError(f"Unable to execute Docker Compose: {exc}") from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "unknown Compose error"
            raise ComposeError(f"Compose command failed for '{project.name}': {detail}")
        return result.stdout.strip()

    def ps(self, project: Project) -> list[Container]:
        """Return containers labeled as belonging to the Compose project."""
        try:
            client = docker.from_env()
            containers = client.containers.list(
                all=True,
                filters={"label": f"com.docker.compose.project={project.name}"},
            )
            return [DockerMapper.container(container) for container in containers]
        except docker.errors.DockerException as exc:
            raise ComposeError(f"Unable to query Compose services for '{project.name}': {exc}") from exc

    def up(
        self,
        project: Project,
        detach: bool = True,
        build: bool = False,
        remove_orphans: bool = False,
    ) -> str:
        arguments = ["up"]
        if detach:
            arguments.append("--detach")
        if build:
            arguments.append("--build")
        if remove_orphans:
            arguments.append("--remove-orphans")
        return self._run_compose(project, arguments)

    def down(self, project: Project) -> str:
        return self._run_compose(project, ["down"])

    def pull(self, project: Project) -> str:
        return self._run_compose(project, ["pull"])
