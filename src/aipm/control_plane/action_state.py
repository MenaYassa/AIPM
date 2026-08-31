"""In-memory action repository (test double) for the canonical action plane.

The former parallel ``ActionStatus`` state machine was retired: the canonical
``LifecycleState`` domain model (``lifecycle.py``) is the single semantic
authority, and this repository persists/loads those values without inventing
transition rules. The durable SQLite implementation in
``control_plane.storage`` obeys exactly the same contract and the same
validation; only the medium differs.

This module performs no execution and touches no external system. The
idempotency contract ``(target_id, operation, idempotency_key)`` is enforced
here and, in the durable implementation, by a database UNIQUE constraint as
the ultimate protection.
"""
from __future__ import annotations

from typing import Any, Mapping

from aipm.control_plane.contracts import LifecycleTransition
from aipm.control_plane.identity import verify_action_identity
from aipm.control_plane.lifecycle import advance as advance_lifecycle
from aipm.control_plane.models import (
    ActionLifecycle,
    ConfirmationBinding,
    ConfirmationState,
    ControlPlaneError,
    LifecycleState,
    PlanningErrorCode,
)

_MAX_RECORDS = 4096
_ACTIVE_CONFIRMATION_STATES = frozenset({ConfirmationState.CONFIRMATION_REQUESTED, ConfirmationState.CONFIRMED})


def validate_action_registration(decision, lifecycle) -> None:
    """Shared registration coherence checks used by every implementation.

    Fails closed on forged identities, mismatched decision/action bindings, or
    lifecycle values that do not carry the canonical identity.
    """

    from aipm.control_plane.identity import ActionIdentity
    from aipm.control_plane.policy import AuthorizationDecision

    if not isinstance(decision, AuthorizationDecision):
        raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid decision registration")
    if not isinstance(lifecycle, ActionLifecycle):
        raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid action registration")
    if not decision.allowed or decision.action_identity is None:
        raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Only allowed decisions can be registered")
    identity: ActionIdentity = decision.action_identity
    if not verify_action_identity(identity):
        raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Action identity failed verification")
    if lifecycle.action_id != identity.action_id or lifecycle.plan_id != identity.plan_id:
        raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Action does not carry the decision identity")
    if lifecycle.plan_digest != identity.plan_digest or lifecycle.scope.policy_version != identity.policy_version:
        raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Action does not carry the decision identity")
    if lifecycle.plan_revision != identity.target_revision:
        raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Action does not carry the bound plan revision")
    if lifecycle.requester_subject != identity.requester_subject:
        raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Action requester does not match the decision")
    if lifecycle.decision_id and lifecycle.decision_id != decision.decision_id:
        raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Action references a different decision")
    if lifecycle.operation.value == "rollback_project_plan":
        if not lifecycle.rollback_of_action_id or not lifecycle.snapshot_id:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Rollback actions must reference their original action and snapshot")
        if lifecycle.rollback_of_action_id == lifecycle.action_id:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Rollback actions cannot reference themselves")
    if decision.request is None or decision.request.idempotency_key != lifecycle.idempotency_key:
        raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Action idempotency key does not match the decision request")


