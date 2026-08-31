from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.audit import InMemoryAuditLedger
from aipm.control_plane.audit import builders as audit_builders
from aipm.control_plane.identity import (
    ACTION_IDENTITY_VERSION,
    PLAN_IDENTITY_VERSION,
    OwnerPrincipal,
    AuthenticationMethod,
    PrincipalVerification,
    canonical_plan_bytes,
    identity_vector,
)
from aipm.control_plane.models import ActionRequest, OperationKind
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import AuthorizationPolicy
from aipm.control_plane.project_plan import Environment, ProjectPlan

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def owner_principal() -> OwnerPrincipal:
    return OwnerPrincipal(
        subject="local-owner",
        issuer="aipm-owner-auth",
        authentication_method=AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE,
        verification=PrincipalVerification.VERIFIED,
        auth_epoch=1,
        authenticated_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
    )


def request(key: str = "idem-001") -> ActionRequest:
    return ActionRequest(OperationKind.UPDATE_PROJECT_PLAN, "project-demo", key, (("title", "Next title"),))


def current_plan() -> ProjectPlan:
    return ProjectPlan.create(
        target_id="project-demo",
        environment=Environment.STAGING,
        title="Old title",
        objective="Objective",
        now=NOW,
    )


def test_planner_policy_confirmation_audit_identity_alignment() -> None:
    req = request()
    plan = PlanOnlyPlanner(clock=lambda: NOW, target_allow_list={"project-demo"}).plan(req)
    decision = AuthorizationPolicy(
        policy_version="policy-v1",
        allowed_scopes=frozenset({("project-demo", "staging")}),
    ).authorize(owner_principal(), req, plan, current_plan(), now=NOW)
    identity = decision.action_identity
    assert identity is not None
    binding = OwnerConfirmationService(clock=lambda: NOW).request_confirmation(decision, requester_subject="local-owner")
    event = InMemoryAuditLedger().append(
        audit_builders.action_created(
            actor_subject="local-owner",
            occurred_at=NOW,
            decision=decision,
            lifecycle_from="requested",
            lifecycle_to="confirmation_required",
        )
    )
    assert binding.plan_id == plan.plan_id == event.draft.plan_id == identity.plan_id
    assert binding.plan_digest == plan.digest == event.draft.plan_digest == identity.plan_digest
    assert binding.action_id == event.draft.action_id == identity.action_id
    assert binding.target_revision == event.draft.plan_revision == identity.target_revision == 1
    assert len(plan.plan_id) == 32
    assert len(plan.digest) == 64
    assert len(identity.action_id) == 64
    assert PLAN_IDENTITY_VERSION == "mc612a-plan-v1"
    assert ACTION_IDENTITY_VERSION == "mc612-action-identity-v1"


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
