from aipm.engines.health.analyzers.compose import ComposeAnalyzer
from aipm.engines.health.analyzers.docker import DockerAnalyzer
from aipm.engines.health.analyzers.git import GitAnalyzer
from aipm.engines.health.report_builder import ReportBuilder

from aipm.models.health_report import HealthReport
from aipm.models.project import Project


class HealthEngine:

    def __init__(self):

        self.analyzers = [
            GitAnalyzer(),
            ComposeAnalyzer(),
            DockerAnalyzer(),
        ]

        self.builder = ReportBuilder()

    def analyze(self, project: Project) -> HealthReport:

        findings = []

        for analyzer in self.analyzers:
            findings.extend(
                analyzer.analyze(project)
            )

        return self.builder.build(
            project,
            findings,
        )