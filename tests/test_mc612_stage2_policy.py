from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aipm.control_plane.identity import (
    AuthenticationMethod,
    OwnerPrincipal,
    PrincipalVerification,
)
from aipm.control_plane.models import (
    ActionRequest,
    ConfirmationKind,
    OperationKind,
)
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import AuthorizationPolicy, PolicyCode, allowed_operations, validate_operation
from aipm.control_plane.project_plan import Environment, ProjectPlan

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def principal(**overrides):
    values = {
        "subject": "local-owner",
        "issuer": "aipm-owner-auth",
        "authentication_method": AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE,
        "verification": PrincipalVerification.VERIFIED,
        "auth_epoch": 1,
        "authenticated_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "roles": ("owner",),
    }
    values.update(overrides)
    return OwnerPrincipal(**values)


def request(**overrides):
    values = {
        "operation": OperationKind.UPDATE_PROJECT_PLAN,
        "target_id": "project-demo",
        "idempotency_key": "idem-001",
        "metadata": (("title", "New title"),),
        "environment": "staging",
    }
    values.update(overrides)
    return ActionRequest(**values)


def current_plan(**overrides):
    values = {
        "target_id": "project-demo",
        "environment": Environment.STAGING,
        "title": "Old title",
        "objective": "Objective",
        "now": NOW,
    }
    values.update(overrides)
    return ProjectPlan.create(**values)


def policy(**overrides):
    values = {
        "policy_version": "policy-v1",
        "allowed_scopes": frozenset({("project-demo", "staging")}),
    }
    values.update(overrides)
    return AuthorizationPolicy(**values)


def authorize(**overrides):
    values = {
        "principal": principal(),
        "request": request(),
        "current": current_plan(),
        "now": NOW,
        "policy_config": None,
    }
    values.update(overrides)
    plan = PlanOnlyPlanner(clock=lambda: values["now"], target_allow_list={"project-demo", "project-other"}).plan(values["request"])
    policy_value = values["policy_config"] or policy()
    return policy_value.authorize(
        values["principal"],
        values["request"],
        plan,
        values["current"],
        now=values["now"],
    )


def test_policy_allows_verified_owner_for_allow_listed_scope_and_plan():
    decision = authorize()
    assert decision.allowed is True
    assert decision.code is PolicyCode.ALLOWED
    assert decision.confirmation_required is True
    assert decision.action_identity is not None
    assert decision.action_identity.target_revision == 1
    assert decision.plan_digest == decision.action_identity.plan_digest
    assert decision.plan_revision == decision.action_identity.target_revision
    assert decision.principal_subject == "local-owner"
    assert decision.confirmation_kind is ConfirmationKind.OWNER_CONFIRMATION
    assert decision.safe_dict()["policy_version"] == "policy-v1"
    assert decision.safe_dict()["action_identity"]["action_id"] == decision.action_identity.action_id


def test_allowed_decision_expires_within_policy_window():
    decision = authorize()
    assert decision.expires_at > decision.decided_at
    assert decision.expires_at - decision.decided_at <= timedelta(minutes=5)
    assert not decision.is_expired(NOW + timedelta(minutes=4, seconds=59))
    assert decision.is_expired(NOW + timedelta(minutes=5))


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"principal": None}, PolicyCode.UNVERIFIED_IDENTITY),
        ({"principal": principal(verification=PrincipalVerification.UNVERIFIED)}, PolicyCode.UNVERIFIED_IDENTITY),
        ({"principal": principal(expires_at=NOW + timedelta(seconds=1)), "now": NOW + timedelta(seconds=1)}, PolicyCode.EXPIRED_IDENTITY),
        ({"principal": principal(roles=("auditor",))}, PolicyCode.MISSING_ROLE),
        ({"request": request(target_id="project-other")}, PolicyCode.TARGET_NOT_ALLOWED),
        ({"request": request(environment="production")}, PolicyCode.ENVIRONMENT_NOT_ALLOWED),
        ({"current": None}, PolicyCode.PLAN_MISSING),
        ({"current": current_plan(enabled=False)}, PolicyCode.PLAN_DISABLED),
        ({"request": request(metadata=())}, PolicyCode.FIELD_NOT_ALLOWED),
        ({"request": request(metadata=(("nickname", "value"),))}, PolicyCode.FIELD_NOT_ALLOWED),
    ],
)
def test_policy_denies_invalid_identity_operation_scope_plan_or_fields(overrides, code):
    decision = authorize(**overrides)
    assert decision.allowed is False
    assert decision.code is code
    assert decision.action_identity is None
    assert decision.confirmation_required is False


