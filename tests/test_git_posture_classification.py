import pytest
from aipm.models.finding import Severity
from aipm.models.git import GitRepository
from aipm.models.project import Project, ProjectCapabilities
from aipm.models.update import UpdateRisk
from aipm.engines.health.analyzers.git import GitAnalyzer
from aipm.services.git.posture import GitPostureLevel, classify_git_posture
from aipm.services.git.service import GitService


def test_classify_posture_conflicts():
    repo = GitRepository(exists=True, dirty=True, conflicted_files=["main.py"])
    assessment = classify_git_posture(repo)
    assert assessment.level == GitPostureLevel.CONFLICTS
    assert assessment.finding_severity == Severity.CRITICAL
    assert assessment.risk == UpdateRisk.BLOCKED
    assert assessment.proceed is False
    assert assessment.review_required is True
    assert assessment.stash_required is False


def test_classify_posture_detached_head():
    repo = GitRepository(exists=True, detached=True)
    assessment = classify_git_posture(repo)
    assert assessment.level == GitPostureLevel.UNSAFE
    assert assessment.finding_severity == Severity.HIGH
    assert assessment.risk == UpdateRisk.BLOCKED
    assert assessment.proceed is False
    assert assessment.review_required is True


def test_classify_posture_critical_file_modified():
    repo = GitRepository(exists=True, dirty=True, modified_files=["docker-compose.yml"])
    assessment = classify_git_posture(repo)
    assert assessment.level == GitPostureLevel.CRITICAL_MODIFIED
    assert assessment.finding_severity == Severity.HIGH
    assert assessment.risk == UpdateRisk.BLOCKED
    assert assessment.proceed is False
    assert assessment.review_required is True


def test_classify_posture_tracked_non_critical_modified():
    repo = GitRepository(exists=True, dirty=True, modified_files=["src/app.py"])
    assessment = classify_git_posture(repo)
    assert assessment.level == GitPostureLevel.TRACKED_MODIFIED
    assert assessment.finding_severity == Severity.WARNING
    assert assessment.risk == UpdateRisk.MEDIUM
    assert assessment.proceed is True
    assert assessment.stash_required is True
    assert assessment.review_required is False


def test_classify_posture_benign_untracked():
    # Simulates config/aipm-drill-transport.yaml
    repo = GitRepository(exists=True, dirty=True, untracked_files=["config/aipm-drill-transport.yaml"])
    assessment = classify_git_posture(repo)
    assert assessment.level == GitPostureLevel.BENIGN_UNTRACKED
    assert assessment.finding_severity == Severity.WARNING
    assert assessment.risk == UpdateRisk.LOW
    assert assessment.proceed is True
    assert assessment.stash_required is True
    assert assessment.review_required is False


def test_classify_posture_clean():
    repo = GitRepository(exists=True, dirty=False)
    assessment = classify_git_posture(repo)
    assert assessment.level == GitPostureLevel.CLEAN
    assert assessment.finding_severity == Severity.INFO
    assert assessment.risk == UpdateRisk.LOW
    assert assessment.proceed is True
    assert assessment.stash_required is False


def test_analyzer_and_service_parity_for_benign_untracked(monkeypatch):
    """GitAnalyzer and GitService must agree: benign untracked does not emit HIGH severity and allows update."""
    repo = GitRepository(
        exists=True,
        dirty=True,
        branch="main",
        untracked_files=["config/aipm-drill-transport.yaml"],
    )
    project = Project(
        name="aipm",
        path="/home/ubuntu/aipm",
        capabilities=ProjectCapabilities(has_git=True),
        git=repo,
    )

    # 1. Check GitAnalyzer findings
    analyzer = GitAnalyzer()
    findings = analyzer.analyze(project)
    # Must NOT produce Severity.HIGH
    high_findings = [f for f in findings if f.severity == Severity.HIGH]
    critical_findings = [f for f in findings if f.severity == Severity.CRITICAL]
    assert len(high_findings) == 0
    assert len(critical_findings) == 0
    # Must produce Severity.WARNING
    warning_findings = [f for f in findings if f.severity == Severity.WARNING]
    assert len(warning_findings) == 1
    assert warning_findings[0].code == "GIT_DIRTY_UNTRACKED"

    # 2. Check GitService.prepare_update
    service = GitService()
    monkeypatch.setattr(service, "repository", lambda p: repo)
    plan = service.prepare_update(project)
    assert plan.proceed is True
    assert plan.stash_required is True
    assert plan.review_required is False