class InMemoryActionRepository:
    """Bounded in-memory implementation of the ActionRepository contract.

    Like the durable implementation it accepts an optional audit sink so the
    composition can commit state transitions and their evidence together.
    """

    def __init__(self, *, max_records: int = _MAX_RECORDS, audit=None, plans=None) -> None:
        if max_records < 1:
            raise ValueError("Invalid action record bound")
        self._max_records = max_records
        self._audit = audit
        self._plans = plans
        self._decisions: dict = {}
        self._actions: dict = {}
        self._idempotency: dict = {}
        self._confirmations: dict = {}
        self._snapshots: dict = {}
        self._verifications: dict = {}

    def _append_evidence(self, drafts) -> None:
        if not self._audit or not drafts:
            return
        for draft in drafts:
            self._audit.append_in_transaction(draft)

    # -- decisions + actions ------------------------------------------------

    def register_action(self, decision, lifecycle, *, audit_drafts=()) -> None:
        validate_action_registration(decision, lifecycle)
        key = (lifecycle.scope.target_id, lifecycle.operation.value, lifecycle.idempotency_key)
        existing_action_id = self._idempotency.get(key)
        if existing_action_id is not None:
            if existing_action_id == lifecycle.action_id and decision.request is not None:
                return
            raise ControlPlaneError(PlanningErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency key already bound to a different request")
        if lifecycle.action_id in self._actions:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Duplicate action identity")
        if len(self._actions) >= self._max_records:
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Action record bound reached")
        self._decisions[decision.decision_id] = decision
        self._actions[lifecycle.action_id] = lifecycle
        self._idempotency[key] = lifecycle.action_id
        self._append_evidence(audit_drafts)

    def get_decision(self, decision_id: str):
        return self._decisions.get(decision_id)

    def get_action(self, action_id: str):
        return self._actions.get(action_id)

    def find_action_by_idempotency(self, *, target_id: str, operation: str, idempotency_key: str):
        action_id = self._idempotency.get((target_id, operation, idempotency_key))
        if action_id is None:
            return None
        return self._actions.get(action_id)

    def advance_action(self, action_id: str, *, expected_version: int, next_state, approver_subject: str, now: Any, audit_drafts=()) -> ActionLifecycle:
        lifecycle = self._actions.get(action_id)
        if lifecycle is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Unknown action")
        if lifecycle.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        advanced = advance_lifecycle(lifecycle, next_state, now=now, actor_subject=approver_subject)
        self._actions[action_id] = advanced
        self._append_evidence(audit_drafts)
        return advanced

    def capture_snapshot_and_advance(self, snapshot, *, action_id: str, expected_version: int, now, audit_drafts=()) -> ActionLifecycle:
        """Test-double composite: snapshot insert + CAS advance + evidence."""

        from aipm.control_plane.models import LifecycleState as _LifecycleState

        if snapshot.action_id != action_id:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Snapshot is bound to a different action")
        if snapshot.revision != (self._actions.get(action_id).plan_revision if self._actions.get(action_id) else None):
            raise ControlPlaneError(PlanningErrorCode.STALE_EVIDENCE, "Snapshot is stale against the action revision")
        current = self._actions.get(action_id)
        if current is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Unknown action")
        if current.state is not _LifecycleState.CONFIRMED:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Snapshot capture requires a confirmed action")
        if action_id in self._snapshots:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Snapshot identity already exists")
        advanced = self.advance_action(
            action_id,
            expected_version=expected_version,
            next_state=_LifecycleState.SNAPSHOT_CAPTURED,
            approver_subject=current.requester_subject,
            now=now,
        )
        self._snapshots[snapshot.snapshot_id] = snapshot
        self._append_evidence(audit_drafts)
        return advanced

    def snapshot_for_action(self, action_id: str):
        for snapshot in self._snapshots.values():
            if snapshot.action_id == action_id:
                return snapshot
        return None

    def mark_outcome(self, action_id: str, *, expected_version: int, outcome: str, now, audit_drafts=()) -> ActionLifecycle:
        from aipm.control_plane.verification import ExecutionOutcome

        try:
            normalized = outcome if isinstance(outcome, ExecutionOutcome) else ExecutionOutcome(outcome)
        except ValueError as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid execution outcome") from exc
        current = self._actions.get(action_id)
        if current is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Unknown action")
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        self._outcomes = getattr(self, "_outcomes", {})
        stored = self._outcomes.get(action_id)
        if stored == ExecutionOutcome.UNKNOWN_OUTCOME.value and normalized is ExecutionOutcome.MUTATION_NOT_STARTED:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Unknown outcome forbids reset to not-started")
        self._outcomes[action_id] = normalized.value
        self._append_evidence(audit_drafts)
        return current

    def outcome_for_action(self, action_id: str):
        return getattr(self, "_outcomes", {}).get(action_id)

    def bind_contract_evidence(self, action_id: str, *, expected_version: int, contract_version: str, capability_version: str, contract_digest: str, now) -> None:
        self._contract_evidence = getattr(self, "_contract_evidence", {})
        existing = self._contract_evidence.get(action_id)
        if existing is not None:
            if existing["contract_digest"] == contract_digest:
                return  # replay: same contract, already bound
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Contract digest already bound to a different value")
        self._contract_evidence[action_id] = {
            "contract_version": contract_version,
            "capability_version": capability_version,
            "contract_digest": contract_digest,
        }

    def get_contract_evidence(self, action_id: str):
        return getattr(self, "_contract_evidence", {}).get(action_id)

    def release_lease(self, action_id: str, *, lease_id: str, fencing_token: int, now) -> bool:
        self._leases = getattr(self, "_leases", {})
        lease = self._leases.get(action_id)
        if lease is None or lease.lease_id != lease_id or lease.fencing_token != fencing_token:
            return False
        from dataclasses import replace

        self._leases[action_id] = replace(lease, state="released", released_at=now)
        return True

    def active_lease(self, action_id: str):
        import datetime as _dt

        lease = getattr(self, "_leases", {}).get(action_id)
        if lease is None:
            return None
        now = _dt.datetime.now(_dt.timezone.utc)
        if lease.state != "granted" or lease.expires_at <= now:
            return None
        return lease

    def acquire_lease(self, action_id: str, expected_version: int, *, now, audit_drafts=()):
        import secrets as _secrets
        from datetime import timedelta as _timedelta

        from aipm.control_plane.audit import builders as audit_builders
        from aipm.control_plane.models import LifecycleState as _LifecycleState
        from aipm.control_plane.storage.sqlite_store import DEFAULT_LEASE_TTL, ExecutionLease

        current = self._actions.get(action_id)
        if current is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Unknown action")
        if current.state is not _LifecycleState.SNAPSHOT_CAPTURED:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Lease acquisition requires a snapshot-captured action")
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        if current.is_expired(now):
            raise ControlPlaneError(PlanningErrorCode.EXPIRED_PLAN, "Action has expired")
        self._leases = getattr(self, "_leases", {})
        if self.active_lease(action_id) is not None:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "An active lease already exists for this action")
        fencing_token = max((lease.fencing_token for lease in self._leases.values() if lease.action_id == action_id), default=0) + 1
        granted_at = now
        lease = ExecutionLease(
            lease_id=_secrets.token_hex(16),
            action_id=action_id,
            environment=current.scope.environment,
            fencing_token=fencing_token,
            state="granted",
            granted_at=granted_at,
            expires_at=granted_at + DEFAULT_LEASE_TTL,
            action_version=advanced.version,
        )
        advanced = self.advance_action(
            action_id,
            expected_version=expected_version,
            next_state=_LifecycleState.LEASED,
            approver_subject=current.requester_subject,
            now=now,
        )
        self._leases[action_id] = lease
        self._append_evidence(audit_drafts or (
            audit_builders.lease_acquired(
                actor_subject="control-plane-system",
                occurred_at=granted_at,
                action_id=action_id,
                plan_id=current.plan_id,
                target_id=current.scope.target_id,
                environment=current.scope.environment,
                lease_id=lease.lease_id,
                fencing_token=fencing_token,
            ),
        ))
        return lease, advanced

    def begin_execution(self, action_id: str, expected_version: int, *, confirmation_id: str, now, audit_drafts=()) -> ActionLifecycle:
        from aipm.control_plane.models import ConfirmationState, LifecycleState as _LifecycleState

        current = self._actions.get(action_id)
        if current is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Unknown action")
        if current.state is not _LifecycleState.LEASED:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Execution start requires a leased action")
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        binding = self._confirmations.get(confirmation_id)
        if binding is None or binding.state is not ConfirmationState.CONFIRMED:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Confirmation is not consumable")
        advanced = self.advance_action(
            action_id,
            expected_version=expected_version,
            next_state=_LifecycleState.RUNNING,
            approver_subject=current.requester_subject,
            now=now,
        )
        from dataclasses import replace

        self._confirmations[confirmation_id] = replace(binding, state=ConfirmationState.CONSUMED)
        self._append_evidence(audit_drafts)
        return advanced

    def execute_plan_mutation(self, action_id: str, expected_version: int, *, expected_revision: int, mutation_fields, now, audit_drafts=()):
        from aipm.control_plane.models import LifecycleState as _LifecycleState

        current = self._actions.get(action_id)
        if current is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Unknown action")
        if current.state is not _LifecycleState.RUNNING:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Plan mutation requires a running action")
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        try:
            self._plans.update(current.scope.target_id, expected_revision=expected_revision, fields=mutation_fields, now=now)
        except Exception as exc:
            raise ControlPlaneError(PlanningErrorCode.STALE_EVIDENCE, "Current plan no longer matches the authorized precondition") from exc
        advanced = self.advance_action(
            action_id,
            expected_version=expected_version,
            next_state=_LifecycleState.EXECUTED_PENDING_VERIFICATION,
            approver_subject=current.requester_subject,
            now=now,
        )
        self._outcomes = getattr(self, "_outcomes", {})
        self._outcomes[action_id] = "mutation_succeeded"
        self._append_evidence(audit_drafts)
        return advanced, None

    def record_verification_outcome(self, action_id: str, expected_version: int, *, result, now, audit_drafts=()) -> ActionLifecycle:
        from aipm.control_plane.models import LifecycleState as _LifecycleState

        current = self._actions.get(action_id)
        if current is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Unknown action")
        if current.state is not _LifecycleState.EXECUTED_PENDING_VERIFICATION:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Verification requires an executed action")
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        target = _LifecycleState.VERIFIED_SUCCESS if result.success else _LifecycleState.VERIFICATION_FAILED
        advanced = self.advance_action(
            action_id,
            expected_version=expected_version,
            next_state=target,
            approver_subject=current.requester_subject,
            now=now,
        )
        self._verifications[result.verification_id] = result
        self._outcomes = getattr(self, "_outcomes", {})
        self._outcomes[action_id] = "verification_succeeded" if result.success else "verification_failed"
        self._append_evidence(audit_drafts)
        return advanced

    def mark_reconciled(self, action_id: str, expected_version: int, *, to_state, outcome: str, now, audit_drafts=()) -> ActionLifecycle:
        current = self._actions.get(action_id)
        if current is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Unknown action")
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        advanced = self.advance_action(
            action_id,
            expected_version=expected_version,
            next_state=to_state,
            approver_subject=current.requester_subject,
            now=now,
        )
        self._outcomes = getattr(self, "_outcomes", {})
        self._outcomes[action_id] = outcome.value if hasattr(outcome, "value") else str(outcome)
        self._append_evidence(audit_drafts)
        return advanced

    def advance_rollback_state(self, action_id: str, expected_version: int, *, from_states, to_state, now, audit_drafts=()) -> ActionLifecycle:
        current = self._actions.get(action_id)
        if current is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Unknown action")
        if current.state not in from_states:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Action is not in a rollback-eligible state")
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        return self.advance_action(
            action_id,
            expected_version=expected_version,
            next_state=to_state,
            approver_subject=current.requester_subject,
            now=now,
        )



    def save(self, result, *, audit_drafts=()):
        """Verification-record store face for the in-memory double."""

        self._verifications[result.verification_id] = result
        self._append_evidence(audit_drafts)
        return result

    def records_for_action(self, action_id: str) -> tuple:
        return tuple(record for record in self._verifications.values() if record.action_id == action_id)

    # -- confirmations ------------------------------------------------------

    def put_confirmation(self, binding) -> None:
        from aipm.control_plane.models import ConfirmationBinding

        if not isinstance(binding, ConfirmationBinding):
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid confirmation binding")
        self._confirmations[binding.confirmation_id] = binding

    def put(self, binding) -> None:
        self.put_confirmation(binding)

    def get_confirmation(self, confirmation_id: str):
        return self._confirmations.get(confirmation_id)

    def get(self, confirmation_id: str):
        return self._confirmations.get(confirmation_id)

    def has_active_for_action(self, action_id: str) -> bool:
        return any(
            binding.action_id == action_id and binding.state in _ACTIVE_CONFIRMATION_STATES
            for binding in self._confirmations.values()
        )

    def get_active_for_action(self, action_id: str):
        from aipm.control_plane.models import ConfirmationState

        for binding in self._confirmations.values():
            if binding.action_id == action_id and binding.state is ConfirmationState.CONFIRMATION_REQUESTED:
                return binding
        return None

    def count(self) -> int:
        return len(self._confirmations)

    def as_mapping(self) -> Mapping:
        return dict(self._confirmations)

    def record_confirmation_with_advance(self, binding, transition: LifecycleTransition, *, audit_drafts=()) -> ActionLifecycle:
        from dataclasses import replace

        if not isinstance(binding, ConfirmationBinding):
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid confirmation binding")
        if binding.action_id != transition.action_id:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Confirmation does not match the transition target")
        # In-memory double: sequential; the durable implementation commits both
        # writes in one SQLite transaction.
        confirmed = replace(binding, state=ConfirmationState.CONFIRMED)
        self._confirmations[confirmed.confirmation_id] = confirmed
        advanced = self.advance_action(
            transition.action_id,
            expected_version=transition.expected_version,
            next_state=transition.next_state,
            approver_subject=transition.approver_subject,
            now=transition.now,
        )
        self._append_evidence(audit_drafts)
        return advanced
