from abc import ABC
from abc import abstractmethod

from aipm.models.finding import Finding
from aipm.models.project import Project


class Analyzer(ABC):

    @abstractmethod
    def analyze(self, project: Project) -> list[Finding]:
        raise NotImplementedError