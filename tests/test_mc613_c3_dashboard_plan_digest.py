"""C3: the canonical update plan digest surfaced in the dashboard (read-only).

Proves that GET /api/projects/{project_id}/update-plan exposes exactly the
canonical UpdatePlanIdentity digest of the plan it renders, that the digest
obeys the approved identity classification (presentation/observation fields
excluded), and that the surface stays read-only, sanitized, and isolated
from execution machinery.
"""
from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from aipm.capabilities.dashboard import update_api as update_api_module
from aipm.capabilities.dashboard.safety import assert_safe_payload
from aipm.capabilities.dashboard.update_api import DashboardUpdateApi
from aipm.dashboard.server import create_app
from aipm.models.git import GitRepository
from aipm.models.health import HealthState
from aipm.models.health_report import HealthReport
from aipm.models.update import UpdatePlan, UpdateRisk
from aipm.services.update.plan_identity import UpdatePlanIdentity

from test_dashboard_update_api import (
    BAD_ID,
    VALID_ID,
    DetailApplication,
    RecordingIntelligence,
    RecordingPlanner,
    StubApi,
    make_client,
    sample_plan,
)


HEX64 = re.compile(r"^[0-9a-f]{64}$")


def make_update_api(plan: UpdatePlan) -> tuple[DashboardUpdateApi, RecordingPlanner]:
    intelligence = RecordingIntelligence("demo")
    recording = RecordingPlanner(plan)
    return DashboardUpdateApi(intelligence, recording), recording


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
        last_fetch=datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
        last_commit_message="feat: thing",
        last_commit_author="Mina",
    )
    fields.update(overrides)
    return GitRepository(**fields)


