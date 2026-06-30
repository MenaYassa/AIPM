from datetime import datetime
from aipm.models.container import Container
from aipm.providers.docker.provider import DockerProvider
from aipm.mappers.docker import DockerMapper

class DockerService:
    def __init__(self):
        self.provider = DockerProvider()

    def ps(self):
        # The list comprehension is now highly readable
        return [DockerMapper.container(c) for c in self.provider.list_containers()]
      
    def inspect(self, name: str):
        return self.provider.inspect(name)

    def start(self, name: str):
        self.provider.start(name)

    def stop(self, name: str):
        self.provider.stop(name)

    def restart(self, name: str):
        self.provider.restart(name)

    def images(self):
        return self.provider.images()

    def volumes(self):
        return self.provider.volumes()

    def networks(self):
        return self.provider.networks()

    def images(self):
        return [DockerMapper.image_view(i) for i in self.provider.images()]

    def volumes(self):
        return [DockerMapper.volume_view(v) for v in self.provider.volumes()]

    def networks(self):
        return [DockerMapper.network_view(n) for n in self.provider.networks()]

    def logs(self, name: str, tail: int = 100):
        return self.provider.logs(name, tail)