"""Recovery manager: durable, first-class restart/reconciliation handling.

Recovery operates on durable action state after any interruption (process
crash, restart, worker termination, lease expiry, lost provider response).
It NEVER blindly retries a mutation: the entry state determines the safe
continuation, and every recovery transition goes through the same CAS
composites and audit evidence as live execution.

Recovery contract per non-terminal state:

    REQUESTED/PLANNED/CONFIRMATION_REQUIRED  → stale (pre-lease) — no action
    SNAPSHOT_CAPTURED                        → lease expired → INVALIDATED path;
                                               otherwise ready for execution
    LEASED                                   → validate lease; expired lease →
                                               reconcile outcome; else ready
    RUNNING                                  → UNKNOWN/observation path only
    EXECUTED_PENDING_VERIFICATION            → verification (fresh read-back)
    UNKNOWN_OUTCOME outcome                  → reconcile by observation only
    terminal states                          → no recovery action
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aipm.control_plane.models import LifecycleState
from aipm.control_plane.verification import ExecutionOutcome

RECOVERY_VERSION = "mc612-recovery-v1"


class RecoveryError(ValueError):
    """Raised when recovery cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """Bounded result of one recovery evaluation."""

    action_id: str
    entry_state: LifecycleState
    exit_state: LifecycleState | None
    outcome: ExecutionOutcome | None
    recovered: bool
    reason_code: str
    recovery_version: str = RECOVERY_VERSION

    def safe_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "entry_state": self.entry_state.value,
            "exit_state": self.exit_state.value if self.exit_state else None,
            "outcome": self.outcome.value if self.outcome else None,
            "recovered": self.recovered,
            "reason_code": self.reason_code,
            "recovery_version": self.recovery_version,
        }


class RecoveryManager:
    """Stateless evaluator over durable action state; no blind retries."""

    __slots__ = ("_actions", "_plans", "_clock", "_initialized")

    def __init__(self, *, actions, plans, clock=None) -> None:
        if actions is None or not hasattr(actions, "get_action"):
            raise TypeError("RecoveryManager requires the action repository")
        if plans is None or not hasattr(plans, "read"):
            raise TypeError("RecoveryManager requires the plan store")
        from datetime import datetime as _dt, timezone as _tz

        object.__setattr__(self, "_actions", actions)
        object.__setattr__(self, "_plans", plans)
        object.__setattr__(self, "_clock", clock or (lambda: _dt.now(_tz.utc)))
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("RecoveryManager configuration is immutable")
        object.__setattr__(self, name, value)

    def recover(self, action_id: str, *, now: datetime | None = None) -> RecoveryOutcome:
        action = self._actions.get_action(action_id)
        if action is None:
            raise RecoveryError("Unknown action")
        moment = now if now is not None else self._clock()
        state = action.state
        outcome_value = self._actions.outcome_for_action(action_id)

        if state in {
            LifecycleState.VERIFIED_SUCCESS,
            LifecycleState.ROLLED_BACK,
            LifecycleState.ROLLBACK_FAILED,
            LifecycleState.EXECUTION_FAILED,
            LifecycleState.REJECTED,
            LifecycleState.EXPIRED,
            LifecycleState.INVALIDATED,
        }:
            return RecoveryOutcome(action_id, state, None, None, False, "terminal_no_action")
        if state in {LifecycleState.REQUESTED, LifecycleState.PLANNED, LifecycleState.CONFIRMATION_REQUIRED}:
            return RecoveryOutcome(action_id, state, None, None, False, "pre_lease_stale")
        if state is LifecycleState.SNAPSHOT_CAPTURED:
            return RecoveryOutcome(action_id, state, None, None, False, "ready_for_execution")
        if state is LifecycleState.LEASED:
            lease = self._actions.active_lease(action_id, now=moment) if hasattr(self._actions, "active_lease") else None
            if lease is None:
                # Expired lease: atomically advance to RECONCILIATION_REQUIRED.
                advanced = self._actions.advance_action(
                    action_id,
                    expected_version=action.version,
                    next_state=LifecycleState.RECONCILIATION_REQUIRED,
                    approver_subject="control-plane-recovery",
                    now=moment,
                )
                return RecoveryOutcome(action_id, advanced.state, advanced.state, ExecutionOutcome.MUTATION_NOT_STARTED, True, "lease_expired_reconciliation_required")
            return RecoveryOutcome(action_id, state, None, None, False, "lease_active_ready")
        if state is LifecycleState.RUNNING:
            if outcome_value == ExecutionOutcome.UNKNOWN_OUTCOME.value:
                return RecoveryOutcome(action_id, state, None, ExecutionOutcome.UNKNOWN_OUTCOME, False, "reconciliation_required")
            # RUNNING without a recorded outcome: the process died before the
            # outcome marker; classify conservatively as unknown.
            return RecoveryOutcome(action_id, state, None, ExecutionOutcome.UNKNOWN_OUTCOME, False, "outcome_marker_missing")
        if state is LifecycleState.EXECUTED_PENDING_VERIFICATION:
            return RecoveryOutcome(action_id, state, None, None, False, "verification_resumable")
        if state is LifecycleState.ROLLBACK_REQUESTED:
            return RecoveryOutcome(action_id, state, None, None, False, "rollback_resumable")
        if state is LifecycleState.RECONCILIATION_REQUIRED:
            return RecoveryOutcome(action_id, state, None, ExecutionOutcome.UNKNOWN_OUTCOME, False, "reconciliation_required")
        return RecoveryOutcome(action_id, state, None, None, False, "unrecognized_state")

    def scan(self, *, now: datetime | None = None) -> list[RecoveryOutcome]:
        """Recover every durable action; bounded, restart-safe."""

        outcomes: list[RecoveryOutcome] = []
        for action_id in self._action_ids():
            outcomes.append(self.recover(action_id, now=now))
        return outcomes

    def _action_ids(self) -> list[str]:
        actions = getattr(self._actions, "_actions", {})
        if actions:  # in-memory double
            return list(actions)
        rows = self._actions._db.connection.execute(
            "SELECT action_id FROM actions ORDER BY created_at"
        ).fetchall()
        return [row["action_id"] for row in rows]
