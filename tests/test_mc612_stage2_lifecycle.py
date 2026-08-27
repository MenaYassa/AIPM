from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from aipm.control_plane.lifecycle import advance, allowed_transitions, terminal_states, validate_transition
from aipm.control_plane.models import ActionLifecycle, ActionScope, LifecycleError, LifecycleState, OperationKind

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def lifecycle(**overrides):
    values = {
        "action_id": "action-001",
        "plan_id": "plan-001",
        "plan_digest": "a" * 64,
        "operation": OperationKind.UPDATE_PROJECT_PLAN,
        "scope": ActionScope("project-demo", "staging", "policy-v1"),
        "state": LifecycleState.REQUESTED,
        "requester_subject": "human-alice",
        "idempotency_key": "idem-001",
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=15),
    }
    values.update(overrides)
    return ActionLifecycle(**values)


def test_legal_plan_and_approval_transitions_are_pure_and_increment_version():
    original = lifecycle()
    planned = advance(original, LifecycleState.PLANNED, now=NOW)
    review = advance(planned, LifecycleState.APPROVAL_REQUESTED, now=NOW)
    approved_source = lifecycle(
        state=LifecycleState.APPROVAL_REQUESTED,
        approver_subject="human-bob",
        version=2,
    )
    approved = advance(approved_source, LifecycleState.APPROVED, now=NOW, actor_subject="human-bob")
    assert original.state is LifecycleState.REQUESTED
    assert planned.state is LifecycleState.PLANNED
    assert review.state is LifecycleState.APPROVAL_REQUESTED
    assert approved.state is LifecycleState.APPROVED
    assert approved.version == 3


def test_approval_requires_distinct_approver_and_requester_cannot_approve():
    pending = lifecycle(state=LifecycleState.APPROVAL_REQUESTED)
    with pytest.raises(LifecycleError, match="approver"):
        validate_transition(pending, LifecycleState.APPROVED, now=NOW)
    with pytest.raises(LifecycleError, match="distinct"):
        lifecycle(state=LifecycleState.APPROVAL_REQUESTED, approver_subject="human-alice")


def test_expired_lifecycle_can_only_move_to_expired():
    current = lifecycle(state=LifecycleState.PLANNED)
    with pytest.raises(LifecycleError, match="Expired"):
        validate_transition(current, LifecycleState.APPROVAL_REQUESTED, now=current.expires_at)
    expired = advance(current, LifecycleState.EXPIRED, now=current.expires_at)
    assert expired.state is LifecycleState.EXPIRED


def test_illegal_and_terminal_transitions_are_rejected():
    with pytest.raises(LifecycleError, match="Illegal"):
        validate_transition(lifecycle(), LifecycleState.RUNNING, now=NOW)
    terminal = lifecycle(state=LifecycleState.VERIFIED_SUCCESS, approver_subject="human-bob")
    with pytest.raises(LifecycleError, match="Terminal"):
        validate_transition(terminal, LifecycleState.ROLLBACK_REQUESTED, now=NOW)
    assert LifecycleState.VERIFIED_SUCCESS in terminal_states()
    assert allowed_transitions(LifecycleState.VERIFIED_SUCCESS) == frozenset()


def test_execution_states_require_approved_action():
    with pytest.raises(LifecycleError, match="approver"):
        lifecycle(state=LifecycleState.APPROVED)
    not_approved = lifecycle(state=LifecycleState.PLANNED, approver_subject="human-bob")
    with pytest.raises(LifecycleError, match="Illegal"):
        validate_transition(not_approved, LifecycleState.LEASED, now=NOW)
    approved = lifecycle(state=LifecycleState.APPROVED, approver_subject="human-bob")
    leased = advance(approved, LifecycleState.LEASED, now=NOW)
    assert leased.state is LifecycleState.LEASED


def test_lifecycle_model_is_immutable_and_canonical():
    value = lifecycle(approver_subject="human-bob")
    assert '"operation":"update_project_plan"' in value.canonical()
    with pytest.raises((FrozenInstanceError, AttributeError)):
        value.state = LifecycleState.RUNNING


def test_lifecycle_rejects_unsupported_operation_and_bad_digest():
    with pytest.raises((LifecycleError, ValueError)):
        lifecycle(operation="other")
    with pytest.raises(LifecycleError):
        lifecycle(plan_digest="not-a-digest")
