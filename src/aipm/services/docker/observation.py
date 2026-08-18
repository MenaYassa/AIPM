"""Read-only Docker observation operations for Mission Control."""

from __future__ import annotations

from typing import Any

from aipm.services.docker.service import DockerService


class DockerObservationService:
    """Expose only Docker observation operations to dashboard façades."""

    def __init__(self, docker_service: DockerService) -> None:
        self._docker = docker_service

    def containers(self) -> list[Any]:
        return self._docker.provider.list_containers()

    def container(self, identifier: str) -> Any:
        return self._docker.provider.inspect(identifier)

    def images(self) -> list[Any]:
        return self._docker.provider.images()

    def volumes(self) -> list[Any]:
        return self._docker.provider.volumes()

    def networks(self) -> list[Any]:
        return self._docker.provider.networks()
