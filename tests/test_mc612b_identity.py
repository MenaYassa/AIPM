from __future__ import annotations

from datetime import datetime, timezone

from aipm.control_plane.approval import ApprovalService
from aipm.control_plane.audit import ActionAuditRepository
from aipm.control_plane.identity import PLAN_IDENTITY_VERSION, canonical_plan_bytes, identity_vector
from aipm.control_plane.models import ActionRequest, OperationKind
from aipm.control_plane.planner import PlanOnlyPlanner

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def request(key: str = "idem-001") -> ActionRequest:
    return ActionRequest(OperationKind.UPDATE_PROJECT_PLAN, "project-demo", key, (("reason", "review"),))


def test_planner_approval_audit_identity_alignment() -> None:
    req = request()
    plan = PlanOnlyPlanner(clock=lambda: NOW, target_allow_list={"project-demo"}).plan(req)
    binding = ApprovalService(clock=lambda: NOW, target_allow_list={"project-demo"}).request(req, actor_id="system")
    record = ActionAuditRepository(clock=lambda: NOW, target_allow_list={"project-demo"}).append_plan(req, actor_id="system", now=NOW)
    assert binding.plan_id == plan.plan_id == record.plan_id
    assert binding.plan_digest == plan.digest == record.plan_digest
    assert len(plan.plan_id) == 32
    assert len(plan.digest) == 64
    assert PLAN_IDENTITY_VERSION == "mc612a-plan-v1"


def test_identity_vector_is_deterministic_and_canonical_bytes_are_utf8() -> None:
    plan = PlanOnlyPlanner(clock=lambda: NOW, target_allow_list={"project-demo"}).plan(request())
    first = identity_vector(plan)
    second = identity_vector(plan)
    assert first == second
    assert canonical_plan_bytes(plan).decode("utf-8").startswith("{")
    assert "evidence" in canonical_plan_bytes(plan).decode("utf-8")


def test_identity_changes_for_identity_bearing_fields() -> None:
    planner = PlanOnlyPlanner(clock=lambda: NOW, target_allow_list={"project-demo"})
    baseline = planner.plan(request("idem-001"))
    changed = planner.plan(request("idem-002"))
    assert baseline.plan_id != changed.plan_id
    assert baseline.digest != changed.digest
