from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aipm.models.finding import Finding, Severity
from aipm.models.health import HealthState
from aipm.models.health_report import HealthReport
from aipm.models.project import Project
from aipm.models.update import UpdatePlan, UpdateRisk
from aipm.models.verification import UpdateVerification, UpdateVerificationStatus
from aipm.services.backup.engine import BackupEngine
from aipm.services.project.service import ProjectService
from aipm.services.update.audit import AuditService
from aipm.services.update.engine import UpdateEngine
from aipm.services.update.rollback import RollbackManager
from aipm.services.update.verifier import UpdateVerifier


class FakeProjectService(ProjectService):
    def __init__(self, project: Project):
        self.project = project

    def get_project(self, name: str) -> Project:
        assert name == self.project.name
        return self.project


class FakeGitService:
    def prepare_update(self, project):
        class Plan:
            proceed = True
            approval_required = True
            stash_required = False
            pull_required = False
            git = None
            risk = UpdateRisk.MEDIUM
            reasons: list[str] = []
            actions: list[str] = []
            snapshot_required = True
            estimated_restart = True

        return Plan()


class FakeCompose:
    def up(self, *args, **kwargs):
        raise AssertionError("compose must not be used when start_services.py exists")


def make_finding(severity: Severity, title: str = "finding", component: str = "Demo") -> Finding:
    return Finding(
        code="TEST",
        component=component,
        severity=severity,
        title=title,
        description="test finding",
        recommendation="none",
    )


def report(severity: Severity | None) -> HealthReport:
    findings = [] if severity is None else [make_finding(severity)]
    return HealthReport(
        project="demo",
        score=100 if severity is None else 60,
        state=HealthState.HEALTHY if severity is None else HealthState.DEGRADED,
        findings=findings,
    )


class FixedHealthEngine:
    def __init__(self, reports: list[HealthReport]):
        self.reports = list(reports)
        self.calls: list[object] = []

    def analyze(self, project):
        self.calls.append(project)
        return self.reports.pop(0)


class RecordingVerifier(UpdateVerifier):
    def __init__(self, verdict: UpdateVerification):
        super().__init__()
        self.verdict = verdict
        self.calls: list[dict] = []

    def verify_update(self, project_name, *, health_before, health_after):
        self.calls.append({"project": project_name, "before": health_before, "after": health_after})
        return self.verdict


def make_project(root: Path, name: str = "demo") -> Project:
    project_path = root / name
    project_path.mkdir(parents=True)
    (project_path / "start_services.py").write_text("print('ok')\n", encoding="utf-8")
    return Project(name=name, path=str(project_path))


def healthy_plan(project: Project, *, dry_run: bool = False, health_before: HealthReport | None = None) -> UpdatePlan:
    return UpdatePlan(
        project=project.name,
        project_path=project.path,
        dry_run=dry_run,
        proceed=True,
        approval_required=True,
        risk=UpdateRisk.MEDIUM,
        actions=["Create a configuration safety snapshot"],
        snapshot_required=True,
        estimated_restart=True,
        health_before=health_before or report(None),
    )


class FixedPlanner:
    def __init__(self, plan: UpdatePlan):
        self._plan = plan

    def plan(self, name, dry_run=False):
        return self._plan


def ok_runner(command, **kwargs):
    return SimpleNamespace(returncode=0, stdout="ok", stderr="")


def failing_runner(command, **kwargs):
    return SimpleNamespace(returncode=1, stdout="", stderr="boom")


def build_engine(
    tmp_path: Path,
    project: Project,
    runner,
    verifier: UpdateVerifier,
    health_engine=None,
    rollback_manager=None,
):
    return UpdateEngine(
        project_service=FakeProjectService(project),
        git_service=FakeGitService(),
        backup_engine=BackupEngine(tmp_path / "backups"),
        compose_provider=FakeCompose(),
        health_engine=health_engine or FixedHealthEngine([report(None)]),
        planner=FixedPlanner(healthy_plan(project)),
        audit_service=AuditService(tmp_path / "audit"),
        rollback_manager=rollback_manager or RollbackManager(),
        verifier=verifier,
        runner=runner,
    )


