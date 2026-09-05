"""Security-contract tests for update plan identity, approval, and single-flight.

Groups covered:
* A — canonical plan identity and digest (determinism, mutation matrix,
  order semantics, optional handling, no-timestamp dependence, extraction).
* B — approval record model validation (malformed inputs fail closed).
* C — approval validation binding matrix (project/digest/operator/epoch/
  session/expiry), validation never mutates state.
* D — single-use consumption (replay matrix, failed consumes never consume,
  concurrent consume is exactly-once).
* E — canonical control-plane compatibility (separate module:
  ``tests/test_update_approval_canonical.py``).
* F — per-project single-flight primitive.
* G — source boundary: no execution capability, no engine wiring, no
  dashboard mutation routes.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest

from aipm.models.git import GitRepository
from aipm.models.health import HealthState
from aipm.models.health_report import HealthReport
from aipm.models.update import UpdatePlan, UpdateRisk
from aipm.services.update.approval import (
    APPROVAL_TTL,
    ApprovalState,
    InMemoryUpdateApprovalStore,
    OperatorIdentity,
    UpdateApprovalError,
    UpdateApprovalRecord,
    UpdateApprovalService,
    UpdateFlightControl,
    _FlightToken,
)
from aipm.services.update.plan_identity import (
    PLAN_IDENTITY_VERSION,
    UpdatePlanIdentity,
    update_plan_digest,
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

PROJECT = "demo"
PROJECT_ID = "b" * 24
DIGEST = "a" * 64
UTC = timezone.utc


def fixed_clock(value: str = "2026-01-01T12:00:00+00:00"):
    moment = datetime.fromisoformat(value)
    return lambda: moment


def make_service(clock=None) -> UpdateApprovalService:
    return UpdateApprovalService(
        store=InMemoryUpdateApprovalStore(),
        clock=clock or fixed_clock(),
    )


def operator(
    subject: str = "local-owner",
    epoch: int = 1,
    session_id: str = "sess-0001",
) -> OperatorIdentity:
    return OperatorIdentity(subject=subject, auth_epoch=epoch, session_id=session_id)


def issue(
    service: UpdateApprovalService | None = None,
    op: OperatorIdentity | None = None,
    digest: str = DIGEST,
    project_id: str = PROJECT_ID,
) -> tuple[UpdateApprovalRecord, UpdateApprovalService]:
    service = service or make_service()
    op = op or operator()
    record = service.issue(
        project_id=project_id,
        project_name=PROJECT,
        plan_digest=digest,
        operator=op,
    )
    return record, service


def make_plan(
    *,
    git: GitRepository | None = None,
    health: HealthReport | None = None,
    reasons: tuple[str, ...] = ("behind remote",),
    actions: tuple[str, ...] = ("snapshot", "pull"),
    risk: UpdateRisk = UpdateRisk.MEDIUM,
    proceed: bool = True,
    approval_required: bool = True,
) -> UpdatePlan:
    return UpdatePlan(
        project=PROJECT,
        project_path="/srv/demo",
        dry_run=False,
        proceed=proceed,
        approval_required=approval_required,
        risk=risk,
        reasons=list(reasons),
        actions=list(actions),
        snapshot_required=True,
        estimated_restart=True,
        stash_required=False,
        pull_required=True,
        git=git,
        health_before=health,
    )


def make_git(**overrides) -> GitRepository:
    fields = dict(
        exists=True,
        branch="main",
        current_sha="c" * 40,
        remote_sha="d" * 40,
        remote_url="git@example.com:demo.git",
        dirty=False,
        detached=False,
        ahead=0,
        behind=2,
        modified_files=["b.txt", "a.txt"],
        untracked_files=["z.log"],
        conflicted_files=[],
        stashes=[],
        last_fetch=datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        last_commit_message="feat: thing",
        last_commit_author="Mina",
    )
    fields.update(overrides)
    return GitRepository(**fields)


def make_health(**overrides) -> HealthReport:
    fields = dict(
        project=PROJECT,
        score=90,
        state=HealthState.HEALTHY,
        critical=0,
        high=1,
        warning=2,
        info=3,
        findings=[],
        recommendations=["consider a snapshot"],
    )
    fields.update(overrides)
    return HealthReport(**fields)


# ---------------------------------------------------------------------------
# A — canonical plan identity and digest
# ---------------------------------------------------------------------------


class TestPlanDigestDeterminism:
    def test_digest_is_stable_across_repeated_construction(self):
        plan = make_plan(git=make_git(), health=make_health())
        first = UpdatePlanIdentity.from_plan(plan).digest()
        second = UpdatePlanIdentity.from_plan(plan).digest()
        assert first == second
        assert len(first) == 64
        int(first, 16)  # lowercase hex

    def test_digest_matches_independently_computed_sha256(self):
        plan = make_plan(git=make_git(), health=make_health())
        identity = UpdatePlanIdentity.from_plan(plan)
        canonical_json = identity.canonical_json()
        assert identity.digest() == hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def test_known_canonical_json_literal(self):
        """Pin the canonical serialization format against drift."""

        plan = make_plan(git=make_git(), health=make_health())
        identity = UpdatePlanIdentity.from_plan(plan)
        payload = identity.canonical_payload()
        # None fields absent; only security-relevant summaries present.
        assert "git_modified_files" in payload
        assert payload["git_modified_files"] == ["a.txt", "b.txt"]  # sorted
        assert "health_state" in payload
        assert "remote_url" not in payload
        assert "last_fetch" not in payload
        # Round-trip serialization is sorted, compact, UTF-8-safe.
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        assert text == identity.canonical_json()
        assert identity.canonical_bytes() == text.encode("utf-8")
        assert identity.version == PLAN_IDENTITY_VERSION

    def test_convenience_function_matches_class(self):
        plan = make_plan(git=make_git(), health=make_health())
        assert update_plan_digest(plan) == UpdatePlanIdentity.from_plan(plan).digest()


class TestPlanDigestMutationMatrix:
    """Any security-relevant mutation must change the digest."""

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda p: replace_project(p, "other"), id="project"),
            pytest.param(lambda p: replace_bool(p, "dry_run", True), id="dry_run"),
            pytest.param(lambda p: replace_bool(p, "proceed", False), id="proceed"),
            pytest.param(lambda p: replace_bool(p, "approval_required", False), id="approval_required"),
            pytest.param(lambda p: replace_risk(p, UpdateRisk.LOW), id="risk"),
            pytest.param(lambda p: replace_reasons(p, ("different",)), id="reasons-content"),
            pytest.param(lambda p: replace_reasons(p, ("behind remote", "extra")), id="reasons-append"),
            pytest.param(lambda p: replace_reasons(p, ()), id="reasons-empty"),
            pytest.param(lambda p: replace_actions(p, ("pull", "snapshot")), id="actions-order"),
            pytest.param(lambda p: replace_actions(p, ("snapshot",)), id="actions-content"),
            pytest.param(lambda p: replace_bool(p, "snapshot_required", False), id="snapshot_required"),
            pytest.param(lambda p: replace_bool(p, "estimated_restart", False), id="estimated_restart"),
            pytest.param(lambda p: replace_bool(p, "stash_required", True), id="stash_required"),
            pytest.param(lambda p: replace_bool(p, "pull_required", False), id="pull_required"),
            pytest.param(lambda p: replace_git(p, dict(behind=3)), id="git-behind"),
            pytest.param(lambda p: replace_git(p, dict(current_sha="e" * 40)), id="git-current-sha"),
            pytest.param(lambda p: replace_git(p, dict(remote_sha="f" * 40)), id="git-remote-sha"),
            pytest.param(lambda p: replace_git(p, dict(dirty=True)), id="git-dirty"),
            pytest.param(lambda p: replace_git(p, dict(detached=True)), id="git-detached"),
            pytest.param(lambda p: replace_git(p, dict(ahead=1)), id="git-ahead"),
            pytest.param(lambda p: replace_git(p, dict(branch="feature")), id="git-branch"),
            pytest.param(lambda p: replace_git(p, dict(exists=False)), id="git-exists"),
            pytest.param(lambda p: replace_git(p, dict(modified_files=["c.txt", "a.txt", "b.txt"])), id="git-modified"),
            pytest.param(lambda p: replace_git(p, dict(untracked_files=[])), id="git-untracked"),
            pytest.param(lambda p: replace_git(p, dict(conflicted_files=["x"])), id="git-conflicted"),
            pytest.param(lambda p: replace_git(p, dict(stashes=["s1"])), id="git-stashes"),
            pytest.param(lambda p: replace_git(p, None), id="git-absent"),
            pytest.param(lambda p: replace_health(p, dict(score=50)), id="health-score"),
            pytest.param(lambda p: replace_health(p, dict(state=HealthState.DEGRADED)), id="health-state"),
            pytest.param(lambda p: replace_health(p, dict(critical=1)), id="health-critical"),
            pytest.param(lambda p: replace_health(p, dict(high=2)), id="health-high"),
            pytest.param(lambda p: replace_health(p, dict(warning=0)), id="health-warning"),
            pytest.param(lambda p: replace_health(p, dict(info=0)), id="health-info"),
            pytest.param(lambda p: replace_health(p, None), id="health-absent"),
        ],
    )
    def test_mutation_changes_digest(self, mutate):
        baseline_plan = make_plan(git=make_git(), health=make_health())
        baseline = UpdatePlanIdentity.from_plan(baseline_plan).digest()
        mutated = UpdatePlanIdentity.from_plan(mutate(baseline_plan)).digest()
        assert mutated != baseline


def replace_project(plan: UpdatePlan, project: str) -> UpdatePlan:
    return UpdatePlan(
        project=project,
        project_path=plan.project_path,
        dry_run=plan.dry_run,
        proceed=plan.proceed,
        approval_required=plan.approval_required,
        risk=plan.risk,
        reasons=list(plan.reasons),
        actions=list(plan.actions),
        snapshot_required=plan.snapshot_required,
        estimated_restart=plan.estimated_restart,
        stash_required=plan.stash_required,
        pull_required=plan.pull_required,
        git=plan.git,
        health_before=plan.health_before,
    )


def replace_bool(plan: UpdatePlan, name: str, value: bool) -> UpdatePlan:
    fields = dict(
        project=plan.project,
        project_path=plan.project_path,
        dry_run=plan.dry_run,
        proceed=plan.proceed,
        approval_required=plan.approval_required,
        snapshot_required=plan.snapshot_required,
        estimated_restart=plan.estimated_restart,
        stash_required=plan.stash_required,
        pull_required=plan.pull_required,
    )
    fields[name] = value
    return UpdatePlan(
        **fields,
        risk=plan.risk,
        reasons=list(plan.reasons),
        actions=list(plan.actions),
        git=plan.git,
        health_before=plan.health_before,
    )


def replace_risk(plan: UpdatePlan, risk: UpdateRisk) -> UpdatePlan:
    return UpdatePlan(
        project=plan.project,
        project_path=plan.project_path,
        dry_run=plan.dry_run,
        proceed=plan.proceed,
        approval_required=plan.approval_required,
        risk=risk,
        reasons=list(plan.reasons),
        actions=list(plan.actions),
        snapshot_required=plan.snapshot_required,
        estimated_restart=plan.estimated_restart,
        stash_required=plan.stash_required,
        pull_required=plan.pull_required,
        git=plan.git,
        health_before=plan.health_before,
    )


def replace_reasons(plan: UpdatePlan, reasons: tuple[str, ...]) -> UpdatePlan:
    return UpdatePlan(
        project=plan.project,
        project_path=plan.project_path,
        dry_run=plan.dry_run,
        proceed=plan.proceed,
        approval_required=plan.approval_required,
        risk=plan.risk,
        reasons=list(reasons),
        actions=list(plan.actions),
        snapshot_required=plan.snapshot_required,
        estimated_restart=plan.estimated_restart,
        stash_required=plan.stash_required,
        pull_required=plan.pull_required,
        git=plan.git,
        health_before=plan.health_before,
    )


def replace_actions(plan: UpdatePlan, actions: tuple[str, ...]) -> UpdatePlan:
    return UpdatePlan(
        project=plan.project,
        project_path=plan.project_path,
        dry_run=plan.dry_run,
        proceed=plan.proceed,
        approval_required=plan.approval_required,
        risk=plan.risk,
        reasons=list(plan.reasons),
        actions=list(actions),
        snapshot_required=plan.snapshot_required,
        estimated_restart=plan.estimated_restart,
        stash_required=plan.stash_required,
        pull_required=plan.pull_required,
        git=plan.git,
        health_before=plan.health_before,
    )


def replace_git(plan: UpdatePlan, overrides: dict | None) -> UpdatePlan:
    if overrides is None:
        git = None
    else:
        git = make_git(**overrides)
    return UpdatePlan(
        project=plan.project,
        project_path=plan.project_path,
        dry_run=plan.dry_run,
        proceed=plan.proceed,
        approval_required=plan.approval_required,
        risk=plan.risk,
        reasons=list(plan.reasons),
        actions=list(plan.actions),
        snapshot_required=plan.snapshot_required,
        estimated_restart=plan.estimated_restart,
        stash_required=plan.stash_required,
        pull_required=plan.pull_required,
        git=git,
        health_before=plan.health_before,
    )


def replace_health(plan: UpdatePlan, overrides: dict | None) -> UpdatePlan:
    health = None if overrides is None else make_health(**overrides)
    return UpdatePlan(
        project=plan.project,
        project_path=plan.project_path,
        dry_run=plan.dry_run,
        proceed=plan.proceed,
        approval_required=plan.approval_required,
        risk=plan.risk,
        reasons=list(plan.reasons),
        actions=list(plan.actions),
        snapshot_required=plan.snapshot_required,
        estimated_restart=plan.estimated_restart,
        stash_required=plan.stash_required,
        pull_required=plan.pull_required,
        git=plan.git,
        health_before=health,
    )


class TestPlanDigestOrderSemantics:
    def test_reasons_are_order_sensitive(self):
        base = make_plan(reasons=("one", "two"))
        swapped = make_plan(reasons=("two", "one"))
        assert UpdatePlanIdentity.from_plan(base).digest() != UpdatePlanIdentity.from_plan(swapped).digest()

    def test_actions_are_order_sensitive(self):
        base = make_plan(actions=("snapshot", "pull"))
        swapped = make_plan(actions=("pull", "snapshot"))
        assert UpdatePlanIdentity.from_plan(base).digest() != UpdatePlanIdentity.from_plan(swapped).digest()

    def test_git_file_lists_are_order_insensitive(self):
        base = make_plan(git=make_git(modified_files=["a.txt", "b.txt", "c.txt"]))
        shuffled = make_plan(git=make_git(modified_files=["c.txt", "a.txt", "b.txt"]))
        assert UpdatePlanIdentity.from_plan(base).digest() == UpdatePlanIdentity.from_plan(shuffled).digest()

    def test_git_file_lists_are_deduplicated(self):
        with_dupes = make_plan(git=make_git(modified_files=["a.txt", "a.txt", "b.txt"]))
        clean = make_plan(git=make_git(modified_files=["a.txt", "b.txt"]))
        assert UpdatePlanIdentity.from_plan(with_dupes).digest() == UpdatePlanIdentity.from_plan(clean).digest()


class TestPlanDigestOptionalHandling:
    def test_git_absent_vs_present_differ(self):
        absent = UpdatePlanIdentity.from_plan(make_plan(git=None)).digest()
        present = UpdatePlanIdentity.from_plan(make_plan(git=make_git())).digest()
        assert absent != present

    def test_health_absent_vs_present_differ(self):
        absent = UpdatePlanIdentity.from_plan(make_plan(health=None)).digest()
        present = UpdatePlanIdentity.from_plan(make_plan(health=make_health())).digest()
        assert absent != present

    def test_observation_timestamps_do_not_affect_digest(self):
        early = make_plan(git=make_git(last_fetch=datetime(2020, 1, 1, tzinfo=UTC)))
        late = make_plan(git=make_git(last_fetch=datetime(2030, 6, 1, tzinfo=UTC)))
        assert UpdatePlanIdentity.from_plan(early).digest() == UpdatePlanIdentity.from_plan(late).digest()

    def test_free_text_observations_do_not_affect_digest(self):
        message_a = make_plan(git=make_git(last_commit_message="feat: one"))
        message_b = make_plan(git=make_git(last_commit_message="fix: other"))
        author = make_plan(git=make_git(last_commit_author="Someone Else"))
        baseline = make_plan(git=make_git())
        baseline_digest = UpdatePlanIdentity.from_plan(baseline).digest()
        assert UpdatePlanIdentity.from_plan(message_a).digest() == baseline_digest
        assert UpdatePlanIdentity.from_plan(message_b).digest() == baseline_digest
        assert UpdatePlanIdentity.from_plan(author).digest() == baseline_digest

    def test_remote_url_does_not_affect_digest(self):
        https = make_plan(git=make_git(remote_url="https://example.com/demo.git"))
        ssh = make_plan(git=make_git(remote_url="git@example.com:demo.git"))
        assert UpdatePlanIdentity.from_plan(https).digest() == UpdatePlanIdentity.from_plan(ssh).digest()

    def test_project_path_is_excluded(self):
        one = UpdatePlan(
            project=PROJECT,
            project_path="/srv/demo",
            dry_run=False,
            proceed=True,
            approval_required=True,
            risk=UpdateRisk.MEDIUM,
            reasons=["behind remote"],
            actions=["snapshot", "pull"],
            snapshot_required=True,
            estimated_restart=True,
            stash_required=False,
            pull_required=True,
            git=None,
            health_before=None,
        )
        fields = {
            "project": one.project,
            "project_path": "/elsewhere/demo",
            "dry_run": one.dry_run,
            "proceed": one.proceed,
            "approval_required": one.approval_required,
            "risk": one.risk,
            "reasons": list(one.reasons),
            "actions": list(one.actions),
            "snapshot_required": one.snapshot_required,
            "estimated_restart": one.estimated_restart,
            "stash_required": one.stash_required,
            "pull_required": one.pull_required,
            "git": None,
            "health_before": None,
        }
        two = UpdatePlan(**fields)
        assert UpdatePlanIdentity.from_plan(one).digest() == UpdatePlanIdentity.from_plan(two).digest()


# ---------------------------------------------------------------------------
# B — approval record model validation
# ---------------------------------------------------------------------------


class TestApprovalRecordValidation:
    def test_valid_record_round_trips(self):
        record, _ = issue()
        assert record.state is ApprovalState.ISSUED
        assert record.expires_at - record.issued_at == APPROVAL_TTL
        assert not record.is_expired(fixed_clock()())
        assert record.safe_dict()["state"] == "issued"

    @pytest.mark.parametrize(
        "field,bad",
        [
            ("approval_id", "short"),
            ("approval_id", "G" * 32),
            ("approval_id", "a" * 31),
            ("plan_digest", "not-a-digest"),
            ("plan_digest", "a" * 63),
            ("plan_digest", "A" * 64),
        ],
    )
    def test_pattern_violations_rejected(self, field, bad):
        kwargs = dict(
            approval_id="1" * 32,
            project_id=PROJECT_ID,
            project_name=PROJECT,
            plan_digest=DIGEST,
            operator_subject="local-owner",
            auth_epoch=1,
            session_id="sess-0001",
            issued_at=fixed_clock()(),
            expires_at=fixed_clock()() + APPROVAL_TTL,
        )
        kwargs[field] = bad
        with pytest.raises(UpdateApprovalError) as excinfo:
            UpdateApprovalRecord(**kwargs)
        assert excinfo.value.reason == UpdateApprovalError.APPROVAL_MALFORMED

    def test_naive_datetimes_rejected(self):
        naive = datetime(2026, 1, 1, 12, 0)
        with pytest.raises(UpdateApprovalError) as excinfo:
            UpdateApprovalRecord(
                approval_id="1" * 32,
                project_id=PROJECT_ID,
                project_name=PROJECT,
                plan_digest=DIGEST,
                operator_subject="local-owner",
                auth_epoch=1,
                session_id="sess-0001",
                issued_at=naive,
                expires_at=naive + APPROVAL_TTL,
            )
        assert excinfo.value.reason == UpdateApprovalError.TIME_PARADOX

    def test_expiry_not_after_issue_rejected(self):
        now = fixed_clock()()
        with pytest.raises(UpdateApprovalError) as excinfo:
            UpdateApprovalRecord(
                approval_id="1" * 32,
                project_id=PROJECT_ID,
                project_name=PROJECT,
                plan_digest=DIGEST,
                operator_subject="local-owner",
                auth_epoch=1,
                session_id="sess-0001",
                issued_at=now,
                expires_at=now,
            )
        assert excinfo.value.reason == UpdateApprovalError.TIME_PARADOX

    def test_expiry_beyond_ttl_rejected(self):
        now = fixed_clock()()
        with pytest.raises(UpdateApprovalError) as excinfo:
            UpdateApprovalRecord(
                approval_id="1" * 32,
                project_id=PROJECT_ID,
                project_name=PROJECT,
                plan_digest=DIGEST,
                operator_subject="local-owner",
                auth_epoch=1,
                session_id="sess-0001",
                issued_at=now,
                expires_at=now + APPROVAL_TTL + timedelta(seconds=1),
            )
        assert excinfo.value.reason == UpdateApprovalError.TIME_PARADOX

    def test_record_is_frozen(self):
        record, _ = issue()
        with pytest.raises(Exception):
            record.state = ApprovalState.CONSUMED

    def test_operator_identity_rejects_bad_epochs_and_sessions(self):
        with pytest.raises(UpdateApprovalError):
            OperatorIdentity(subject="local-owner", auth_epoch=0, session_id="sess")
        with pytest.raises(UpdateApprovalError):
            OperatorIdentity(subject="local-owner", auth_epoch=True, session_id="sess")
        with pytest.raises(UpdateApprovalError):
            OperatorIdentity(subject="local-owner", auth_epoch=1, session_id="")
        with pytest.raises(UpdateApprovalError):
            OperatorIdentity(subject="local-owner", auth_epoch=1, session_id="-bad-start")
        with pytest.raises(UpdateApprovalError):
            OperatorIdentity(subject="", auth_epoch=1, session_id="sess")
        with pytest.raises(UpdateApprovalError):
            OperatorIdentity(subject="bad\nsubject", auth_epoch=1, session_id="sess")


# ---------------------------------------------------------------------------
# C — validation binding matrix
# ---------------------------------------------------------------------------


class TestValidationBindingMatrix:
    def test_valid_binding_passes_without_mutation(self):
        record, service = issue()
        validated = service.validate(
            record.approval_id,
            project_id=PROJECT_ID,
            plan_digest=DIGEST,
            operator=operator(),
        )
        assert validated.state is ApprovalState.ISSUED
        # validation is read-only
        assert service._store.get(record.approval_id).state is ApprovalState.ISSUED

    def test_unknown_approval_rejected(self):
        service = make_service()
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.validate("0" * 32, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator())
        assert excinfo.value.reason == UpdateApprovalError.APPROVAL_NOT_FOUND

    def test_malformed_approval_id_rejected(self):
        service = make_service()
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.validate("nope", project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator())
        assert excinfo.value.reason == UpdateApprovalError.APPROVAL_MALFORMED

    def test_wrong_project_rejected(self):
        record, service = issue()
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.validate(record.approval_id, project_id="c" * 24, plan_digest=DIGEST, operator=operator())
        assert excinfo.value.reason == UpdateApprovalError.PROJECT_MISMATCH

    def test_wrong_digest_rejected(self):
        record, service = issue()
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.validate(record.approval_id, project_id=PROJECT_ID, plan_digest="f" * 64, operator=operator())
        assert excinfo.value.reason == UpdateApprovalError.DIGEST_MISMATCH

    def test_wrong_operator_rejected(self):
        record, service = issue()
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.validate(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator(subject="someone-else"))
        assert excinfo.value.reason == UpdateApprovalError.OPERATOR_MISMATCH

    def test_rotated_epoch_rejected(self):
        record, service = issue(op=operator(epoch=1))
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.validate(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator(epoch=2))
        assert excinfo.value.reason == UpdateApprovalError.SESSION_MISMATCH

    def test_wrong_session_rejected(self):
        record, service = issue(op=operator(session_id="sess-a"))
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.validate(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator(session_id="sess-b"))
        assert excinfo.value.reason == UpdateApprovalError.SESSION_MISMATCH

    def test_expired_rejected(self):
        record, service = issue()
        later = fixed_clock()() + APPROVAL_TTL + timedelta(seconds=1)
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.validate(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator(), now=later)
        assert excinfo.value.reason == UpdateApprovalError.APPROVAL_EXPIRED

    def test_boundary_expiry_at_exact_moment_rejected(self):
        record, service = issue()
        boundary = fixed_clock()() + APPROVAL_TTL
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.validate(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator(), now=boundary)
        assert excinfo.value.reason == UpdateApprovalError.APPROVAL_EXPIRED


# ---------------------------------------------------------------------------
# D — single-use consumption and replay matrix
# ---------------------------------------------------------------------------


class TestConsumptionReplayMatrix:
    def test_consume_transitions_once(self):
        record, service = issue()
        consumed = service.consume(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator())
        assert consumed.state is ApprovalState.CONSUMED
        stored = service._store.get(record.approval_id)
        assert stored.state is ApprovalState.CONSUMED

    def test_double_consume_rejected(self):
        record, service = issue()
        service.consume(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator())
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.consume(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator())
        assert excinfo.value.reason == UpdateApprovalError.APPROVAL_ALREADY_CONSUMED

    @pytest.mark.parametrize(
        "kwargs,expected",
        [
            (dict(project_id="c" * 24), UpdateApprovalError.PROJECT_MISMATCH),
            (dict(plan_digest="f" * 64), UpdateApprovalError.DIGEST_MISMATCH),
            (dict(operator=operator(subject="intruder")), UpdateApprovalError.OPERATOR_MISMATCH),
            (dict(operator=operator(epoch=9)), UpdateApprovalError.SESSION_MISMATCH),
            (dict(operator=operator(session_id="sess-9999")), UpdateApprovalError.SESSION_MISMATCH),
        ],
    )
    def test_failed_consume_never_consumes(self, kwargs, expected):
        record, service = issue()
        base = dict(project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator())
        base.update(kwargs)
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.consume(record.approval_id, **base)
        assert excinfo.value.reason == expected
        assert service._store.get(record.approval_id).state is ApprovalState.ISSUED

    def test_consume_after_expiry_rejected_and_not_consumed(self):
        record, service = issue()
        later = fixed_clock()() + APPROVAL_TTL + timedelta(seconds=1)
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.consume(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator(), now=later)
        assert excinfo.value.reason == UpdateApprovalError.APPROVAL_EXPIRED
        assert service._store.get(record.approval_id).state is ApprovalState.ISSUED

    def test_consume_with_modified_plan_digest_rejected(self):
        """Replay against a *different* (modified) plan fails closed."""

        record, service = issue(digest="a" * 64)
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.consume(record.approval_id, project_id=PROJECT_ID, plan_digest="b" * 64, operator=operator())
        assert excinfo.value.reason == UpdateApprovalError.DIGEST_MISMATCH
        assert service._store.get(record.approval_id).state is ApprovalState.ISSUED

    def test_consume_malformed_id_rejected(self):
        service = make_service()
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.consume("", project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator())
        assert excinfo.value.reason == UpdateApprovalError.APPROVAL_MALFORMED

    def test_replay_after_success_rejected_even_with_new_service_view(self):
        record, service = issue()
        service.consume(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator())
        for _ in range(3):
            with pytest.raises(UpdateApprovalError):
                service.consume(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator())

    def test_consumed_record_not_reusable_via_validate(self):
        record, service = issue()
        service.consume(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator())
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.validate(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator())
        assert excinfo.value.reason == UpdateApprovalError.APPROVAL_ALREADY_CONSUMED

    def test_concurrent_consume_is_exactly_once(self):
        record, service = issue()
        successes: list[int] = []
        barrier = threading.Barrier(16)

        def attempt():
            barrier.wait()
            try:
                service.consume(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator())
                successes.append(1)
            except UpdateApprovalError:
                pass

        threads = [threading.Thread(target=attempt) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(successes) == 1

    def test_validate_then_atomic_consume_toctou_loses_to_racing_consumer(self):
        """DETERMINISTIC TOCTOU BOUNDARY: validation is not consumption.

        Sequence (single-threaded, no timing dependence):
          1. consumer A completes the full validate() phase on a valid approval;
          2. consumer B performs a complete, valid consume() — the atomic CAS
             wins the transition ISSUED → CONSUMED;
          3. consumer A then performs its atomic store-level consume.

        Invariant: the CAS is the final authority. A MUST fail; the approval
        MUST remain CONSUMED; A MUST NOT receive a successful consumption
        result; and no binding mismatch is reported — this is a state race,
        not a validation failure.
        """

        record, service = issue()

        # Step 1: consumer A validates (the pre-CAS phase of consume()).
        validated = service.validate(
            record.approval_id,
            project_id=PROJECT_ID,
            plan_digest=DIGEST,
            operator=operator(),
        )
        assert validated.state is ApprovalState.ISSUED

        # Step 2: consumer B wins the atomic transition while A is in flight.
        consumed_by_b = service.consume(
            record.approval_id,
            project_id=PROJECT_ID,
            plan_digest=DIGEST,
            operator=operator(),
        )
        assert consumed_by_b.state is ApprovalState.CONSUMED

        # Step 3: consumer A completes its atomic consume against the store.
        consumed_by_a = service._store.consume(validated.approval_id)
        assert consumed_by_a is None, "A lost the race; the CAS must return None, never a success"

        # Step 4: a full service-level consume by A must also fail closed.
        with pytest.raises(UpdateApprovalError) as excinfo:
            service.consume(
                validated.approval_id,
                project_id=PROJECT_ID,
                plan_digest=DIGEST,
                operator=operator(),
            )
        assert excinfo.value.reason == UpdateApprovalError.APPROVAL_ALREADY_CONSUMED

        # Approval stays consumed, exactly once, by exactly one consumer.
        stored = service._store.get(record.approval_id)
        assert stored.state is ApprovalState.CONSUMED

    def test_toctou_interleaved_consume_with_spy_store(self):
        """Deterministic mid-consume interleaving via a test-only store wrapper.

        A spy store freezes the service mid-``consume`` — after validate(),
        right before the CAS — letting another consumer complete first.
        Exercises the exact production code path (validate → store.consume)
        with the race forced at the narrowest window, no threads, no timing.
        """

        class CASDoorbellStore:
            """Delegates to a real in-memory store; doorbell rings between
            validate() and the CAS, allowing the test to interleave a second
            consumer at the precise race point."""

            def __init__(self, inner: InMemoryUpdateApprovalStore) -> None:
                self.inner = inner
                self.doorbell: list[Callable[[], None]] = []

            def put(self, record: UpdateApprovalRecord) -> None:
                self.inner.put(record)

            def get(self, approval_id: str) -> UpdateApprovalRecord | None:
                return self.inner.get(approval_id)

            def consume(self, approval_id: str) -> UpdateApprovalRecord | None:
                for ring in self.doorbell:
                    ring()
                return self.inner.consume(approval_id)

        base = InMemoryUpdateApprovalStore()
        spy = CASDoorbellStore(base)
        # The racing consumer (B) uses the plain inner store directly,
        # as a separate process would.
        racer = UpdateApprovalService(store=base, clock=fixed_clock())
        victim = UpdateApprovalService(store=spy, clock=fixed_clock())

        op = operator()
        record = victim.issue(project_id=PROJECT_ID, project_name=PROJECT, plan_digest=DIGEST, operator=op)
        assert base.get(record.approval_id) is not None

        interleaved = threading.Event()

        def doorbell():
            # B consumes the approval while A sits between validate and CAS.
            stolen = racer.consume(
                record.approval_id,
                project_id=PROJECT_ID,
                plan_digest=DIGEST,
                operator=op,
            )
            assert stolen.state is ApprovalState.CONSUMED
            interleaved.set()

        spy.doorbell.append(doorbell)

        # Consumer A: validate passes, then the CAS loses the race.
        with pytest.raises(UpdateApprovalError) as excinfo:
            victim.consume(
                record.approval_id,
                project_id=PROJECT_ID,
                plan_digest=DIGEST,
                operator=op,
            )
        assert excinfo.value.reason == UpdateApprovalError.APPROVAL_ALREADY_CONSUMED
        assert interleaved.is_set()
        assert base.get(record.approval_id).state is ApprovalState.CONSUMED

    def test_process_local_store_forgets_consumed_state_on_recreation(self):
        """Documented durability boundary: a recreated (or new-process) store
        instance cannot see prior consumption, which is precisely why the
        in-memory store MUST NOT be production's authoritative replay
        protection."""

        base = InMemoryUpdateApprovalStore()
        first = UpdateApprovalService(store=base, clock=fixed_clock())
        record, _ = issue(service=first)

        # Consume in "process one".
        first.consume(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator())

        # "Process two" (fresh instance) has amnesia about the consumption.
        second = UpdateApprovalService(store=InMemoryUpdateApprovalStore(), clock=fixed_clock())
        with pytest.raises(UpdateApprovalError) as excinfo:
            second.validate(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator())
        assert excinfo.value.reason == UpdateApprovalError.APPROVAL_NOT_FOUND

        # But the original instance still enforces single-use.
        with pytest.raises(UpdateApprovalError) as excinfo:
            first.validate(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator())
        assert excinfo.value.reason == UpdateApprovalError.APPROVAL_ALREADY_CONSUMED

    def test_issue_ids_are_unique(self):
        service = make_service()
        seen = {
            service.issue(project_id=PROJECT_ID, project_name=PROJECT, plan_digest=DIGEST, operator=operator()).approval_id
            for _ in range(50)
        }
        assert len(seen) == 50


# ---------------------------------------------------------------------------
# F — per-project single-flight
# ---------------------------------------------------------------------------


class TestSingleFlight:
    def test_acquire_free_project(self):
        control = UpdateFlightControl()
        with control.acquire(PROJECT) as token:
            assert token.project_name == PROJECT

    def test_second_acquire_fails_fast(self):
        control = UpdateFlightControl()
        with control.acquire(PROJECT):
            with pytest.raises(UpdateApprovalError) as excinfo:
                with control.acquire(PROJECT):
                    pass
            assert excinfo.value.reason == UpdateApprovalError.STATE_CONFLICT

    def test_release_then_reacquire(self):
        control = UpdateFlightControl()
        with control.acquire(PROJECT):
            pass
        with control.acquire(PROJECT):
            pass

    def test_exception_in_body_still_releases(self):
        control = UpdateFlightControl()
        with pytest.raises(RuntimeError):
            with control.acquire(PROJECT):
                raise RuntimeError("boom")
        with control.acquire(PROJECT):
            pass

    def test_independent_projects_do_not_block(self):
        control = UpdateFlightControl()
        with control.acquire("alpha"):
            with control.acquire("beta"):
                pass

    def test_malformed_project_identifier_fails_closed(self):
        control = UpdateFlightControl()
        for bad in ("", "a/b", "a\\b", "x" * 129, "bad\nname", None, 7):
            with pytest.raises(UpdateApprovalError):
                with control.acquire(bad):
                    pass
        # nothing leaked into the held map
        with control.acquire("unrelated-project"):
            pass

    def test_release_with_foreign_token_is_idempotent_false(self):
        control = UpdateFlightControl()
        token_outside = _FlightToken("ghost-project")
        assert control.release(token_outside) is False
        with control.acquire(PROJECT) as token:
            # the context manager still owns release while held: releasing the
            # live token early hands ownership over and the second release is False
            assert control.release(token) is True
        assert control.release(token) is False

    def test_concurrent_holders_are_serialized(self):
        """One holder blocks others until release; exactly one wins while held."""

        control = UpdateFlightControl()
        entered = threading.Event()
        may_proceed = threading.Event()
        rejected: list[int] = []

        def holder():
            with control.acquire("contended"):
                entered.set()
                may_proceed.wait(timeout=5)

        thread = threading.Thread(target=holder)
        thread.start()
        assert entered.wait(timeout=5)

        def contender():
            try:
                with control.acquire("contended"):
                    pass
                rejected.append(0)  # should not happen
            except UpdateApprovalError:
                rejected.append(1)

        threads = [threading.Thread(target=contender) for _ in range(4)]
        for contender_thread in threads:
            contender_thread.start()
            contender_thread.join(timeout=5)
        may_proceed.set()
        thread.join(timeout=5)
        assert sum(rejected) == 4
        # after release the project is free again
        with control.acquire("contended"):
            pass


# ---------------------------------------------------------------------------
# G — source boundary: no execution capability, no wiring
# ---------------------------------------------------------------------------


def _read(path: str) -> str:
    from pathlib import Path

    return Path(path).read_text(encoding="utf-8")


def _read_code(path: str) -> str:
    """Source with docstrings and comment lines stripped, for token scans."""

    import ast

    text = _read(path)
    tree = ast.parse(text)
    lines = text.splitlines()
    # Drop expression-statement docstrings (module/class/function).
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            start = (node.lineno or 1) - 1
            end = node.end_lineno or (node.lineno or 1)
            for index in range(start, end):
                lines[index] = ""
    import tokenize

    comment_lines: set[int] = set()
    try:
        with open(path, "rb") as handle:
            for token in tokenize.tokenize(handle.readline):
                if token.type == tokenize.COMMENT:
                    comment_lines.update(range(token.start[0], token.end[0] + 1))
    except Exception:
        pass
    return "\n".join(line for index, line in enumerate(lines, start=1) if index not in comment_lines)


class TestSourceBoundary:
    NEW_MODULES = (
        "src/aipm/services/update/plan_identity.py",
        "src/aipm/services/update/approval.py",
    )

    def test_new_modules_have_no_execution_primitives(self):
        for path in self.NEW_MODULES:
            source = _read_code(path)
            for forbidden in ("subprocess", "os.system", "Popen", "check_output", "check_call"):
                assert forbidden not in source, f"{path} must not reference {forbidden}"

    def test_new_modules_do_not_import_engine_or_control_plane(self):
        for path in self.NEW_MODULES:
            source = _read_code(path)
            assert "from aipm.services.update.engine" not in source
            assert "control_plane" not in source

    def test_engine_does_not_import_new_modules(self):
        source = _read_code("src/aipm/services/update/engine.py")
        # C1 evolution: the engine now binds execution to the canonical plan
        # identity digest (approved architecture). It must consume the
        # canonical derivation only — never re-implement it — and must stay
        # free of approval state machinery.
        assert "from aipm.services.update.plan_identity import UpdatePlanIdentity" in source
        assert "def digest" not in source
        assert "hashlib" not in source
        assert "UpdateApproval" not in source
        assert "UpdateFlightControl" not in source

    def test_dashboard_stays_read_only(self):
        source = _read_code("src/aipm/dashboard/server.py")
        assert "UpdateEngine" not in source
        assert "UpdateApprovalService" not in source
        assert "UpdateFlightControl" not in source

    def test_no_new_mutation_routes_in_dashboard(self):
        import re

        source = _read_code("src/aipm/dashboard/server.py")
        # C5 supersedes the blanket route ban with an exact allow-list: the
        # dashboard may proxy approval and execution to the canonical operator
        # transport, but must never grow a rollback surface or call an
        # execution primitive itself.
        mutation_routes = sorted(re.findall(r"@app\.(?:post|put|patch|delete)\(\"([^\"]+)\"\)", source))
        assert mutation_routes == [
            "/api/projects/{project_id}/update/approve",
            "/api/projects/{project_id}/update/execute",
        ]
        for forbidden in ("execute_update", "update/rollback", "rollback", "subprocess", "Popen"):
            assert forbidden not in source

    def test_update_plan_route_is_declared_get_only(self):
        source = _read_code("src/aipm/dashboard/server.py")
        assert '@app.get("/api/projects/{project_id}/update-plan")' in source
        route_path = "/api/projects/{project_id}/update-plan"
        for method in ("post", "put", "patch", "delete"):
            assert f"@app.{method}(\"{route_path}\")" not in source

    def test_new_modules_do_not_open_sockets_or_files(self):
        for path in self.NEW_MODULES:
            source = _read_code(path)
            for forbidden in ("socket", "requests.", "urllib", "httpx", "os.open"):
                assert forbidden not in source, f"{path} must not reference {forbidden}"

    def test_in_memory_store_documents_process_local_durability_boundary(self):
        """The durability contract must stay explicit and greppable.

        A future execution integration must not be able to accidentally treat
        the in-memory store as durable replay protection: the class contract
        must carry the process-local warning verbatim.
        """

        source = _read("src/aipm/services/update/approval.py")
        assert "PROCESS-LOCAL ONLY" in source
        assert "MUST NOT be used as the authoritative durable approval store" in source
        assert "across process boundaries" in source
        # And the store protocol itself standardizes CAS semantics only.
        assert "Durability boundary" in source
