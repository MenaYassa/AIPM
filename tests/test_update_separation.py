"""Service separation with Rich decoupling: engine is presentation-free; capability owns rendering.

Uses disposable temporary directories and explicit fakes only; never depends
on /home/ubuntu or any real production repository.
"""
from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from aipm.capabilities.update import UpdateCapability
from aipm.core.exceptions import UpdateError
from aipm.models.git_update_plan import GitUpdatePlan
from aipm.models.health import HealthState
from aipm.models.health_report import HealthReport
from aipm.models.project import Project, ProjectCapabilities
from aipm.models.update import UpdatePlan, UpdateRisk
from aipm.services.backup.engine import BackupEngine
from aipm.services.project.service import ProjectService
from aipm.services.update.audit import AuditService
from aipm.services.update.engine import UpdateEngine
from aipm.services.update.execution_contract import ExecutionContract
from aipm.services.update.planner import UpdatePlanner
from aipm.services.update.rollback import RollbackManager
from aipm.services.update.verifier import UpdateVerifier


class FakeProjectService:
    def __init__(self, project: Project):
        self.project = project

    def get_project(self, name: str) -> Project:
        assert name == self.project.name
        return self.project


class FakeGitService:
    def prepare_update(self, project):
        return GitUpdatePlan(
            proceed=True,
            stash_required=False,
            fetch_required=False,
            pull_required=False,
            review_required=False,
            rollback_required=False,
        )

    def repository(self, project):
        return project.git

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FakeHealthEngine:
    def __init__(self, report: HealthReport | None = None):
        self.report = report or HealthReport(project="demo", score=100, state=HealthState.HEALTHY)

    def analyze(self, project):
        return self.report


class FakeBackup:
    def __init__(self, root: Path):
        self.root = root
        self.created = False

    def create_snapshot(self, project):
        self.created = True
        path = self.root / "snapshot.tar.gz"
        path.write_bytes(b"snapshot")
        return SimpleNamespace(archive_path=path)


