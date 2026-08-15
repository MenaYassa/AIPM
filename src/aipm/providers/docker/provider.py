from __future__ import annotations

from typing import Any

import docker

from aipm.core.exceptions import ContainerNotFound, DockerError


class DockerProvider:
    def __init__(self, client: Any | None = None):
        if client is not None:
            self.client = client
            return
        try:
            self.client = docker.from_env()
        except docker.errors.DockerException as exc:
            raise DockerError(f"Docker is unavailable: {exc}") from exc

    def _container(self, name: str):
        try:
            return self.client.containers.get(name)
        except docker.errors.NotFound as exc:
            raise ContainerNotFound(f"Container '{name}' not found.") from exc
        except docker.errors.DockerException as exc:
            raise DockerError(f"Unable to access Docker container '{name}': {exc}") from exc

    def list_containers(self):
        try:
            return self.client.containers.list(all=True)
        except docker.errors.DockerException as exc:
            raise DockerError(f"Docker is unavailable: {exc}") from exc

    def inspect(self, name: str):
        return self._container(name)

    def start(self, name: str) -> None:
        try:
            self._container(name).start()
        except docker.errors.DockerException as exc:
            raise DockerError(f"Unable to start container '{name}': {exc}") from exc

    def stop(self, name: str) -> None:
        try:
            self._container(name).stop()
        except docker.errors.DockerException as exc:
            raise DockerError(f"Unable to stop container '{name}': {exc}") from exc

    def restart(self, name: str) -> None:
        try:
            self._container(name).restart()
        except docker.errors.DockerException as exc:
            raise DockerError(f"Unable to restart container '{name}': {exc}") from exc

    def images(self):
        try:
            return self.client.images.list()
        except docker.errors.DockerException as exc:
            raise DockerError(f"Docker error while fetching images: {exc}") from exc

    def volumes(self):
        try:
            return self.client.volumes.list()
        except docker.errors.DockerException as exc:
            raise DockerError(f"Docker error while fetching volumes: {exc}") from exc

    def networks(self):
        try:
            return self.client.networks.list()
        except docker.errors.DockerException as exc:
            raise DockerError(f"Docker error while fetching networks: {exc}") from exc

    def logs(self, name: str, tail: int = 100) -> str:
        if tail < 0:
            raise DockerError("Log tail must be zero or greater.")
        try:
            return self._container(name).logs(tail=tail).decode("utf-8", errors="replace")
        except docker.errors.DockerException as exc:
            raise DockerError(f"Docker error while fetching logs: {exc}") from exc
