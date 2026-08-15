from __future__ import annotations

from aipm.engines.health.analyzers.compose import ComposeAnalyzer
from aipm.engines.health.analyzers.docker import DockerAnalyzer
from aipm.engines.health.analyzers.git import GitAnalyzer
from aipm.engines.health.report_builder import ReportBuilder
from aipm.models.finding import Finding, Severity
from aipm.models.health_report import HealthReport
from aipm.models.project import Project


class HealthEngine:
    def __init__(self, analyzers=None, builder: ReportBuilder | None = None):
        self.analyzers = analyzers or [GitAnalyzer(), ComposeAnalyzer(), DockerAnalyzer()]
        self.builder = builder or ReportBuilder()

    def analyze(self, project: Project) -> HealthReport:
        findings: list[Finding] = []
        for analyzer in self.analyzers:
            try:
                findings.extend(analyzer.analyze(project))
            except Exception as exc:  # analyzer failures must not hide the rest of the report
                findings.append(
                    Finding(
                        code="ANALYZER_FAILED",
                        component=type(analyzer).__name__,
                        severity=Severity.WARNING,
                        title="Health analyzer failed",
                        description=str(exc),
                        recommendation="Review the AIPM log and resolve the analyzer dependency or runtime error.",
                    )
                )
        return self.builder.build(project, findings)