# --- verifier unit semantics -------------------------------------------------


def test_verifier_success_when_no_warning_or_worse_findings(tmp_path: Path):
    project = make_project(tmp_path)
    verifier = UpdateVerifier()
    result = verifier.verify_update("demo", health_before=report(None), health_after=report(None))
    assert result.status is UpdateVerificationStatus.SUCCESS
    assert result.update_successful is True
    assert result.rollback_required is False
    assert result.warnings == []
    assert result.failures == []


def test_verifier_warning_for_high_and_warning_findings_without_critical(tmp_path: Path):
    project = make_project(tmp_path)
    verifier = UpdateVerifier()
    health_after = HealthReport(
        project="demo",
        score=60,
        state=HealthState.DEGRADED,
        findings=[make_finding(Severity.HIGH, "high finding"), make_finding(Severity.WARNING, "warn finding")],
    )
    result = verifier.verify_update("demo", health_before=report(None), health_after=health_after)
    assert result.status is UpdateVerificationStatus.WARNING
    # Roadmap: warnings are not rollback conditions; the update is still successful.
    assert result.update_successful is True
    assert result.rollback_required is False
    assert len(result.warnings) == 2
    assert "high finding" in result.warnings[0]
    assert "warn finding" in result.warnings[1]


def test_verifier_failure_for_critical_findings(tmp_path: Path):
    project = make_project(tmp_path)
    verifier = UpdateVerifier()
    health_after = HealthReport(
        project="demo",
        score=50,
        state=HealthState.CRITICAL,
        findings=[make_finding(Severity.CRITICAL, "critical finding")],
    )
    result = verifier.verify_update("demo", health_before=report(None), health_after=health_after)
    assert result.status is UpdateVerificationStatus.FAILURE
    assert result.update_successful is False
    assert result.rollback_required is True
    assert "critical finding" in result.failures[0]


def test_verifier_failure_when_health_after_missing(tmp_path: Path):
    project = make_project(tmp_path)
    verifier = UpdateVerifier()
    result = verifier.verify_update("demo", health_before=report(None), health_after=None)
    assert result.status is UpdateVerificationStatus.FAILURE
    assert result.rollback_required is True
    assert "missing" in (result.error or "")


def test_verifier_unexpected_error_fails_safe(tmp_path: Path):
    project = make_project(tmp_path)

    class ExplodingVerifier(UpdateVerifier):
        def _verify(self, health_before, health_after):
            raise RuntimeError("analyzer exploded")

    result = ExplodingVerifier().verify_update(
        "demo", health_before=report(None), health_after=report(None)
    )
    assert result.status is UpdateVerificationStatus.FAILURE
    assert result.rollback_required is True
    assert "could not establish" in (result.error or "")


def test_verifier_is_read_only_against_project_state(tmp_path: Path):
    project = make_project(tmp_path)
    before_snapshot = sorted(str(p.relative_to(project.path)) for p in Path(project.path).rglob("*"))
    verifier = UpdateVerifier()
    verifier.verify_update("demo", health_before=report(None), health_after=report(None))
    after_snapshot = sorted(str(p.relative_to(project.path)) for p in Path(project.path).rglob("*"))
    assert before_snapshot == after_snapshot
    assert (Path(project.path) / "start_services.py").read_text(encoding="utf-8") == "print('ok')\n"


# --- engine integration --------------------------------------------------------


