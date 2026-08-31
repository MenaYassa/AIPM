"""Shared storage contracts for the control plane.

Production uses the SQLite implementations in ``control_plane.storage``; tests
use the in-memory implementations that live beside their domain modules. Both
implementations obey exactly the same domain contracts defined here — there is
one lifecycle state machine, one identity derivation, and one set of semantic
rules; only the storage medium differs.

No protocol in this module can execute anything: every method persists or
reads control-plane state only.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aipm.control_plane.identity import ActionIdentity
    from aipm.control_plane.kill_switch import KillSwitch, KillSwitchState
    from aipm.control_plane.models import ActionLifecycle, ConfirmationBinding
    from aipm.control_plane.policy import AuthorizationDecision
    from aipm.control_plane.project_plan import ProjectPlan


@runtime_checkable
class ProjectPlanStore(Protocol):
    """Durable or in-memory store of staging ProjectPlans with CAS updates."""

    def create(self, plan: "ProjectPlan") -> "ProjectPlan": ...

    def read(self, target_id: str) -> "ProjectPlan": ...

    def update(self, target_id: str, *, expected_revision: int, fields: Mapping[str, str], now: Any) -> "ProjectPlan": ...


@runtime_checkable
class ConfirmationStore(Protocol):
    """Persistence face for owner confirmation bindings."""

    def put(self, binding: "ConfirmationBinding") -> None: ...

    def get(self, confirmation_id: str) -> "ConfirmationBinding | None": ...

    def get_active_for_action(self, action_id: str) -> "ConfirmationBinding | None": ...

    def has_active_for_action(self, action_id: str) -> bool: ...

    def count(self) -> int: ...

    def as_mapping(self) -> Mapping[str, "ConfirmationBinding"]: ...


class LifecycleTransition:
    """Typed description of one CAS-guarded lifecycle transition."""

    __slots__ = ("action_id", "expected_version", "next_state", "approver_subject", "now")

    def __init__(self, *, action_id: str, expected_version: int, next_state: Any, approver_subject: str, now: Any) -> None:
        self.action_id = action_id
        self.expected_version = expected_version
        self.next_state = next_state
        self.approver_subject = approver_subject
        self.now = now


@runtime_checkable
class ActionRepository(Protocol):
    """Durable or in-memory authority for decisions, actions, and confirmations.

    Implementations must persist the canonical values exactly as issued: no
    identity re-derivation, no second digest, no alternate state machine. The
    database-level uniqueness constraint on
    ``(target_id, operation, idempotency_key)`` is the ultimate idempotency
    protection.
    """

    def register_action(self, decision: "AuthorizationDecision", lifecycle: "ActionLifecycle") -> None: ...

    def get_decision(self, decision_id: str) -> "AuthorizationDecision | None": ...

    def get_action(self, action_id: str) -> "ActionLifecycle | None": ...

    def find_action_by_idempotency(self, *, target_id: str, operation: str, idempotency_key: str) -> "ActionLifecycle | None": ...

    def advance_action(self, action_id: str, *, expected_version: int, next_state: Any, approver_subject: str, now: Any) -> "ActionLifecycle": ...

    def put_confirmation(self, binding: "ConfirmationBinding") -> None: ...

    def get_confirmation(self, confirmation_id: str) -> "ConfirmationBinding | None": ...

    def has_active_for_action(self, action_id: str) -> bool: ...

    def count(self) -> int: ...

    def as_mapping(self) -> Mapping[str, "ConfirmationBinding"]: ...

    def record_confirmation_with_advance(self, binding: "ConfirmationBinding", transition: LifecycleTransition) -> "ActionLifecycle": ...


@runtime_checkable
class KillSwitchStore(Protocol):
    """Persistence face for kill-switch state; missing record means engaged."""

    def record_for(self, environment: Any) -> "KillSwitch | None": ...

    def save(self, switch: "KillSwitch", *, epoch: int, actor_subject: str | None) -> None: ...

    def records(self) -> tuple["KillSwitch", ...]: ...


@runtime_checkable
class LeaseRepository(Protocol):
    """Future execution-lease persistence; not wired to any executor yet."""

    def save(self, lease: Any) -> None: ...

    def get(self, lease_id: str) -> Any | None: ...

    def leases_for_action(self, action_id: str) -> tuple[Any, ...]: ...


@runtime_checkable
class PlanSnapshotRepository(Protocol):
    """Immutable before-image persistence for future rollback; append-only."""

    def save(self, snapshot: Any) -> None: ...

    def get(self, snapshot_id: str) -> Any | None: ...

    def snapshots_for_target(self, target_id: str) -> tuple[Any, ...]: ...
