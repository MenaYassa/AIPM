from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from aipm.control_plane.lifecycle import advance, allowed_transitions, implemented_states, terminal_states, validate_transition
from aipm.control_plane.models import ActionLifecycle, ActionScope, ConfirmationKind, LifecycleError, LifecycleState, OperationKind

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def lifecycle(**overrides):
    values = {
        "action_id": "action-001",
        "plan_id": "plan-001",
        "plan_digest": "a" * 64,
        "operation": OperationKind.UPDATE_PROJECT_PLAN,
        "scope": ActionScope("project-demo", "staging", "policy-v1"),
        "state": LifecycleState.REQUESTED,
        "requester_subject": "local-owner",
        "idempotency_key": "idem-001",
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=15),
    }
    values.update(overrides)
    return ActionLifecycle(**values)


def test_owner_confirmation_flow_is_pure_and_increments_version():
    original = lifecycle()
    planned = advance(original, LifecycleState.PLANNED, now=NOW)
    pending = advance(planned, LifecycleState.CONFIRMATION_REQUIRED, now=NOW)
    confirmed = advance(pending, LifecycleState.CONFIRMED, now=NOW, actor_subject="local-owner")
    assert original.state is LifecycleState.REQUESTED
    assert planned.state is LifecycleState.PLANNED
    assert pending.state is LifecycleState.CONFIRMATION_REQUIRED
    assert confirmed.state is LifecycleState.CONFIRMED
    assert confirmed.approver_subject == "local-owner"
    assert confirmed.version == 3


def test_owner_confirmation_requires_the_authenticated_owner():
    pending = lifecycle(state=LifecycleState.CONFIRMATION_REQUIRED)
    with pytest.raises(LifecycleError, match="owner"):
        advance(pending, LifecycleState.CONFIRMED, now=NOW, actor_subject="someone-else")
    with pytest.raises(LifecycleError, match="confirmer"):
        validate_transition(pending, LifecycleState.CONFIRMED, now=NOW, actor_subject=None)


def test_distinct_mode_rejects_self_confirmation_but_accepts_other_subject():
    pending = lifecycle(state=LifecycleState.CONFIRMATION_REQUIRED, confirmation_kind=ConfirmationKind.DISTINCT_APPROVAL)
    with pytest.raises(LifecycleError, match="distinct"):
        advance(pending, LifecycleState.CONFIRMED, now=NOW, actor_subject="local-owner")
    confirmed = advance(pending, LifecycleState.CONFIRMED, now=NOW, actor_subject="human-bob")
    assert confirmed.approver_subject == "human-bob"
    with pytest.raises(LifecycleError, match="distinct"):
        lifecycle(state=LifecycleState.CONFIRMATION_REQUIRED, confirmation_kind=ConfirmationKind.DISTINCT_APPROVAL, approver_subject="local-owner")
    with pytest.raises(LifecycleError, match="requesting owner"):
        lifecycle(state=LifecycleState.CONFIRMATION_REQUIRED, approver_subject="someone-else")


def test_expired_lifecycle_can_only_move_to_expired():
    current = lifecycle(state=LifecycleState.PLANNED)
    with pytest.raises(LifecycleError, match="Expired"):
        validate_transition(current, LifecycleState.CONFIRMATION_REQUIRED, now=current.expires_at)
    expired = advance(current, LifecycleState.EXPIRED, now=current.expires_at)
    assert expired.state is LifecycleState.EXPIRED


def test_illegal_and_terminal_transitions_are_rejected():
    with pytest.raises(LifecycleError, match="Illegal"):
        validate_transition(lifecycle(), LifecycleState.RUNNING, now=NOW)
    with pytest.raises((LifecycleError, ValueError)):
        lifecycle(state=LifecycleState.VERIFIED_SUCCESS)
    assert LifecycleState.VERIFIED_SUCCESS in terminal_states()
    assert allowed_transitions(LifecycleState.VERIFIED_SUCCESS) == frozenset()


def test_unimplemented_control_states_are_reserved_and_refused():
    # Shot 6 backs LEASED and the execution states with the bounded executor;
    # the never-implemented control states remain reserved and refused.
    from aipm.control_plane.lifecycle import advance as _adv
    for reserved in (LifecycleState.CANCEL_REQUESTED, LifecycleState.TIMED_OUT, LifecycleState.INTERRUPTED, LifecycleState.ROLLBACK_UNAVAILABLE):
        assert reserved not in implemented_states()
    confirmed = lifecycle(state=LifecycleState.CONFIRMATION_REQUIRED)
    confirmed = advance(confirmed, LifecycleState.CONFIRMED, now=NOW, actor_subject="local-owner")
    snap = _adv(confirmed, LifecycleState.SNAPSHOT_CAPTURED, now=NOW)
    reconciliation = lifecycle(
        state=LifecycleState.RECONCILIATION_REQUIRED,
        approver_subject="local-owner",
    )
    with pytest.raises(LifecycleError, match="reserved"):
        _adv(reconciliation, LifecycleState.ROLLBACK_UNAVAILABLE, now=NOW)
    assert implemented_states() <= set(LifecycleState)


def test_lifecycle_model_is_immutable_and_canonical():
    value = lifecycle()
    assert '"operation":"update_project_plan"' in value.canonical()
    assert '"confirmation_kind":"owner_confirmation"' in value.canonical()
    with pytest.raises((FrozenInstanceError, AttributeError)):
        value.state = LifecycleState.RUNNING


def test_lifecycle_rejects_unsupported_operation_and_bad_digest():
    with pytest.raises((LifecycleError, ValueError)):
        lifecycle(operation="other")
    with pytest.raises(LifecycleError):
        lifecycle(plan_digest="not-a-digest")
