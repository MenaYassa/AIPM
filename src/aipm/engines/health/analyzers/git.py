from aipm.engines.health.analyzers.base import HealthAnalyzer
from aipm.models.finding import Finding
from aipm.models.project import Project


class GitAnalyzer(HealthAnalyzer):

    def analyze(self, project: Project) -> list[Finding]:

        findings: list[Finding] = []

        return findings