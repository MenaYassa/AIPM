from __future__ import annotations

from aipm.models.finding import Finding, Severity
from aipm.models.project import Project
from aipm.engines.health.analyzers.base import Analyzer


class GitAnalyzer(Analyzer):
    def analyze(self, project: Project) -> list[Finding]:
        if not project.capabilities.has_git or project.git is None:
            return []

        repository = project.git
        findings: list[Finding] = []
        if repository.detached:
            findings.append(
                Finding(
                    code="GIT_DETACHED_HEAD",
                    component="Git",
                    severity=Severity.WARNING,
                    title="Repository is in detached HEAD state",
                    description="The project is not currently checked out on a named branch.",
                    recommendation="Check out the intended deployment branch before updating.",
                )
            )
        if repository.conflicted_files:
            findings.append(
                Finding(
                    code="GIT_CONFLICTS",
                    component="Git",
                    severity=Severity.CRITICAL,
                    title="Unresolved merge conflicts detected",
                    description=f"{len(repository.conflicted_files)} file(s) contain unresolved conflicts.",
                    recommendation="Resolve all conflicts before running AIPM update operations.",
                )
            )
        elif repository.dirty:
            findings.append(
                Finding(
                    code="GIT_DIRTY",
                    component="Git",
                    severity=Severity.HIGH,
                    title="Uncommitted changes detected",
                    description="The working tree contains local changes or untracked files.",
                    recommendation="Commit or intentionally stash local changes before updating.",
                )
            )
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
