"""Startup reconciliation sweep over non-terminal durable actions.

This module composes the EXISTING recovery authority: it enumerates every
durable action that is not in a terminal lifecycle state and routes each one
through the canonical ``RecoveryManager``. It adds no new transition, no new
authority, and no execution capability:

- REQUESTED/PLANNED/CONFIRMATION_REQUIRED  → observed as stale (pre-lease)
- SNAPSHOT_CAPTURED                        → observed as ready for execution
- LEASED with an active lease              → observed as ready
- LEASED with an expired lease             → the single existing safe
                                             transition (CAS advance to
                                             RECONCILIATION_REQUIRED)
- RUNNING / UNKNOWN_OUTCOME                → observation only; never resumed
- EXECUTED_PENDING_VERIFICATION            → observed as verification-resumable
- terminal states                          → never enumerated

The sweep never resumes or retries runtime work, never consumes or recreates
confirmations, never acquires leases, never touches rollbacks, and is
idempotent: re-running it over unchanged state yields the same observations,
and the one mutating transition is CAS-guarded so concurrent sweeps cannot
double-apply (a loser observes a stale-version conflict as an isolated,
bounded error while the durable state remains correct).

Enumeration failure fails closed (the error propagates); a failure on one
action is isolated and aggregated without leaking tracebacks.
"""
from __future__ import annotations

from dataclasses import dataclass

from aipm.control_plane.recovery import RecoveryManager, RecoveryOutcome, RECOVERY_VERSION

DEFAULT_SWEEP_LIMIT = 1000

_MAX_ERROR_TEXT = 256


class RecoverySweepError(ValueError):
    """Raised when the sweep cannot be performed safely."""


@dataclass(frozen=True, slots=True)
class RecoverySweepActionError:
    """Bounded, traceback-free failure isolated to a single action."""

    action_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RecoverySweepResult:
    """Aggregate of one bounded sweep; observational except CAS-advanced ids."""

    scanned: int
    outcomes: tuple[RecoveryOutcome, ...]
    errors: tuple[RecoverySweepActionError, ...]
    advanced_action_ids: tuple[str, ...]
    recovery_version: str = RECOVERY_VERSION

    def safe_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "advanced_action_ids": list(self.advanced_action_ids),
            "errors": [{"action_id": error.action_id, "reason": error.reason} for error in self.errors],
            "recovery_version": self.recovery_version,
        }


def reconcile_non_terminal_actions(
    *,
    actions,
    plans,
    clock=None,
    limit: int = DEFAULT_SWEEP_LIMIT,
) -> RecoverySweepResult:
    """Enumerate non-terminal actions and recover each via RecoveryManager.

    Requires a repository that exposes ``non_terminal_action_ids``; the sweep
    deliberately has no fallback enumeration so it can never widen its own
    scope to terminal actions.
    """

    if not hasattr(actions, "non_terminal_action_ids"):
        raise RecoverySweepError("Sweep requires a repository with non_terminal_action_ids")
    if limit < 1:
        raise RecoverySweepError("Sweep limit must be positive")
    manager = RecoveryManager(actions=actions, plans=plans, clock=clock)
    # Fail closed: enumeration failure propagates; nothing is recovered on a
    # partial or unknown enumeration.
    action_ids = actions.non_terminal_action_ids(limit=limit)

    outcomes: list[RecoveryOutcome] = []
    errors: list[RecoverySweepActionError] = []
    for action_id in action_ids:
        try:
            outcomes.append(manager.recover(action_id))
        except ValueError as exc:
            # Typed boundary failures (ControlPlaneError, LifecycleError,
            # RecoveryError) are isolated to this action and bounded.
            errors.append(RecoverySweepActionError(action_id=action_id, reason=str(exc)[:_MAX_ERROR_TEXT]))
    advanced = tuple(outcome.action_id for outcome in outcomes if outcome.recovered)
    return RecoverySweepResult(
        scanned=len(outcomes) + len(errors),
        outcomes=tuple(outcomes),
        errors=tuple(errors),
        advanced_action_ids=advanced,
    )