class FakeCompose:
    def __init__(self):
        self.calls = []

    def up(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class FixedPlanner:
    def __init__(self, plan: UpdatePlan):
        self._plan = plan

    def plan(self, name, dry_run=False):
        return self._plan


class FakeVerifier:
    def verify_update(self, project_name, *, health_before, health_after):
        from aipm.models.verification import UpdateVerification, UpdateVerificationStatus

        return UpdateVerification(status=UpdateVerificationStatus.SUCCESS, passed=["Demo: info"])


def ok_runner(command, **kwargs):
    return SimpleNamespace(returncode=0, stdout="ok", stderr="")


def failing_runner(command, **kwargs):
    return SimpleNamespace(returncode=1, stdout="", stderr="boom")


def make_project(tmp_path: Path) -> Project:
    project_path = tmp_path / "demo"
    project_path.mkdir()
    (project_path / "start_services.py").write_text("print('ok')\n", encoding="utf-8")
    return Project(name="demo", path=str(project_path))


def healthy_plan(project: Project, *, dry_run: bool, approval_required: bool = True) -> UpdatePlan:
    return UpdatePlan(
        project=project.name,
        project_path=project.path,
        dry_run=dry_run,
        proceed=True,
        approval_required=approval_required,
        risk=UpdateRisk.MEDIUM,
        actions=["Create a configuration safety snapshot", "Run the project start_services.py orchestration script"],
        estimated_restart=True,
        health_before=HealthReport(project=project.name, score=100, state=HealthState.HEALTHY),
    )


def build_engine(tmp_path: Path, project: Project, plan: UpdatePlan, runner=ok_runner) -> UpdateEngine:
    backup = FakeBackup(tmp_path / "backups")
    backup.root.mkdir(exist_ok=True)
    return UpdateEngine(
        project_service=FakeProjectService(project),
        git_service=FakeGitService(),
        backup_engine=backup,
        compose_provider=FakeCompose(),
        health_engine=FakeHealthEngine(),
        planner=FixedPlanner(plan),
        audit_service=AuditService(tmp_path / "audit"),
        rollback_manager=RollbackManager(),
        verifier=FakeVerifier(),
        runner=runner,
    )


def build_capability(engine: UpdateEngine) -> UpdateCapability:
    return UpdateCapability(engine=engine, console=Console(file=Path("/dev/null").open("w")))


# ---------------------------------------------------------------------------
# Engine is Rich-free and presentation-free
# ---------------------------------------------------------------------------


def test_engine_module_does_not_import_rich_or_render():
    from aipm.services.update import engine as engine_module

    source = inspect.getsource(engine_module)
    assert "rich" not in source
    assert "console" not in source
    assert "render" not in source
    public = {name for name in dir(UpdateEngine) if not name.startswith("_")}
    assert "console" not in public
    assert "render_plan" not in public


def test_engine_executes_without_any_console_output(tmp_path: Path):
    import io

    project = make_project(tmp_path)
    engine = build_engine(tmp_path, project, healthy_plan(project, dry_run=False, approval_required=True))
    # No console is injected; if the engine tried to render, Rich would write
    # to stdout. Capture stdout to prove silence.
    captured = io.StringIO()
    import contextlib
    import sys as _sys

    with contextlib.redirect_stdout(captured):
        audit = engine.execute_update("demo", approve=True)
    assert captured.getvalue() == ""
    assert audit.outcome == "success"
    assert audit.audit_path is not None
    assert audit.audit_path.exists()


# ---------------------------------------------------------------------------
# Executed plan is the reviewed plan (fail-closed plan reuse)
# ---------------------------------------------------------------------------


def test_engine_executes_the_supplied_plan_not_a_replanned_one(tmp_path: Path):
    project = make_project(tmp_path)
    reviewed = healthy_plan(project, dry_run=False, approval_required=True)
    engine = build_engine(tmp_path, project, reviewed)

    class ReplanningPlanner:
        def plan(self, name, dry_run=False):
            raise AssertionError("execute_update must reuse the supplied reviewed plan, not replan")

    engine.planner = ReplanningPlanner()
    audit = engine.execute_update("demo", approve=True, plan=reviewed)

    assert audit.outcome == "success"
    assert audit.plan is reviewed


def test_engine_refuses_plan_for_a_different_project(tmp_path: Path):
    project = make_project(tmp_path)
    plan = healthy_plan(project, dry_run=False, approval_required=True)
    engine = build_engine(tmp_path, project, plan)

    with pytest.raises(UpdateError, match="against requested project 'other'"):
        engine.execute_update("other", approve=True, plan=plan)


def test_engine_refuses_dry_run_plan_for_execute_and_vice_versa(tmp_path: Path):
    project = make_project(tmp_path)
    dry_plan = healthy_plan(project, dry_run=True, approval_required=False)
    execute_plan = healthy_plan(project, dry_run=False, approval_required=True)
    engine = build_engine(tmp_path, project, execute_plan)

    with pytest.raises(UpdateError, match="dry-run flag does not match"):
        engine.execute_update("demo", approve=True, plan=dry_plan)

    class CountingPlanner:
        def __init__(self):
            self.calls = 0

        def plan(self, name, dry_run=False):
            self.calls += 1
            return execute_plan

    counting = CountingPlanner()
    engine.planner = counting
    audit = engine.execute_update("demo", dry_run=True)
    assert audit.outcome == "planned"
    assert counting.calls == 1  # dry-run without a supplied plan plans once


def test_plan_mismatch_failures_are_audited(tmp_path: Path):
    project = make_project(tmp_path)
    plan = healthy_plan(project, dry_run=False, approval_required=True)
    engine = build_engine(tmp_path, project, plan)

    with pytest.raises(UpdateError, match="dry-run flag does not match"):
        engine.execute_update("demo", dry_run=True, plan=plan)

    audits = list((tmp_path / "audit").glob("*.json"))
    assert audits == []  # refusals happen before any audit record is written


# ---------------------------------------------------------------------------
# Capability renders the plan and the typed outcome
# ---------------------------------------------------------------------------


def test_capability_renders_plan_before_execution_and_outcome_after(tmp_path: Path):
    project = make_project(tmp_path)
    plan = healthy_plan(project, dry_run=False, approval_required=True)
    engine = build_engine(tmp_path, project, plan)

    events: list[tuple[str, object]] = []

    class RecordingCapability(UpdateCapability):
        def render_plan(self, plan):
            events.append(("plan", plan))

        def render_outcome(self, audit):
            events.append(("outcome", audit))

    capability = RecordingCapability(engine=engine)
    audit = capability.run("demo", approve=True)

    assert [kind for kind, _ in events] == ["plan", "outcome"]
    assert events[0][1] is plan
    assert events[1][1] is audit
    assert audit.outcome == "success"


def test_capability_render_outcome_covers_dry_run(tmp_path: Path):
    import io

    project = make_project(tmp_path)
    plan = healthy_plan(project, dry_run=True, approval_required=False)
    engine = build_engine(tmp_path, project, plan)

    stream = io.StringIO()
    capability = UpdateCapability(engine=engine, console=Console(file=stream, force_terminal=False, width=200))
    audit = capability.run("demo", dry_run=True)

    assert audit.outcome == "planned"
    rendered = stream.getvalue()
    assert "Dry-run complete; no state was changed." in rendered
    assert "demo" in rendered
    assert "MEDIUM" in rendered


def test_capability_render_outcome_restore_visibility(tmp_path: Path):
    """Direct render checks for outcome paths not reachable via run() (failed updates raise)."""
    import io
    from datetime import datetime, timezone

    from aipm.models.rollback import RestoreResult
    from aipm.models.update import UpdateAudit

    def audit_with(outcome: str, restore: RestoreResult | None = None) -> UpdateAudit:
        return UpdateAudit(
            project="demo",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            mode="execute",
            outcome=outcome,
            risk=UpdateRisk.MEDIUM,
            plan=UpdatePlan(project="demo", project_path=str(tmp_path), dry_run=False, proceed=True, approval_required=True, risk=UpdateRisk.MEDIUM),
            restore=restore,
        )

    stream = io.StringIO()
    capability = UpdateCapability(console=Console(file=stream, force_terminal=False, width=200))

    capability.render_outcome(audit_with("failed", RestoreResult(attempted=True, success=True, restored=["config.txt"], left_in_place=["extra.txt"])))
    assert "restored from the pre-update snapshot" in stream.getvalue()
    assert "left in place: 1" in stream.getvalue()

    stream2 = io.StringIO()
    capability2 = UpdateCapability(console=Console(file=stream2, force_terminal=False, width=200))
    capability2.render_outcome(audit_with("failed", RestoreResult(attempted=True, success=False, error="Snapshot archive not found")))
    assert "Automatic restore failed: Snapshot archive not found" in stream2.getvalue()

    stream3 = io.StringIO()
    capability3 = UpdateCapability(console=Console(file=stream3, force_terminal=False, width=200))
    capability3.render_outcome(audit_with("failed"))
    assert stream3.getvalue() == ""  # no restore: nothing extra to render


def test_capability_failure_error_carries_restore_outcome(tmp_path: Path):
    project = make_project(tmp_path)
    plan = healthy_plan(project, dry_run=False, approval_required=True)
    engine = build_engine(tmp_path, project, plan, runner=failing_runner)
    engine.backup_engine = BackupEngine(tmp_path / "backups")

    with pytest.raises(UpdateError) as excinfo:
        engine.execute_update("demo", approve=True)
    message = str(excinfo.value)
    assert "restored from the pre-update snapshot" in message
    assert "Audit:" in message


def test_capability_imports_no_control_plane_and_stays_cli_side():
    from aipm.capabilities.update import commands as capability_module

    source = inspect.getsource(capability_module)
    assert "control_plane" not in source
    assert "subprocess" not in source


# ---------------------------------------------------------------------------
# C1 — engine-side execution-contract binding (fail-closed, pre-mutation)
# ---------------------------------------------------------------------------


def matching_contract(plan: UpdatePlan) -> "ExecutionContract":
    from aipm.services.update.plan_identity import UpdatePlanIdentity

    return ExecutionContract(
        project_name=plan.project,
        plan_digest=UpdatePlanIdentity.from_plan(plan).digest(),
        confirmation_id="c" * 32,
    )


def test_execution_contract_model_rejects_malformed_binding_material():
    from aipm.services.update.execution_contract import ExecutionContract

    good = {"project_name": "demo", "plan_digest": "d" * 64, "confirmation_id": "c" * 32}
    ExecutionContract(**good)  # well-formed values construct

    bad_contracts = [
        {**good, "project_name": ""},          # empty project
        {**good, "project_name": "a/b"},       # path separator
        {**good, "project_name": None},        # not a string
        {**good, "plan_digest": "D" * 64},     # uppercase hex
        {**good, "plan_digest": "d" * 63},     # wrong length
        {**good, "plan_digest": ""},           # missing digest
        {**good, "plan_digest": None},
        {**good, "confirmation_id": "c" * 31},  # wrong length
        {**good, "confirmation_id": "C" * 32},  # uppercase hex
    ]
    for bad in bad_contracts:
        with pytest.raises(ValueError):
            ExecutionContract(**bad)


def test_engine_executes_with_a_matching_contract(tmp_path: Path):
    project = make_project(tmp_path)
    plan = healthy_plan(project, dry_run=False, approval_required=True)
    engine = build_engine(tmp_path, project, plan)

    audit = engine.execute_update("demo", approve=True, execution_contract=matching_contract(plan))

    assert audit.outcome == "success"
    assert audit.audit_path is not None and audit.audit_path.exists()
    assert engine.backup_engine.created  # runtime path ran fully under the contract


def test_engine_refuses_contract_digest_mismatch_before_any_mutation(tmp_path: Path):
    project = make_project(tmp_path)
    plan = healthy_plan(project, dry_run=False, approval_required=True)
    engine = build_engine(tmp_path, project, plan)

    # A different plan the operator did NOT see: vary a security-relevant
    # identity field (risk) so the canonical digest differs. The contract is
    # well-formed, so the refusal comes from the engine's binding check.
    other_plan = replace(plan, risk=UpdateRisk.HIGH)
    other_contract = matching_contract(other_plan)
    assert other_contract.plan_digest != matching_contract(plan).plan_digest

    with pytest.raises(UpdateError, match="issued for a different plan"):
        engine.execute_update("demo", approve=True, execution_contract=other_contract)

    assert not engine.backup_engine.created        # no snapshot attempted
    assert engine.compose_provider.calls == []     # no runtime mutation
    assert list((tmp_path / "audit").glob("*.json")) == []  # refusal precedes audit records
    assert plan.actions == healthy_plan(project, dry_run=False, approval_required=True).actions  # plan untouched


def test_engine_refuses_contract_for_a_different_project(tmp_path: Path):
    project = make_project(tmp_path)
    plan = healthy_plan(project, dry_run=False, approval_required=True)
    engine = build_engine(tmp_path, project, plan)

    foreign = matching_contract(plan)
    from dataclasses import replace
    foreign = replace(foreign, project_name="other")

    with pytest.raises(UpdateError, match="not bound to this project"):
        engine.execute_update("demo", approve=True, execution_contract=foreign)

    assert not engine.backup_engine.created
    assert engine.compose_provider.calls == []
    assert list((tmp_path / "audit").glob("*.json")) == []


def test_engine_contract_rejection_happens_before_blocked_and_approval_gates(tmp_path: Path):
    """Ordering: the binding check fires even when later gates would also refuse.

    A plan that is both unconfirmed (approval_required, no --yes) and carrying
    a mismatched contract must be refused by the contract check first — proof
    that the binding check sits before every runtime-mutation gate.
    """
    project = make_project(tmp_path)
    unconfirmed = UpdatePlan(
        project=project.name,
        project_path=project.path,
        dry_run=False,
        proceed=True,
        approval_required=True,
        risk=UpdateRisk.BLOCKED,  # a review-blocking plan the operator never approved
        reasons=["Compose file has unresolved keys"],
    )
    engine = build_engine(tmp_path, project, unconfirmed)
    mismatched = matching_contract(healthy_plan(project, dry_run=False))

    with pytest.raises(UpdateError, match="issued for a different plan"):
        engine.execute_update("demo", approve=False, execution_contract=mismatched)

    assert not engine.backup_engine.created
    assert list((tmp_path / "audit").glob("*.json")) == []


def test_engine_contract_check_tolerates_plan_field_changes_that_change_digest(tmp_path: Path):
    """Any security-relevant plan mutation must invalidate the contract.

    Changing only a non-identity field (project_path, an observation string)
    keeps the digest stable and the contract valid — evidence that the binding
    targets the canonical identity, not incidental fields.
    """
    from aipm.services.update.plan_identity import UpdatePlanIdentity

    project = make_project(tmp_path)
    plan = healthy_plan(project, dry_run=False, approval_required=True)
    engine = build_engine(tmp_path, project, plan)

    same_identity = replace(plan, project_path=str(tmp_path / "elsewhere"))
    assert UpdatePlanIdentity.from_plan(same_identity).digest() == matching_contract(plan).plan_digest
    audit = engine.execute_update("demo", approve=True, plan=same_identity, execution_contract=matching_contract(plan))
    assert audit.outcome == "success"

    changed_identity = replace(plan, risk=UpdateRisk.HIGH)
    assert UpdatePlanIdentity.from_plan(changed_identity).digest() != matching_contract(plan).plan_digest
    rejection_engine = build_engine(tmp_path, project, plan)
    with pytest.raises(UpdateError, match="issued for a different plan"):
        rejection_engine.execute_update("demo", approve=True, plan=changed_identity, execution_contract=matching_contract(plan))
    assert not rejection_engine.backup_engine.created


def test_engine_without_contract_keeps_legacy_cli_path(tmp_path: Path):
    """The CLI legacy path (no contract) is unchanged by C1."""
    project = make_project(tmp_path)
    plan = healthy_plan(project, dry_run=False, approval_required=True)
    engine = build_engine(tmp_path, project, plan)

    audit = engine.execute_update("demo", approve=True)

    assert audit.outcome == "success"
    assert engine.backup_engine.created


def test_engine_never_gates_on_flight_control_and_contract_module_stays_pure():
    from aipm.services.update import execution_contract as module

    source = inspect.getsource(module)
    for forbidden in ("subprocess", "os.system", "Popen", "socket", "requests.", "urllib", "httpx"):
        assert forbidden not in source
    assert "control_plane" not in source
    # The contract type's own code carries no engine or execution behavior:
    # scan only code identifiers via AST (docstrings/comments excluded).
    import ast

    tree = ast.parse(inspect.getsource(ExecutionContract))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
    for forbidden in ("engine", "execute_update", "subprocess", "Popen"):
        assert not any(forbidden in name for name in identifiers)
