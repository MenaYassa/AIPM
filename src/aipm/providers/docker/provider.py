# src/aipm/providers/docker/provider.py
import docker
from aipm.core.exceptions import ContainerNotFound, DockerError

class DockerProvider:
    def __init__(self):
        self.client = docker.from_env()

    def list_containers(self):
        return self.client.containers.list(all=True)
        
    def inspect(self, name: str):
        try:
            return self.client.containers.get(name)
        except docker.errors.NotFound:
            raise ContainerNotFound(f"Container '{name}' not found.")
        except docker.errors.APIError as e:
            raise DockerError(f"Docker API error: {e}")
            
    def start(self, name: str):
        container = self.client.containers.get(name)
        container.start()

    def stop(self, name: str):
        container = self.client.containers.get(name)
        container.stop()

    def restart(self, name: str):
        container = self.client.containers.get(name)
        container.restart()

    def images(self):
        try:
            return self.client.images.list()
        except docker.errors.APIError as e:
            raise DockerError(f"Docker API error fetching images: {e}")

    def volumes(self):
        try:
            return self.client.volumes.list()
        except docker.errors.APIError as e:
            raise DockerError(f"Docker API error fetching volumes: {e}")

    def networks(self):
        try:
            return self.client.networks.list()
        except docker.errors.APIError as e:
            raise DockerError(f"Docker API error fetching networks: {e}")

    def logs(self, name: str, tail: int = 100):
        try:
            container = self.client.containers.get(name)
            # Decode bytes to string
            return container.logs(tail=tail).decode("utf-8", errors="replace")
        except docker.errors.NotFound:
            raise ContainerNotFound(f"Container '{name}' not found.")
        except docker.errors.APIError as e:
            raise DockerError(f"Docker API error fetching logs: {e}")