def test_engine_consumes_success_verdict_and_audits_it(tmp_path: Path):
    project = make_project(tmp_path)
    verdict = UpdateVerification(status=UpdateVerificationStatus.SUCCESS, passed=["Demo: info"])
    verifier = RecordingVerifier(verdict)
    engine = build_engine(tmp_path, project, ok_runner, verifier)

    audit = engine.execute_update("demo", approve=True)

    assert audit.outcome == "success"
    assert audit.verification is verdict
    assert len(verifier.calls) == 1
    assert verifier.calls[0]["after"] is audit.health_after
    payload = json.loads(next(iter((tmp_path / "audit").glob("*.json"))).read_text(encoding="utf-8"))
    assert payload["outcome"] == "success"
    assert payload["verification"]["status"] == "success"
    assert payload["verification"]["passed"] == ["Demo: info"]


def test_engine_warning_verdict_is_success_without_rollback(tmp_path: Path):
    project = make_project(tmp_path)
    verdict = UpdateVerification(
        status=UpdateVerificationStatus.WARNING,
        warnings=["Demo: degraded (warning)"],
    )
    verifier = RecordingVerifier(verdict)
    rollback = RollbackManager()
    engine = build_engine(tmp_path, project, ok_runner, verifier, rollback_manager=rollback)

    audit = engine.execute_update("demo", approve=True)

    assert audit.outcome == "success"
    assert audit.verification is verdict
    payload = json.loads(next(iter((tmp_path / "audit").glob("*.json"))).read_text(encoding="utf-8"))
    assert payload["verification"]["status"] == "warning"
    assert payload["verification"]["warnings"] == ["Demo: degraded (warning)"]
    assert payload["restore"] is None


def test_engine_verification_failure_enters_rollback_path_and_audits_all_three(tmp_path: Path):
    project = make_project(tmp_path)
    (Path(project.path) / "config.txt").write_text("v1", encoding="utf-8")

    def mutating_runner(command, **kwargs):
        (Path(project.path) / "config.txt").write_text("v2", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    verdict = UpdateVerification(
        status=UpdateVerificationStatus.FAILURE,
        failures=["Demo: container down (critical)"],
    )
    verifier = RecordingVerifier(verdict)
    engine = build_engine(tmp_path, project, mutating_runner, verifier)

    with pytest.raises(Exception, match="Post-update verification failed"):
        engine.execute_update("demo", approve=True)

    # verification failure entered the same rollback path as an execution failure
    assert (Path(project.path) / "config.txt").read_text(encoding="utf-8") == "v1"
    payload = json.loads(next(iter((tmp_path / "audit").glob("*.json"))).read_text(encoding="utf-8"))
    assert payload["outcome"] == "failed"
    # audit preserves execution result, verification result, and restore result distinctly
    assert payload["verification"]["status"] == "failure"
    assert "container down" in payload["verification"]["failures"][0]
    assert payload["restore"]["success"] is True
    assert "config.txt" in payload["restore"]["restored"]
    assert "Post-update verification failed" in payload["error"]


def test_engine_success_message_only_when_verifier_says_so(tmp_path: Path):
    """The engine must not declare success on its own; it consumes the verdict."""
    project = make_project(tmp_path)
    verdict = UpdateVerification(status=UpdateVerificationStatus.SUCCESS)
    verifier = RecordingVerifier(verdict)
    engine = build_engine(tmp_path, project, ok_runner, verifier)

    audit = engine.execute_update("demo", approve=True)

    assert audit.verification is not None
    assert audit.verification.status is UpdateVerificationStatus.SUCCESS


def test_verifier_independent_of_planner_and_executor(tmp_path: Path):
    """The verifier is a distinct component: planner and executor fakes are unused."""
    project = make_project(tmp_path)

    class NeverCalledPlanner:
        def plan(self, *args, **kwargs):
            raise AssertionError("planner must not be involved in verification")

    class NeverCalledRuntime:
        def up(self, *args, **kwargs):
            raise AssertionError("executor must not be involved in verification")

    verdict = UpdateVerifier().verify_update(
        "demo",
        health_before=report(None),
        health_after=report(None),
    )
    assert verdict.status is UpdateVerificationStatus.SUCCESS
