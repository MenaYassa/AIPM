from datetime import datetime, timedelta, timezone

import pytest

from aipm.control_plane.project_plan import (
    Environment,
    InMemoryProjectPlanStore,
    PlanConflict,
    ProjectPlan,
    ProjectPlanError,
    allowed_fields,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def make_plan():
    return ProjectPlan.create(
        target_id="plan-stage-1",
        environment=Environment.STAGING,
        title="Initial plan",
        objective="Bounded staging objective",
        now=NOW,
    )


def test_project_plan_has_exact_field_allow_list_and_deterministic_digest():
    plan = make_plan()
    assert allowed_fields() == frozenset({"title", "objective"})
    assert plan.revision == 1
    assert plan.digest() == plan.canonical_digest
    assert plan.safe_dict()["target_id"] == "plan-stage-1"
    assert plan.safe_dict()["environment"] == "staging"


def test_project_plan_rejects_production_and_invalid_values():
    with pytest.raises(ProjectPlanError, match="production"):
        ProjectPlan.create(
            target_id="plan-prod-1",
            environment=Environment.PRODUCTION,
            title="Production",
            objective="Disabled",
            now=NOW,
        )
    with pytest.raises(ProjectPlanError):
        ProjectPlan.create(
            target_id="plan-stage-1",
            environment=Environment.STAGING,
            title=" title",
            objective="Objective",
            now=NOW,
        )


def test_project_plan_update_is_immutable_and_cas_guarded():
    plan = make_plan()
    updated = plan.update(
        expected_revision=1,
        fields={"title": "Updated"},
        now=NOW + timedelta(minutes=1),
    )
    assert plan.title == "Initial plan"
    assert updated.title == "Updated"
    assert updated.objective == plan.objective
    assert updated.revision == 2
    assert updated.canonical_digest != plan.canonical_digest
    with pytest.raises(PlanConflict):
        plan.update(expected_revision=0, fields={"title": "Stale"}, now=NOW)
    with pytest.raises(ProjectPlanError):
        plan.update(expected_revision=1, fields={"metadata": "blocked"}, now=NOW)


def test_project_plan_store_is_staging_only_and_reads_back_canonical_state():
    store = InMemoryProjectPlanStore()
    plan = store.create(make_plan())
    assert store.read(plan.target_id) == plan
    updated = store.update(
        plan.target_id,
        expected_revision=1,
        fields={"objective": "Updated objective"},
        now=NOW + timedelta(minutes=1),
    )
    assert store.read(plan.target_id).safe_dict() == updated.safe_dict()
    with pytest.raises(PlanConflict):
        store.update(plan.target_id, expected_revision=1, fields={"title": "Conflict"}, now=NOW)