def make_health(**overrides) -> HealthReport:
    fields = dict(
        project="demo",
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
# Digest presence, shape, and canonical equality
# ---------------------------------------------------------------------------


def test_valid_update_plan_response_contains_plan_digest():
    api, _ = make_update_api(sample_plan())
    response = make_client(api).get(f"/api/projects/{VALID_ID}/update-plan")
    assert response.status_code == 200
    plan = response.json()["update_plan"]
    assert "plan_digest" in plan
    assert HEX64.fullmatch(plan["plan_digest"])


def test_returned_digest_equals_canonical_identity_digest():
    plan_model = sample_plan()
    api, _ = make_update_api(plan_model)
    body = make_client(api).get(f"/api/projects/{VALID_ID}/update-plan").json()
    assert body["update_plan"]["plan_digest"] == UpdatePlanIdentity.from_plan(plan_model).digest()


def test_digest_is_exactly_64_lowercase_hex():
    api, _ = make_update_api(sample_plan())
    digest = make_client(api).get(f"/api/projects/{VALID_ID}/update-plan").json()["update_plan"]["plan_digest"]
    assert len(digest) == 64
    assert digest == digest.lower()
    assert HEX64.fullmatch(digest) is not None


def test_identical_plans_produce_identical_digest():
    first, _ = make_update_api(sample_plan())
    second, _ = make_update_api(sample_plan())
    digest_one = make_client(first).get(f"/api/projects/{VALID_ID}/update-plan").json()["update_plan"]["plan_digest"]
    digest_two = make_client(second).get(f"/api/projects/{VALID_ID}/update-plan").json()["update_plan"]["plan_digest"]
    assert digest_one == digest_two
    assert digest_one == UpdatePlanIdentity.from_plan(sample_plan()).digest()


# ---------------------------------------------------------------------------
# Approved identity classification: what changes / does not change the digest
# ---------------------------------------------------------------------------


def _digest_for(plan: UpdatePlan) -> str:
    api, _ = make_update_api(plan)
    return make_client(api).get(f"/api/projects/{VALID_ID}/update-plan").json()["update_plan"]["plan_digest"]


def test_security_relevant_field_change_changes_digest():
    baseline = sample_plan()
    for mutate in (
        lambda p: sample_plan(risk=UpdateRisk.HIGH),
        lambda p: sample_plan(proceed=not p.proceed),
        lambda p: sample_plan(approval_required=not p.approval_required),
        lambda p: sample_plan(snapshot_required=not p.snapshot_required),
        lambda p: sample_plan(stash_required=True),
        lambda p: sample_plan(pull_required=True),
        lambda p: sample_plan(reasons=["different reason"]),
        lambda p: sample_plan(actions=["different action"]),
        lambda p: sample_plan(project="other"),
    ):
        mutated = mutate(baseline)
        assert _digest_for(mutated) != _digest_for(baseline)


def test_git_state_change_changes_digest():
    without_git = sample_plan(git=None)
    with_git = sample_plan(git=make_git())
    assert _digest_for(without_git) != _digest_for(with_git)
    assert _digest_for(with_git) == UpdatePlanIdentity.from_plan(with_git).digest()
    dirty = sample_plan(git=make_git(dirty=True))
    assert _digest_for(dirty) != _digest_for(with_git)


def test_health_state_change_changes_digest():
    without_health = sample_plan(health_before=None)
    with_health = sample_plan(health_before=make_health())
    assert _digest_for(without_health) != _digest_for(with_health)
    degraded = sample_plan(health_before=make_health(state=HealthState.DEGRADED, score=40))
    assert _digest_for(degraded) != _digest_for(with_health)


def test_excluded_presentation_fields_do_not_change_digest():
    baseline = sample_plan()
    # project_path is a filesystem location — excluded from identity.
    other_path = sample_plan(project_path="/different/location")
    assert _digest_for(other_path) == _digest_for(baseline)
    # Remote URL is presentation/observation-only — excluded.
    https_url = sample_plan(git=make_git(remote_url="https://example.com/demo.git"))
    ssh_url = sample_plan(git=make_git(remote_url="git@example.com:demo.git"))
    assert _digest_for(https_url) == _digest_for(ssh_url)
    # Observation timestamps and free-text commit metadata are excluded.
    early = sample_plan(git=make_git(last_fetch=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    late = sample_plan(git=make_git(last_fetch=datetime(2030, 6, 1, tzinfo=timezone.utc)))
    assert _digest_for(early) == _digest_for(late)
    authored = sample_plan(git=make_git(last_commit_message="feat: one", last_commit_author="Someone"))
    assert _digest_for(authored) == _digest_for(sample_plan(git=make_git()))
    # Health free-text findings/recommendations are excluded.
    chatty = sample_plan(health_before=make_health(findings=["disk failing"], recommendations=["replace disk"]))
    assert _digest_for(chatty) == _digest_for(sample_plan(health_before=make_health()))


def test_sanitized_text_is_digested_as_canonical_unsanitized_content():
    """The digest binds the plan the operator saw, but the canonical identity
    is derived from the plan itself, not the sanitized display strings: the
    dashboard never re-hashes display text (no second digest definition)."""

    plan_model = sample_plan(reasons=["Declared Compose file is missing: /home/ubuntu/aipm/projects/demo/compose.yaml"])
    api, _ = make_update_api(plan_model)
    body = make_client(api).get(f"/api/projects/{VALID_ID}/update-plan").json()
    assert body["update_plan"]["plan_digest"] == UpdatePlanIdentity.from_plan(plan_model).digest()


# ---------------------------------------------------------------------------
# Error behavior and payload safety remain unchanged
# ---------------------------------------------------------------------------


def test_malformed_project_id_behavior_unchanged():
    api, _ = make_update_api(sample_plan())
    client = make_client(api)
    for bad in (BAD_ID, "   ", "xyz", "a" * 23, "a" * 25, "A" * 24, "g" * 24):
        body = client.get(f"/api/projects/{bad}/update-plan").json()
        assert body["error"] == "Project identifier is invalid", bad
        assert body["update_plan"] is None


def test_unknown_project_behavior_unchanged():
    api, _ = make_update_api(sample_plan())
    response = make_client(api).get("/api/projects/" + "b" * 24 + "/update-plan")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["error"] == "Project is unavailable"
    assert body["update_plan"] is None
    assert "plan_digest" not in (body["update_plan"] or {})


def test_existing_payload_sanitization_remains_intact():
    api, _ = make_update_api(sample_plan())
    body = make_client(api).get(f"/api/projects/{VALID_ID}/update-plan").json()
    serialized = str(body)
    assert "/tmp/should-not-appear" not in serialized
    assert "/home/ubuntu/aipm" not in serialized
    assert "password" not in serialized.lower()
    assert_safe_payload(body)


def test_existing_payload_fields_remain_intact():
    api, _ = make_update_api(sample_plan())
    plan = make_client(api).get(f"/api/projects/{VALID_ID}/update-plan").json()["update_plan"]
    # The pre-C3 fields are preserved; plan_digest is added, nothing renamed.
    assert set(plan) == {
        "project",
        "dry_run",
        "proceed",
        "approval_required",
        "risk",
        "reasons",
        "actions",
        "snapshot_required",
        "estimated_restart",
        "stash_required",
        "pull_required",
        "plan_digest",
    }


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def test_no_mutation_occurs_and_no_state_is_created():
    plan_model = sample_plan()
    api, recording = make_update_api(plan_model)
    response = make_client(api).get(f"/api/projects/{VALID_ID}/update-plan")
    assert response.status_code == 200
    assert response.request.method == "GET"
    # The planner was invoked exactly once, read-only (dry_run=True).
    assert recording.calls == [{"project_name": "demo", "dry_run": True}]
    # The exposed object graph gained nothing.
    public = [name for name in vars(api) if not name.startswith("_")]
    assert public == ["intelligence", "planner", "clock"]


def test_update_plan_route_is_get_only():
    api, _ = make_update_api(sample_plan())
    client = make_client(api)
    routes = {getattr(route, "path", None): getattr(route, "methods", None) for route in client.app.routes}
    assert routes["/api/projects/{project_id}/update-plan"] == {"GET"}


def test_no_approval_or_confirmation_state_is_created():
    api, _ = make_update_api(sample_plan())
    make_client(api).get(f"/api/projects/{VALID_ID}/update-plan")
    source = Path("src/aipm/capabilities/dashboard/update_api.py").read_text(encoding="utf-8")
    for forbidden in ("UpdateApprovalService", "UpdateApprovalRecord", "InMemoryUpdateApprovalStore", "UpdateFlightControl", "ConfirmationBinding"):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# Structural isolation
# ---------------------------------------------------------------------------


def test_no_local_digest_or_hash_implementation_in_dashboard():
    """The digest must be the canonical one only: no hashlib/JSON hashing in
    dashboard code."""

    source = inspect.getsource(update_api_module)
    assert "from aipm.services.update.plan_identity import UpdatePlanIdentity" in source
    for forbidden in ("hashlib", "sha256(", "json.dumps", "canonical_json", "hexdigest"):
        assert forbidden not in source, forbidden


def test_dashboard_update_api_does_not_import_execution_machinery():
    source = inspect.getsource(update_api_module)
    for forbidden in (
        "from aipm.services.update.engine",
        "from aipm.services.update.executor",
        "from aipm.control_plane",
        "UpdateEngine",
        "subprocess",
        "import docker",
    ):
        assert forbidden not in source, forbidden


def test_planner_sees_only_read_only_plan_call():
    api, recording = make_update_api(sample_plan())
    make_client(api).get(f"/api/projects/{VALID_ID}/update-plan")
    assert recording.calls == [{"project_name": "demo", "dry_run": True}]
