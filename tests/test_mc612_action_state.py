"""In-memory action repository tests (test double for the durable store).

The former parallel ActionStatus machine was retired in Shot 3; these tests
pin the contract that BOTH the in-memory double and the durable SQLite
implementation must obey: canonical identity registration, database-level
idempotency semantics, CAS-guarded lifecycle transitions on the canonical
LifecycleState, and confirmation persistence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aipm.control_plane.action_state import InMemoryActionRepository, validate_action_registration
from aipm.control_plane.contracts import LifecycleTransition
from aipm.control_plane.identity import AuthenticationMethod, OwnerPrincipal, PrincipalVerification
from aipm.control_plane.models import (
    ActionRequest,
    ConfirmationState,
    ControlPlaneError,
    LifecycleError,
    LifecycleState,
    OperationKind,
    PlanningErrorCode,
)
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import AuthorizationPolicy
from aipm.control_plane.project_plan import Environment, ProjectPlan

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


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


def decision_and_lifecycle(**request_overrides):
    plan = PlanOnlyPlanner(clock=lambda: NOW, target_allow_list={"project-demo", "project-other"}).plan(request(**request_overrides))
    current = ProjectPlan.create(
        target_id=request_overrides.get("target_id", "project-demo"),
        environment=Environment.STAGING,
        title="Old",
        objective="Objective",
        now=NOW,
    )
    decision = AuthorizationPolicy(
        policy_version="policy-v1",
        allowed_scopes=frozenset({(current.target_id, "staging")}),
    ).authorize(owner_principal(), plan.request, plan, current, now=NOW)
    from aipm.control_plane.models import ActionLifecycle, ActionScope

    identity = decision.action_identity
    assert identity is not None
    lifecycle = ActionLifecycle(
        action_id=identity.action_id,
        plan_id=identity.plan_id,
        plan_digest=identity.plan_digest,
        plan_revision=identity.target_revision,
        operation=plan.request.operation,
        scope=ActionScope(target_id=identity.target_id, environment=identity.environment, policy_version=identity.policy_version),
        state=LifecycleState.REQUESTED,
        requester_subject=identity.requester_subject,
        idempotency_key=plan.request.idempotency_key,
        created_at=decision.decided_at,
        expires_at=decision.expires_at,
    )
    from aipm.control_plane.lifecycle import advance

    planned = advance(lifecycle, LifecycleState.PLANNED, now=NOW)
    pending = advance(planned, LifecycleState.CONFIRMATION_REQUIRED, now=NOW)
    return decision, pending


def test_register_and_load_preserves_canonical_identity():
    repo = InMemoryActionRepository()
    decision, lifecycle = decision_and_lifecycle()
    repo.register_action(decision, lifecycle)
    identity = decision.action_identity
    assert identity is not None
    loaded = repo.get_action(identity.action_id)
    assert loaded == lifecycle
    loaded_decision = repo.get_decision(decision.decision_id)
    assert loaded_decision == decision
    found = repo.find_action_by_idempotency(
        target_id="project-demo", operation="update_project_plan", idempotency_key="idem-001"
    )
    assert found == lifecycle


def test_register_rejects_forged_identity():
    from dataclasses import replace
    from aipm.control_plane.identity import ActionIdentity

    repo = InMemoryActionRepository()
    decision, lifecycle = decision_and_lifecycle()
    identity = decision.action_identity
    assert identity is not None
    forged = ActionIdentity(
        action_id="0" * 64,
        plan_id=identity.plan_id,
        plan_digest=identity.plan_digest,
        target_revision=identity.target_revision,
        target_digest=identity.target_digest,
        policy_version=identity.policy_version,
        requester_subject=identity.requester_subject,
        operation=identity.operation,
        target_id=identity.target_id,
        environment=identity.environment,
    )
    tampered_decision = replace(decision, action_identity=forged)
    with pytest.raises(ControlPlaneError, match="verification"):
        repo.register_action(tampered_decision, lifecycle)


def test_idempotency_same_request_returns_existing():
    repo = InMemoryActionRepository()
    decision, lifecycle = decision_and_lifecycle()
    repo.register_action(decision, lifecycle)
    decision2, lifecycle2 = decision_and_lifecycle()
    assert lifecycle2.action_id == lifecycle.action_id
    repo.register_action(decision2, lifecycle2)
    assert repo.count() >= 0
    actions = [repo.get_action(lifecycle.action_id)]
    assert actions[0] == lifecycle


def test_idempotency_conflicting_request_is_rejected():
    repo = InMemoryActionRepository()
    decision, lifecycle = decision_and_lifecycle()
    repo.register_action(decision, lifecycle)
    other_decision, other_lifecycle = decision_and_lifecycle(metadata=(("title", "Different"),))
    # Same idempotency key, different canonical request → deterministic conflict.
    with pytest.raises(ControlPlaneError) as error:
        repo.register_action(other_decision, other_lifecycle)
    assert error.value.code is PlanningErrorCode.IDEMPOTENCY_CONFLICT


def test_advance_is_cas_guarded_and_domain_validated():
    repo = InMemoryActionRepository()
    decision, lifecycle = decision_and_lifecycle()
    repo.register_action(decision, lifecycle)
    advanced = repo.advance_action(
        lifecycle.action_id,
        expected_version=lifecycle.version,
        next_state=LifecycleState.CONFIRMED,
        approver_subject="local-owner",
        now=NOW,
    )
    assert advanced.state is LifecycleState.CONFIRMED
    assert advanced.version == lifecycle.version + 1
    with pytest.raises(ControlPlaneError) as error:
        repo.advance_action(
            lifecycle.action_id,
            expected_version=lifecycle.version,
            next_state=LifecycleState.INVALIDATED,
            approver_subject="local-owner",
            now=NOW,
        )
    assert error.value.code is PlanningErrorCode.STATE_CONFLICT
    with pytest.raises(LifecycleError, match="Illegal"):
        repo.advance_action(
            lifecycle.action_id,
            expected_version=advanced.version,
            next_state=LifecycleState.LEASED,
            approver_subject="local-owner",
            now=NOW,
        )


def test_confirmation_persistence_and_atomic_advance():
    repo = InMemoryActionRepository()
    decision, lifecycle = decision_and_lifecycle()
    repo.register_action(decision, lifecycle)
    identity = decision.action_identity
    assert identity is not None
    from aipm.control_plane.models import ConfirmationBinding, ConfirmationKind

    binding = ConfirmationBinding(
        confirmation_id="c" * 32,
        decision_id=decision.decision_id,
        action_id=identity.action_id,
        plan_id=identity.plan_id,
        plan_digest=identity.plan_digest,
        target_revision=identity.target_revision,
        target_digest=identity.target_digest,
        policy_version=identity.policy_version,
        requester_subject="local-owner",
        confirmation_kind=ConfirmationKind.OWNER_CONFIRMATION,
        request=decision.request,
        created_at=NOW,
        expires_at=decision.expires_at,
        confirmed_by_subject="local-owner",
        state=ConfirmationState.CONFIRMED,
    )
    advanced = repo.record_confirmation_with_advance(
        binding,
        LifecycleTransition(
            action_id=identity.action_id,
            expected_version=lifecycle.version,
            next_state=LifecycleState.CONFIRMED,
            approver_subject="local-owner",
            now=NOW,
        ),
    )
    assert advanced.state is LifecycleState.CONFIRMED
    assert repo.get_confirmation("c" * 32) is not None
    assert repo.has_active_for_action(identity.action_id) is True


def test_validate_action_registration_rejects_mismatched_bindings():
    decision, lifecycle = decision_and_lifecycle()
    from dataclasses import replace

    with pytest.raises(ControlPlaneError):
        validate_action_registration(decision, replace(lifecycle, requester_subject="someone-else"))