def test_unsupported_operations_cannot_reach_the_policy():
    with pytest.raises(ValueError):
        request(operation="reboot_host")
    with pytest.raises(ValueError):
        validate_operation("reboot_host")


def test_policy_denies_disabled_plan_explicitly():
    decision = authorize(current=current_plan(enabled=False))
    assert decision.code is PolicyCode.PLAN_DISABLED


def test_policy_denies_when_target_state_is_missing():
    decision = authorize(current=None)
    assert decision.code is PolicyCode.PLAN_MISSING


def test_policy_is_deterministic_for_identical_inputs():
    first = authorize()
    second = authorize()
    assert first.decision_id == second.decision_id
    assert first.safe_dict() == second.safe_dict()


def test_decision_id_changes_for_changed_security_relevant_inputs():
    base = authorize()
    changed_field = authorize(request=request(metadata=(("title", "Different"),)))
    changed_target = authorize(request=request(target_id="project-other"))
    assert base.decision_id != changed_field.decision_id
    assert base.decision_id != changed_target.decision_id


def test_policy_denies_forged_plan_without_leaking_details():
    from aipm.control_plane.models import ActionPlan

    genuine = PlanOnlyPlanner(clock=lambda: NOW, target_allow_list={"project-demo", "project-other"}).plan(request())
    forged = object.__new__(ActionPlan)
    for field in ActionPlan.__dataclass_fields__:
        object.__setattr__(forged, field, getattr(genuine, field))
    object.__setattr__(forged, "plan_id", "0" * 32)
    decision = policy().authorize(principal(), request(), forged, current_plan(), now=NOW)
    assert decision.allowed is False
    assert decision.code is PolicyCode.INVALID_REQUEST
    assert decision.safe_dict()["action_identity"] is None


def test_denial_reasons_do_not_leak_request_content():
    request_value = request(metadata=(("title", "Private Plan Title"),))
    decision = authorize(request=request_value, principal=None)
    rendered = str(sorted(decision.safe_dict().items()))
    assert "Private Plan Title" not in rendered
    assert decision.safe_dict()["principal_subject"] is None


def test_policy_cannot_be_configured_with_another_operation_or_rule_disabled():
    with pytest.raises(ValueError):
        AuthorizationPolicy(policy_version="policy-v1", allowed_scopes={("project-demo", "staging")}, allowed_operations=frozenset())
    with pytest.raises(ValueError):
        AuthorizationPolicy(policy_version="policy-v1", allowed_scopes={("project-demo", "staging")}, require_distinct_requester_approver=False)
    with pytest.raises(ValueError):
        AuthorizationPolicy(policy_version="policy-v1", allowed_scopes={("project-demo", "staging")}, allowed_fields=frozenset())
    assert allowed_operations() == frozenset({OperationKind.UPDATE_PROJECT_PLAN, OperationKind.ROLLBACK_PROJECT_PLAN})


def test_policy_supports_explicit_distinct_confirmation_mode_for_future_use():
    distinct = policy(confirmation_kind=ConfirmationKind.DISTINCT_APPROVAL)
    assert distinct.confirmation_kind is ConfirmationKind.DISTINCT_APPROVAL
    decision = authorize(policy_config=distinct)
    assert decision.allowed is True
    assert decision.confirmation_kind is ConfirmationKind.DISTINCT_APPROVAL


def test_policy_produces_identical_identity_for_equivalent_requests():
    left = authorize(request=request(metadata=(("objective", "b"), ("title", "a"))))
    right = authorize(request=request(metadata=(("title", "a"), ("objective", "b"))))
    assert left.action_identity is not None and right.action_identity is not None
    assert left.action_identity == right.action_identity
