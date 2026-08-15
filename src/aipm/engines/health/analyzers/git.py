from aipm.engines.health.analyzers.base import Analyzer
from aipm.models.finding import Finding
from aipm.models.project import Project


class GitAnalyzer(Analyzer):

    def analyze(self, project: Project) -> list[Finding]:

        findings: list[Finding] = []

        return findings