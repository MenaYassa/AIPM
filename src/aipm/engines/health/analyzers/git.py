from __future__ import annotations

from aipm.models.finding import Finding, Severity
from aipm.models.project import Project
from aipm.engines.health.analyzers.base import Analyzer
from aipm.services.git.posture import classify_git_posture


class GitAnalyzer(Analyzer):
    def analyze(self, project: Project) -> list[Finding]:
        if not project.capabilities.has_git or project.git is None:
            return []

        repository = project.git
        assessment = classify_git_posture(repository)
        findings: list[Finding] = list(assessment.findings)

        if repository.behind:
            findings.append(
                Finding(
                    code="GIT_BEHIND",
                    component="Git",
                    severity=Severity.WARNING,
                    title="Local branch is behind its remote",
                    description=f"The branch is {repository.behind} commit(s) behind origin.",
                    recommendation="Fetch and review remote changes before deployment.",
                )
            )
        if repository.ahead:
            findings.append(
                Finding(
                    code="GIT_AHEAD",
                    component="Git",
                    severity=Severity.INFO,
                    title="Local branch contains unpublished commits",
                    description=f"The branch is {repository.ahead} commit(s) ahead of origin.",
                    recommendation="Confirm that local commits are intended for this environment.",
                )
            )
        return findings
