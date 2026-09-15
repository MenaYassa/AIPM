from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from aipm.models.finding import Finding, Severity
from aipm.models.git import GitRepository
from aipm.models.update import UpdateRisk
from aipm.services.git.conflicts import ConflictAnalyzer


class GitPostureLevel(Enum):
    CONFLICTS = "conflicts"
    UNSAFE = "unsafe"
    CRITICAL_MODIFIED = "critical_modified"
    TRACKED_MODIFIED = "tracked_modified"
    BENIGN_UNTRACKED = "benign_untracked"
    CLEAN = "clean"


@dataclass(frozen=True, slots=True)
class GitPostureAssessment:
    level: GitPostureLevel
    finding_severity: Severity
    risk: UpdateRisk
    proceed: bool
    review_required: bool
    stash_required: bool
    reasons: tuple[str, ...]
    critical_files: tuple[str, ...]
    normal_files: tuple[str, ...]
    findings: tuple[Finding, ...]


def classify_git_posture(repository: GitRepository | None) -> GitPostureAssessment:
    """Canonical shared classification for Git repository update posture.

    Consumed by both GitAnalyzer (health) and GitService.prepare_update() (planner)
    to ensure health findings and update decisions cannot drift or disagree.
    """
    if repository is None or not repository.exists:
        finding = Finding(
            code="GIT_NOT_FOUND",
            component="Git",
            severity=Severity.HIGH,
            title="Repository not found",
            description="Project is not a Git repository.",
            recommendation="Initialize or clone the repository.",
        )
        return GitPostureAssessment(
            level=GitPostureLevel.UNSAFE,
            finding_severity=Severity.HIGH,
            risk=UpdateRisk.BLOCKED,
            proceed=False,
            review_required=True,
            stash_required=False,
            reasons=("Project is not a Git repository.",),
            critical_files=(),
            normal_files=(),
            findings=(finding,),
        )

    if repository.conflicted_files:
        count = len(repository.conflicted_files)
        finding = Finding(
            code="GIT_CONFLICTS",
            component="Git",
            severity=Severity.CRITICAL,
            title="Unresolved merge conflicts detected",
            description=f"{count} file(s) contain unresolved conflicts.",
            recommendation="Resolve all conflicts before running AIPM update operations.",
        )
        return GitPostureAssessment(
            level=GitPostureLevel.CONFLICTS,
            finding_severity=Severity.CRITICAL,
            risk=UpdateRisk.BLOCKED,
            proceed=False,
            review_required=True,
            stash_required=False,
            reasons=(f"Unresolved merge conflicts in {count} file(s).",),
            critical_files=tuple(sorted(repository.conflicted_files)),
            normal_files=(),
            findings=(finding,),
        )

    if repository.detached:
        finding = Finding(
            code="GIT_DETACHED_HEAD",
            component="Git",
            severity=Severity.HIGH,
            title="Repository is in detached HEAD state",
            description="The project is not currently checked out on a named branch.",
            recommendation="Check out the intended deployment branch before updating.",
        )
        return GitPostureAssessment(
            level=GitPostureLevel.UNSAFE,
            finding_severity=Severity.HIGH,
            risk=UpdateRisk.BLOCKED,
            proceed=False,
            review_required=True,
            stash_required=False,
            reasons=("Repository is in detached HEAD state.",),
            critical_files=(),
            normal_files=(),
            findings=(finding,),
        )

    if repository.dirty:
        all_dirty = list(dict.fromkeys(repository.modified_files + repository.untracked_files))
        classified = ConflictAnalyzer().classify(all_dirty)
        critical = tuple(sorted(classified["critical"]))
        normal = tuple(sorted(classified["normal"]))

        if critical:
            finding = Finding(
                code="GIT_DIRTY_CRITICAL",
                component="Git",
                severity=Severity.HIGH,
                title="Critical infrastructure files modified",
                description=f"The working tree modifies critical infrastructure files: {', '.join(critical)}.",
                recommendation="Revert or review changes to critical files before updating.",
            )
            return GitPostureAssessment(
                level=GitPostureLevel.CRITICAL_MODIFIED,
                finding_severity=Severity.HIGH,
                risk=UpdateRisk.BLOCKED,
                proceed=False,
                review_required=True,
                stash_required=False,
                reasons=(f"Critical infrastructure files modified: {', '.join(critical)}.",),
                critical_files=critical,
                normal_files=normal,
                findings=(finding,),
            )

        if repository.modified_files:
            finding = Finding(
                code="GIT_DIRTY_TRACKED",
                component="Git",
                severity=Severity.WARNING,
                title="Uncommitted changes in tracked files",
                description="Non-critical tracked files are modified; AIPM can preserve them in a safety stash.",
                recommendation="Review or commit changes before updating.",
            )
            return GitPostureAssessment(
                level=GitPostureLevel.TRACKED_MODIFIED,
                finding_severity=Severity.WARNING,
                risk=UpdateRisk.MEDIUM,
                proceed=True,
                review_required=False,
                stash_required=True,
                reasons=("Uncommitted changes detected in non-critical files; AIPM can preserve them in a safety stash.",),
                critical_files=(),
                normal_files=normal,
                findings=(finding,),
            )

        if repository.untracked_files:
            finding = Finding(
                code="GIT_DIRTY_UNTRACKED",
                component="Git",
                severity=Severity.WARNING,
                title="Untracked non-critical files detected",
                description="Non-critical untracked files detected in working tree.",
                recommendation="Untracked non-critical files can be preserved in a safety stash.",
            )
            return GitPostureAssessment(
                level=GitPostureLevel.BENIGN_UNTRACKED,
                finding_severity=Severity.WARNING,
                risk=UpdateRisk.LOW,
                proceed=True,
                review_required=False,
                stash_required=True,
                reasons=("Uncommitted changes detected in non-critical files; AIPM can preserve them in a safety stash.",),
                critical_files=(),
                normal_files=normal,
                findings=(finding,),
            )

    return GitPostureAssessment(
        level=GitPostureLevel.CLEAN,
        finding_severity=Severity.INFO,
        risk=UpdateRisk.LOW,
        proceed=True,
        review_required=False,
        stash_required=False,
        reasons=(),
        critical_files=(),
        normal_files=(),
        findings=(),
    )
