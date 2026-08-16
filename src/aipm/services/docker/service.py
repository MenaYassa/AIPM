from aipm.mappers.docker import DockerMapper
from aipm.providers.docker.provider import DockerProvider


class DockerService:
    def __init__(self, provider: DockerProvider | None = None):
        self._provider = provider

    @property
    def provider(self) -> DockerProvider:
        if self._provider is None:
            self._provider = DockerProvider()
        return self._provider

    def ps(self):
        return [DockerMapper.container(container) for container in self.provider.list_containers()]

    def inspect(self, name: str):
        return self.provider.inspect(name)

    def start(self, name: str) -> None:
        self.provider.start(name)

    def stop(self, name: str) -> None:
        self.provider.stop(name)

    def restart(self, name: str) -> None:
        self.provider.restart(name)

    def images(self) -> list[dict]:
        return [DockerMapper.image_view(image) for image in self.provider.images()]

    def volumes(self) -> list[dict]:
        return [DockerMapper.volume_view(volume) for volume in self.provider.volumes()]

    def networks(self) -> list[dict]:
        return [DockerMapper.network_view(network) for network in self.provider.networks()]

    def logs(self, name: str, tail: int = 100) -> str:
        return self.provider.logs(name, tail)
