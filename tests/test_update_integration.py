"""End-to-end integration of the real update pipeline against disposable fixtures.

Every test wires the REAL components together — UpdatePlanner, UpdateEngine
(with its real subprocess runner), GitTransactionRunner, RollbackManager,
UpdateVerifier, BackupEngine, AuditService, and the real GitService/GitProvider
— against disposable Git repositories with bare remotes and Compose project
layouts built under pytest's tmp_path. Never touches /home/ubuntu, real
project directories, production databases, or the real Docker socket.

The only seams are the two documented ones: FixedProjectService (the real
ProjectService is not hermetic: it constructs an Application that writes
user-level config and logs) and the GuardCompose tripwire (start_services.py
must drive the runtime; no Docker daemon is guaranteed in tests). The
Docker-daemon boundary is exercised at the planner level: the Compose
fixture drives the real static preflight, and runtime execution is proven
with committed start_services.py scripts instead.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aipm.core.exceptions import UpdateError
from aipm.models.update import UpdateRisk
from aipm.models.verification import UpdateVerificationStatus

from update_fixtures import (
    DEFAULT_COMPOSE_DOCUMENT,
    FAILING_RUNTIME_SCRIPT,
    MARKER_RUNTIME_SCRIPT,
    build_engine,
    fetch_origin,
    make_compose_project,
    make_remote_commit,
    make_repo,
    rev_parse_head,
    stash_entries,
    status_porcelain,
)


def read_single_audit(tmp_path: Path) -> dict:
    """Read the one audit record written so far under the fixture audit dir."""
    audit_dir = tmp_path / "audit"
    files = sorted(audit_dir.glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_clean_update_pulls_remote_changes_end_to_end(tmp_path: Path, monkeypatch):
    project = make_repo(tmp_path, runtime_script=MARKER_RUNTIME_SCRIPT)
    make_remote_commit(project, "docs.md", "from remote\n")
    fetch_origin(project)
    marker = tmp_path / "runtime-marker.txt"
    monkeypatch.setenv("AIPM_INTEGRATION_MARKER", str(marker))

    engine = build_engine(tmp_path, project)
    plan = engine.plan_update(project.name)
    assert plan.proceed is True
    assert plan.pull_required is True
    assert plan.stash_required is False

    audit = engine.execute_update(project.name, approve=True, plan=plan)
    assert audit.mode == "execute"
    assert audit.outcome == "success"
    assert audit.git_transaction.success is True
    assert audit.git_transaction.pulled is True
    assert audit.git_transaction.stashed is False
    assert audit.verification.status is UpdateVerificationStatus.SUCCESS

    # the real runner executed the committed start_services.py
    assert marker.read_text(encoding="utf-8") == "started"
    # the pull landed and the repository is left clean
    assert (Path(project.path) / "docs.md").read_text(encoding="utf-8") == "from remote\n"
    assert status_porcelain(project) == ""
    assert stash_entries(project) == []

    # the written audit record and snapshot stay inside the fixture tree
    record = json.loads(audit.audit_path.read_text(encoding="utf-8"))
    assert record["mode"] == "execute"
    assert record["outcome"] == "success"
    assert record["git_transaction"]["pulled"] is True
    assert record["verification"]["status"] == "success"
    snapshot = Path(record["snapshot_path"])
    assert snapshot.parent == tmp_path / "backups"
    assert snapshot.is_file()
    assert audit.audit_path.parent == tmp_path / "audit"


def test_operator_changes_preserved_through_stash_pull_apply(tmp_path: Path):
    project = make_repo(tmp_path)
    work = Path(project.path)
    (work / "config.txt").write_text("operator edit\n", encoding="utf-8")
    (work / "notes.txt").write_text("operator note\n", encoding="utf-8")
    make_remote_commit(project, "docs.md", "from remote\n")
    fetch_origin(project)

    engine = build_engine(tmp_path, project)
    plan = engine.plan_update(project.name)
    assert plan.proceed is True
    assert plan.stash_required is True
    assert plan.pull_required is True

    audit = engine.execute_update(project.name, approve=True)
    assert audit.outcome == "success"
    assert audit.git_transaction.stashed is True
    assert audit.git_transaction.stash_applied is True
    assert audit.git_transaction.stash_preserved is False
    assert audit.git_transaction.pulled is True
    # reapplied operator changes make the post-update health report dirty at
    # HIGH severity: a WARNING verdict, which is not a rollback condition
    assert audit.verification.status is UpdateVerificationStatus.WARNING
    assert audit.verification.update_successful is True

    # operator changes preserved in the working tree, stash dropped after apply
    assert (work / "config.txt").read_text(encoding="utf-8") == "operator edit\n"
    assert (work / "notes.txt").read_text(encoding="utf-8") == "operator note\n"
    assert (work / "docs.md").read_text(encoding="utf-8") == "from remote\n"
    assert stash_entries(project) == []


def test_conflicting_stash_apply_fails_audits_and_preserves_stash(tmp_path: Path):
    project = make_repo(tmp_path)
    work = Path(project.path)
    (work / "config.txt").write_text("operator edit\n", encoding="utf-8")
    make_remote_commit(project, "config.txt", "remote v2\n")
    fetch_origin(project)

    engine = build_engine(tmp_path, project)
    plan = engine.plan_update(project.name)
    assert plan.proceed is True
    assert plan.stash_required is True

    with pytest.raises(UpdateError) as excinfo:
        engine.execute_update(project.name, approve=True)
    message = str(excinfo.value)
    assert message.startswith("Git transaction failed applying the safety stash")
    assert "Conflicting files: config.txt" in message
    assert "Audit:" in message
    assert "project files were restored from the pre-update snapshot" in message

    # the safety stash is preserved for manual recovery
    stashes = stash_entries(project)
    assert len(stashes) == 1
    assert "AIPM safety stash" in stashes[0]
    # rollback restored the operator's pre-update content
    assert (work / "config.txt").read_text(encoding="utf-8") == "operator edit\n"

    record = read_single_audit(tmp_path)
    assert record["mode"] == "execute"
    assert record["outcome"] == "failed"
    assert record["git_transaction"]["stash_preserved"] is True
    assert record["git_transaction"]["conflicts"] == ["config.txt"]
    assert record["git_transaction"]["stash_applied"] is False
    assert record["restore"]["success"] is True
    assert "config.txt" in record["restore"]["restored"]


def test_incoming_critical_file_change_blocks_planning_and_execution(tmp_path: Path):
    project = make_repo(tmp_path)
    make_remote_commit(project, "compose.yaml", "services: {}\n")
    fetch_origin(project)
    head_before = rev_parse_head(project)

    engine = build_engine(tmp_path, project)
    plan = engine.plan_update(project.name)
    assert plan.proceed is False
    assert plan.risk is UpdateRisk.BLOCKED
    assert any(
        "Incoming remote changes modify critical infrastructure files: compose.yaml." in reason
        for reason in plan.reasons
    )
    assert any(
        "Git state requires manual review before an update can proceed." in reason
        for reason in plan.reasons
    )

    # even explicit --yes cannot bypass a blocked plan
    with pytest.raises(UpdateError) as excinfo:
        engine.execute_update(project.name, approve=True)
    assert str(excinfo.value) == "Update blocked: the plan requires manual review. No state was changed."

    record = read_single_audit(tmp_path)
    assert record["mode"] == "execute"
    assert record["outcome"] == "blocked"
    assert record["error"] == "Plan requires manual review."
    assert record["snapshot_path"] is None
    assert record["git_transaction"] is None
    assert record["restore"] is None
    # no state changed: nothing pulled, nothing stashed, worktree untouched
    assert rev_parse_head(project) == head_before
    assert not (Path(project.path) / "compose.yaml").exists()
    assert status_porcelain(project) == ""
    assert stash_entries(project) == []


def test_runtime_failure_rolls_back_and_reports_restore(tmp_path: Path):
    project = make_repo(tmp_path, runtime_script=FAILING_RUNTIME_SCRIPT)
    make_remote_commit(project, "docs.md", "from remote\n")
    fetch_origin(project)

    engine = build_engine(tmp_path, project)
    with pytest.raises(UpdateError) as excinfo:
        engine.execute_update(project.name, approve=True)
    message = str(excinfo.value)
    assert "Step 'Custom runtime rebuild' failed: boom" in message
    assert "Audit:" in message
    assert "project files were restored from the pre-update snapshot" in message

    record = read_single_audit(tmp_path)
    assert record["mode"] == "execute"
    assert record["outcome"] == "failed"
    # the Git transaction itself completed; the runtime step failed afterwards
    assert record["git_transaction"]["success"] is True
    assert record["git_transaction"]["pulled"] is True
    # rollback restored the snapshotted files and reported the pulled file as
    # left in place (restore never deletes anything)
    assert record["restore"]["success"] is True
    assert sorted(record["restore"]["restored"]) == ["config.txt", "start_services.py"]
    assert record["restore"]["left_in_place"] == ["docs.md"]
    assert record["verification"] is None
    assert record["health_after"] is None
    assert (Path(project.path) / "docs.md").read_text(encoding="utf-8") == "from remote\n"


def test_approval_gate_end_to_end(tmp_path: Path, monkeypatch):
    project = make_repo(tmp_path, runtime_script=MARKER_RUNTIME_SCRIPT)
    marker = tmp_path / "runtime-marker.txt"
    monkeypatch.setenv("AIPM_INTEGRATION_MARKER", str(marker))

    engine = build_engine(tmp_path, project)
    plan = engine.plan_update(project.name)
    assert plan.proceed is True
    assert plan.approval_required is True

    with pytest.raises(UpdateError) as excinfo:
        engine.execute_update(project.name, approve=False)
    message = str(excinfo.value)
    assert message.startswith("Explicit approval is required. Review the plan and rerun with --yes.")
    assert "Audit:" in message

    record = read_single_audit(tmp_path)
    assert record["mode"] == "execute"
    assert record["outcome"] == "approval_required"
    assert record["error"] == "Explicit --yes approval was not provided."
    assert record["snapshot_path"] is None
    # nothing ran: no snapshot, no runtime, no stash, no worktree change
    assert not marker.exists()
    assert stash_entries(project) == []
    assert status_porcelain(project) == ""

    audit = engine.execute_update(project.name, approve=True)
    assert audit.outcome == "success"
    assert marker.read_text(encoding="utf-8") == "started"


def test_dry_run_writes_audit_without_touching_state(tmp_path: Path, monkeypatch):
    project = make_repo(tmp_path, runtime_script=MARKER_RUNTIME_SCRIPT)
    marker = tmp_path / "runtime-marker.txt"
    monkeypatch.setenv("AIPM_INTEGRATION_MARKER", str(marker))
    head_before = rev_parse_head(project)

    engine = build_engine(tmp_path, project)
    audit = engine.execute_update(project.name, dry_run=True)
    assert audit.mode == "dry-run"
    assert audit.outcome == "planned"
    assert audit.audit_path is not None
    assert audit.audit_path.is_file()
    assert audit.audit_path.parent == tmp_path / "audit"

    record = json.loads(audit.audit_path.read_text(encoding="utf-8"))
    assert record["mode"] == "dry-run"
    assert record["outcome"] == "planned"
    assert record["plan"]["dry_run"] is True
    assert record["snapshot_path"] is None
    # dry-run is fully read-only: no runtime, no snapshot, no Git mutation
    assert not marker.exists()
    assert rev_parse_head(project) == head_before
    assert status_porcelain(project) == ""
    assert stash_entries(project) == []


def test_compose_fixture_drives_real_planner_preflight_without_docker(tmp_path: Path):
    """The Compose fixture exercises the real planner preflight; runtime
    execution needs a Docker daemon, which tests must never assume — the
    boundary is the GuardCompose tripwire plus this planner-level coverage.
    """
    project = make_compose_project(tmp_path)
    engine = build_engine(tmp_path, project)

    plan = engine.plan_update(project.name, dry_run=True)
    assert plan.proceed is True
    assert plan.risk is UpdateRisk.MEDIUM
    assert "Rebuild and start the project Compose services (db, web)" in plan.actions
    assert "Verify health after the update" in plan.actions
    assert plan.estimated_restart is True
    assert plan.reasons == []

    # static preflight only: the fixture layout is unchanged, nothing added
    work = Path(project.path)
    assert sorted(entry.name for entry in work.iterdir()) == ["compose.yaml"]
    assert (work / "compose.yaml").read_text(encoding="utf-8") == DEFAULT_COMPOSE_DOCUMENT
