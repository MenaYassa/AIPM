"""Pure MC-6.12 Stage 2 lifecycle transition validation.

This module validates logical state changes only. It has no executor, lease,
persistence, target, network, filesystem, or runtime boundary.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Mapping

from aipm.control_plane.models import ActionLifecycle, LifecycleError, LifecycleState

_TERMINAL_STATES = frozenset({
    LifecycleState.REJECTED,
    LifecycleState.EXPIRED,
    LifecycleState.INVALIDATED,
    LifecycleState.VERIFIED_SUCCESS,
    LifecycleState.ROLLED_BACK,
    LifecycleState.ROLLBACK_UNAVAILABLE,
    LifecycleState.ROLLBACK_FAILED,
})

_ALLOWED_TRANSITIONS: Mapping[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.REQUESTED: frozenset({LifecycleState.PLANNED, LifecycleState.REJECTED, LifecycleState.EXPIRED}),
    LifecycleState.PLANNED: frozenset({LifecycleState.APPROVAL_REQUESTED, LifecycleState.INVALIDATED, LifecycleState.EXPIRED}),
    LifecycleState.APPROVAL_REQUESTED: frozenset({LifecycleState.APPROVED, LifecycleState.REJECTED, LifecycleState.INVALIDATED, LifecycleState.EXPIRED}),
    LifecycleState.APPROVED: frozenset({LifecycleState.LEASED, LifecycleState.INVALIDATED, LifecycleState.EXPIRED, LifecycleState.CANCEL_REQUESTED}),
    LifecycleState.LEASED: frozenset({LifecycleState.RUNNING, LifecycleState.CANCEL_REQUESTED, LifecycleState.INTERRUPTED}),
    LifecycleState.RUNNING: frozenset({LifecycleState.EXECUTED_PENDING_VERIFICATION, LifecycleState.CANCEL_REQUESTED, LifecycleState.TIMED_OUT, LifecycleState.INTERRUPTED}),
    LifecycleState.EXECUTED_PENDING_VERIFICATION: frozenset({LifecycleState.VERIFIED_SUCCESS, LifecycleState.VERIFICATION_FAILED, LifecycleState.ROLLBACK_REQUESTED}),
    LifecycleState.VERIFICATION_FAILED: frozenset({LifecycleState.ROLLBACK_REQUESTED, LifecycleState.RECONCILIATION_REQUIRED}),
    LifecycleState.CANCEL_REQUESTED: frozenset({LifecycleState.RECONCILIATION_REQUIRED, LifecycleState.INTERRUPTED}),
    LifecycleState.TIMED_OUT: frozenset({LifecycleState.RECONCILIATION_REQUIRED}),
    LifecycleState.INTERRUPTED: frozenset({LifecycleState.RECONCILIATION_REQUIRED}),
    LifecycleState.RECONCILIATION_REQUIRED: frozenset({LifecycleState.VERIFIED_SUCCESS, LifecycleState.ROLLBACK_REQUESTED, LifecycleState.ROLLBACK_UNAVAILABLE}),
    LifecycleState.ROLLBACK_REQUESTED: frozenset({LifecycleState.ROLLED_BACK, LifecycleState.ROLLBACK_UNAVAILABLE, LifecycleState.ROLLBACK_FAILED}),
}


def allowed_transitions(state: LifecycleState) -> frozenset[LifecycleState]:
    """Return the immutable legal successors for a logical state."""

    normalized = state if isinstance(state, LifecycleState) else LifecycleState(state)
    return _ALLOWED_TRANSITIONS.get(normalized, frozenset())


def terminal_states() -> frozenset[LifecycleState]:
    return _TERMINAL_STATES


def validate_transition(
    current: ActionLifecycle,
    next_state: LifecycleState,
    *,
    now: datetime | None = None,
    actor_subject: str | None = None,
) -> None:
    """Validate one logical transition without causing any side effect."""

    if not isinstance(current, ActionLifecycle):
        raise LifecycleError("Invalid lifecycle value")
    try:
        target = next_state if isinstance(next_state, LifecycleState) else LifecycleState(next_state)
    except (TypeError, ValueError) as exc:
        raise LifecycleError("Invalid lifecycle state") from exc
    if current.state in _TERMINAL_STATES:
        raise LifecycleError("Terminal lifecycle state cannot transition")
    if target not in allowed_transitions(current.state):
        raise LifecycleError(f"Illegal lifecycle transition: {current.state.value} to {target.value}")
    if now is not None:
        value = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        if value >= current.expires_at and target is not LifecycleState.EXPIRED:
            raise LifecycleError("Expired lifecycle cannot advance")
    if target is LifecycleState.APPROVED and not current.approver_subject:
        raise LifecycleError("Approval requires a distinct approver")
    if target is LifecycleState.APPROVED and actor_subject and actor_subject == current.requester_subject:
        raise LifecycleError("Requester cannot approve its own action")
    if target in {LifecycleState.LEASED, LifecycleState.RUNNING, LifecycleState.EXECUTED_PENDING_VERIFICATION} and not current.approver_subject:
        raise LifecycleError("Execution lifecycle requires an approved action")


def advance(
    current: ActionLifecycle,
    next_state: LifecycleState,
    *,
    now: datetime | None = None,
    actor_subject: str | None = None,
) -> ActionLifecycle:
    """Return a new lifecycle value after pure transition validation."""

    validate_transition(current, next_state, now=now, actor_subject=actor_subject)
    return replace(current, state=next_state if isinstance(next_state, LifecycleState) else LifecycleState(next_state), version=current.version + 1)
