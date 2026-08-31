"""Pure MC-6.12 lifecycle transition validation.

This module validates logical state changes only. It has no executor, lease,
persistence, target, network, filesystem, or runtime boundary.

Semantic note: the single-owner path drives REQUESTED → PLANNED →
CONFIRMATION_REQUIRED → CONFIRMED (explicit owner confirmation, not
independent approval). Execution-bearing states (LEASED and beyond) exist as
reserved transition targets for the future executor shot; ``advance`` refuses
to enter any state that is not yet backed by an implementation, so no action
is ever claimed executable merely because its lifecycle state exists.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Mapping

from aipm.control_plane.models import ActionLifecycle, ConfirmationKind, LifecycleError, LifecycleState

_TERMINAL_STATES = frozenset({
    LifecycleState.REJECTED,
    LifecycleState.EXPIRED,
    LifecycleState.INVALIDATED,
    LifecycleState.VERIFIED_SUCCESS,
    LifecycleState.EXECUTION_FAILED,
    LifecycleState.ROLLED_BACK,
    LifecycleState.ROLLBACK_UNAVAILABLE,
    LifecycleState.ROLLBACK_FAILED,
})

_ALLOWED_TRANSITIONS: Mapping[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.REQUESTED: frozenset({LifecycleState.PLANNED, LifecycleState.REJECTED, LifecycleState.EXPIRED}),
    LifecycleState.PLANNED: frozenset({LifecycleState.CONFIRMATION_REQUIRED, LifecycleState.INVALIDATED, LifecycleState.EXPIRED}),
    LifecycleState.CONFIRMATION_REQUIRED: frozenset({LifecycleState.CONFIRMED, LifecycleState.REJECTED, LifecycleState.INVALIDATED, LifecycleState.EXPIRED}),
    LifecycleState.CONFIRMED: frozenset({LifecycleState.SNAPSHOT_CAPTURED, LifecycleState.INVALIDATED, LifecycleState.EXPIRED}),
    LifecycleState.SNAPSHOT_CAPTURED: frozenset({LifecycleState.LEASED, LifecycleState.INVALIDATED, LifecycleState.EXPIRED}),
    LifecycleState.LEASED: frozenset({LifecycleState.RUNNING, LifecycleState.RECONCILIATION_REQUIRED, LifecycleState.INVALIDATED, LifecycleState.EXPIRED}),
    LifecycleState.RUNNING: frozenset({LifecycleState.EXECUTED_PENDING_VERIFICATION, LifecycleState.EXECUTION_FAILED, LifecycleState.RECONCILIATION_REQUIRED}),
    LifecycleState.EXECUTED_PENDING_VERIFICATION: frozenset({LifecycleState.VERIFIED_SUCCESS, LifecycleState.VERIFICATION_FAILED}),
    LifecycleState.VERIFICATION_FAILED: frozenset({LifecycleState.ROLLBACK_REQUESTED, LifecycleState.RECONCILIATION_REQUIRED}),
    LifecycleState.EXECUTION_FAILED: frozenset({LifecycleState.ROLLBACK_REQUESTED, LifecycleState.RECONCILIATION_REQUIRED}),
    LifecycleState.CANCEL_REQUESTED: frozenset({LifecycleState.RECONCILIATION_REQUIRED, LifecycleState.INTERRUPTED}),
    LifecycleState.TIMED_OUT: frozenset({LifecycleState.RECONCILIATION_REQUIRED}),
    LifecycleState.INTERRUPTED: frozenset({LifecycleState.RECONCILIATION_REQUIRED}),
    LifecycleState.RECONCILIATION_REQUIRED: frozenset({LifecycleState.VERIFIED_SUCCESS, LifecycleState.ROLLBACK_REQUESTED, LifecycleState.ROLLBACK_UNAVAILABLE}),
    LifecycleState.ROLLBACK_REQUESTED: frozenset({LifecycleState.ROLLED_BACK, LifecycleState.ROLLBACK_UNAVAILABLE, LifecycleState.ROLLBACK_FAILED}),
}

#: States a lifecycle may actually reach in the current implementation, each
#: backed by real machinery: CONFIRMED by the confirmation service;
#: SNAPSHOT_CAPTURED by the durable snapshot composite; LEASED, RUNNING and
#: EXECUTED_PENDING_VERIFICATION by the bounded executor and its lease
#: grantor; VERIFIED_SUCCESS/VERIFICATION_FAILED by the independent
#: verification contract; EXECUTION_FAILED/RECONCILIATION_REQUIRED by the
#: outcome classifier; ROLLBACK_REQUESTED/ROLLED_BACK/ROLLBACK_FAILED by the
#: rollback executor. CANCEL_REQUESTED, TIMED_OUT, INTERRUPTED and
#: ROLLBACK_UNAVAILABLE remain reserved until an implementation backs them.
IMPLEMENTED_STATES = frozenset({
    LifecycleState.REQUESTED,
    LifecycleState.PLANNED,
    LifecycleState.CONFIRMATION_REQUIRED,
    LifecycleState.CONFIRMED,
    LifecycleState.SNAPSHOT_CAPTURED,
    LifecycleState.LEASED,
    LifecycleState.RUNNING,
    LifecycleState.EXECUTED_PENDING_VERIFICATION,
    LifecycleState.VERIFIED_SUCCESS,
    LifecycleState.VERIFICATION_FAILED,
    LifecycleState.EXECUTION_FAILED,
    LifecycleState.RECONCILIATION_REQUIRED,
    LifecycleState.ROLLBACK_REQUESTED,
    LifecycleState.ROLLED_BACK,
    LifecycleState.ROLLBACK_FAILED,
    LifecycleState.REJECTED,
    LifecycleState.EXPIRED,
    LifecycleState.INVALIDATED,
})

_CONFIRMATION_STATES = frozenset({LifecycleState.CONFIRMED})


def allowed_transitions(state: LifecycleState) -> frozenset[LifecycleState]:
    """Return the immutable legal successors for a logical state."""

    normalized = state if isinstance(state, LifecycleState) else LifecycleState(state)
    return _ALLOWED_TRANSITIONS.get(normalized, frozenset())


def terminal_states() -> frozenset[LifecycleState]:
    return _TERMINAL_STATES


def implemented_states() -> frozenset[LifecycleState]:
    return IMPLEMENTED_STATES


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
    if target not in IMPLEMENTED_STATES:
        raise LifecycleError(f"Lifecycle state {target.value} is reserved for a future implementation")
    if now is not None:
        value = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        if value >= current.expires_at and target is not LifecycleState.EXPIRED:
            raise LifecycleError("Expired lifecycle cannot advance")
    if target in _CONFIRMATION_STATES:
        if not actor_subject:
            raise LifecycleError("Confirmation requires an authenticated confirmer")
        if current.confirmation_kind is ConfirmationKind.DISTINCT_APPROVAL:
            if actor_subject == current.requester_subject:
                raise LifecycleError("Requester cannot confirm its own action in distinct mode")
        elif current.confirmation_kind is ConfirmationKind.OWNER_CONFIRMATION:
            if actor_subject != current.requester_subject:
                raise LifecycleError("Only the authenticated owner may confirm this action")
        if current.approver_subject is not None and current.approver_subject != actor_subject:
            raise LifecycleError("Confirmation subject does not match the recorded confirmer")


def advance(
    current: ActionLifecycle,
    next_state: LifecycleState,
    *,
    now: datetime | None = None,
    actor_subject: str | None = None,
) -> ActionLifecycle:
    """Return a new lifecycle value after pure transition validation."""

    validate_transition(current, next_state, now=now, actor_subject=actor_subject)
    target = next_state if isinstance(next_state, LifecycleState) else LifecycleState(next_state)
    if target in _CONFIRMATION_STATES and actor_subject:
        return replace(current, state=target, approver_subject=actor_subject, version=current.version + 1)
    return replace(current, state=target, version=current.version + 1)
