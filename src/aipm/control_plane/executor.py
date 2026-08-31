"""The bounded reference executor for the control plane.

This executor is deliberately tiny. It is NOT a second control plane: it does
not authenticate, does not authorize, does not interpret commands, and does
not accept callbacks or generic providers. It executes exactly one closed
operation — ``UPDATE_PROJECT_PLAN`` against the control-plane ProjectPlan
store — and only when handed a fully bound, fully validated
:class:`ExecutionContract` whose authorization artifacts already exist.

The control plane is the authorization authority; this module is the
execution authority. Every mutation boundary re-validates the world (action
version, lease, fencing token, kill switch, expiry, current plan state)
because the world may have changed since authorization.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from aipm.control_plane.audit.sanitize import AuditEventError, bounded_reference
from aipm.control_plane.lifecycle import advance as advance_lifecycle
from aipm.control_plane.models import (
    ActionLifecycle,
    LifecycleState,
    OperationKind,
)
from aipm.control_plane.project_plan import allowed_fields
from aipm.control_plane.verification import (
    ExpectedState,
    ExecutionOutcome,
    ObservedState,
    observed_from_plan,
    retry_permitted,
    verify,
)

EXECUTION_CONTRACT_VERSION = "mc612-execution-contract-v2"
CONTRACT_DIGEST_VERSION = "mc612-contract-digest-v1"
SYSTEM_ACTOR = "control-plane-system"

_HEX64 = "^[0-9a-f]{64}$"


class ExecutorCapability(str, Enum):
    """Closed executor capability set; no free-form operations exist."""

    UPDATE_PROJECT_PLAN = "update_project_plan"


class ExecutionRefused(Exception):
    """Raised when a contract fails integrity/precondition validation.

    No mutation has occurred and no lifecycle state changed; the refusal is
    the safety response, not an execution outcome.
    """

    def __init__(self, reason_code: str, message: str = "Execution refused") -> None:
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {message}"[:256])


@dataclass(frozen=True, slots=True)
class ExecutionContract:
    """The only input the executor accepts.

    Carries the already-issued authorization bindings (decision,
    confirmation, snapshot, lease, fencing token, kill-switch epoch) plus the
    exact bounded mutation. It contains no commands, no callables, no
    providers, no credentials, no session material — the typed fields make
    those unrepresentable.
    """

    contract_version: str
    action_id: str
    action_version: int
    operation: ExecutorCapability
    target_id: str
    environment: str
    plan_id: str
    expected_plan_revision: int
    expected_plan_digest: str
    mutation_fields: tuple[tuple[str, str], ...]
    snapshot_id: str
    decision_id: str
    confirmation_id: str
    policy_version: str
    verification_version: str
    kill_switch_epoch: int
    lease_id: str
    fencing_token: int
    expires_at: datetime
    capability_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability_version", bounded_reference(self.capability_version, field="capability version", maximum=64))
        if self.contract_version != EXECUTION_CONTRACT_VERSION:
            raise AuditEventError("Unsupported execution contract version")
        object.__setattr__(self, "action_id", bounded_reference(self.action_id, field="action id"))
        if not isinstance(self.action_version, int) or self.action_version < 1:
            raise AuditEventError("Invalid action version")
        capability = self.operation if isinstance(self.operation, ExecutorCapability) else ExecutorCapability(self.operation)
        if capability is not ExecutorCapability.UPDATE_PROJECT_PLAN:
            raise AuditEventError("Executor capability is closed to plan updates")
        object.__setattr__(self, "operation", capability)
        object.__setattr__(self, "target_id", bounded_reference(self.target_id, field="target id"))
        object.__setattr__(self, "environment", bounded_reference(self.environment, field="environment", maximum=32))
        object.__setattr__(self, "plan_id", bounded_reference(self.plan_id, field="plan id"))
        if not isinstance(self.expected_plan_revision, int) or self.expected_plan_revision < 1:
            raise AuditEventError("Invalid expected plan revision")
        import re

        if re.fullmatch(r"[0-9a-f]{64}", self.expected_plan_digest or "") is None:
            raise AuditEventError("Invalid expected plan digest")
        allowed = allowed_fields()
        fields = tuple(sorted((str(key), str(value)) for key, value in self.mutation_fields))
        if not fields or any(name not in allowed for name, _value in fields):
            raise AuditEventError("Mutation fields outside the allow-list")
        object.__setattr__(self, "mutation_fields", fields)
        object.__setattr__(self, "snapshot_id", bounded_reference(self.snapshot_id, field="snapshot id"))
        object.__setattr__(self, "decision_id", bounded_reference(self.decision_id, field="decision id"))
        object.__setattr__(self, "confirmation_id", bounded_reference(self.confirmation_id, field="confirmation id"))
        object.__setattr__(self, "policy_version", bounded_reference(self.policy_version, field="policy version", maximum=64))
        if not isinstance(self.kill_switch_epoch, int) or self.kill_switch_epoch < 1:
            raise AuditEventError("Invalid kill-switch epoch")
        object.__setattr__(self, "lease_id", bounded_reference(self.lease_id, field="lease id"))
        if not isinstance(self.fencing_token, int) or self.fencing_token < 1:
            raise AuditEventError("Invalid fencing token")
        if not isinstance(self.expires_at, datetime):
            raise AuditEventError("Invalid contract expiry")
        object.__setattr__(self, "expires_at", self.expires_at if self.expires_at.tzinfo is not None else self.expires_at.replace(tzinfo=timezone.utc))

    def is_expired(self, now: datetime) -> bool:
        current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        return current >= self.expires_at

    def canonical_payload(self) -> dict[str, Any]:
        """Deterministic serialization; the integrity digest binds ALL fields."""

        import hashlib
        import json as _json

        payload = {
            "action_id": self.action_id,
            "action_version": self.action_version,
            "capability": self.operation.value,
            "capability_version": self.capability_version,
            "confirmation_id": self.confirmation_id,
            "decision_id": self.decision_id,
            "environment": self.environment,
            "expected_plan_digest": self.expected_plan_digest,
            "expected_plan_revision": self.expected_plan_revision,
            "expires_at": self.expires_at.isoformat(),
            "fencing_token": self.fencing_token,
            "kill_switch_epoch": self.kill_switch_epoch,
            "lease_id": self.lease_id,
            "mutation_fields": [[key, value] for key, value in self.mutation_fields],
            "plan_id": self.plan_id,
            "policy_version": self.policy_version,
            "snapshot_id": self.snapshot_id,
            "target_id": self.target_id,
            "verification_version": self.verification_version,
            "version": CONTRACT_DIGEST_VERSION,
        }
        canonical = _json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return {
            "canonical": canonical,
            "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }

    def digest(self) -> str:
        return self.canonical_payload()["digest"]


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Bounded executor result; never a substitute for verification."""

    action_id: str
    outcome: ExecutionOutcome
    lifecycle_state: LifecycleState
    mutated_revision: int | None = None
    verification_success: bool | None = None
    verification_id: str | None = None
    refusal_reason: str | None = None


def _utc(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class Executor:
    """Executes one closed operation against the control-plane plan store.

    Dependencies are narrow control-plane components (the CAS plan store, the
    action repository, the confirmation service, the kill switch, and the
    audit ledger) — never generic providers or callables.
    """

    __slots__ = ("_plans", "_actions", "_confirmations", "_kill_switches", "_audit", "_snapshots", "_gate", "_initialized")

    def __init__(self, *, plans, actions, confirmations, kill_switches=None, audit, snapshots=None) -> None:
        if plans is None or not hasattr(plans, "read") or not hasattr(plans, "update"):
            raise TypeError("executor requires the CAS plan store")
        if actions is None or not hasattr(actions, "get_action"):
            raise TypeError("executor requires the action repository")
        if confirmations is None or not hasattr(confirmations, "consume"):
            raise TypeError("executor requires the confirmation service")
        if audit is None or not hasattr(audit, "append_in_transaction"):
            raise TypeError("executor requires the audit ledger")
        object.__setattr__(self, "_plans", plans)
        object.__setattr__(self, "_actions", actions)
        object.__setattr__(self, "_confirmations", confirmations)
        object.__setattr__(self, "_kill_switches", kill_switches)
        object.__setattr__(self, "_audit", audit)
        object.__setattr__(self, "_snapshots", snapshots)
        object.__setattr__(self, "_gate", None)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("Executor configuration is immutable")
        object.__setattr__(self, name, value)

    # ------------------------------------------------------------------
    # Preconditions
    # ------------------------------------------------------------------

    def _validate_world(self, contract: ExecutionContract, *, now: datetime) -> ActionLifecycle:
        action = self._actions.get_action(contract.action_id)
        if action is None:
            raise ExecutionRefused("action_missing", "Contract action does not exist")
        if action.version != contract.action_version:
            raise ExecutionRefused("stale_action_version", "Action version changed since the contract was issued")
        if action.operation.value != contract.operation.value:
            raise ExecutionRefused("operation_mismatch", "Contract operation does not match the action")
        if action.scope.target_id != contract.target_id or action.scope.environment != contract.environment:
            raise ExecutionRefused("binding_mismatch", "Contract target/environment does not match the action")
        if action.plan_id != contract.plan_id or action.plan_revision != contract.expected_plan_revision:
            raise ExecutionRefused("binding_mismatch", "Contract plan binding does not match the action")
        if contract.is_expired(now):
            raise ExecutionRefused("contract_expired", "Execution contract has expired")
        if action.is_expired(now):
            raise ExecutionRefused("action_expired", "Action has expired")
        if self._kill_switches is not None:
            switch = self._kill_switches.switch(contract.environment)
            if switch.epoch != contract.kill_switch_epoch:
                raise ExecutionRefused("kill_switch_epoch_mismatch", "Kill switch changed since the lease was granted")
            if not switch.permits_operations():
                raise ExecutionRefused("kill_switch_engaged", "Kill switch denies execution")
        return action

    def _bind_and_check_gate(self, contract: ExecutionContract, action: ActionLifecycle, *, now: datetime) -> None:
        """Bind the contract digest durably and run the final execution gate.

        Binding happens only when the action is LEASED (first execution
        attempt); on replay (RUNNING or later) the existing binding is
        already durable and must not be overwritten.
        """
        from aipm.control_plane.gate import FinalExecutionGate

        if action.state is not LifecycleState.LEASED:
            return  # replay: digest already durably bound from the first attempt
        self._actions.bind_contract_evidence(
            contract.action_id,
            expected_version=action.version,
            contract_version=contract.contract_version,
            capability_version=contract.capability_version,
            contract_digest=contract.digest(),
            now=now,
        )
        gate = FinalExecutionGate(
            actions=self._actions, plans=self._plans, confirmations=self._confirmations,
            snapshots=self._snapshots, kill_switches=self._kill_switches,
        )
        decision = gate.evaluate(contract, now=now)
        if not decision.allowed:
            raise ExecutionRefused(decision.reason.value, f"Final execution gate denied: {decision.reason.value}")

    def _validate_confirmation(self, contract: ExecutionContract, *, allow_consumed: bool, now: datetime) -> None:
        binding = self._confirmations.store.get(contract.confirmation_id)
        if binding is None:
            raise ExecutionRefused("confirmation_missing", "Contract confirmation does not exist")
        if binding.action_id != contract.action_id:
            raise ExecutionRefused("confirmation_mismatch", "Confirmation is bound to a different action")
        if binding.is_expired(_utc(now)):
            raise ExecutionRefused("confirmation_expired", "Confirmation has expired")
        if binding.state.value == "consumed" and not allow_consumed:
            raise ExecutionRefused("confirmation_consumed", "Confirmation was already consumed")

    def _validate_snapshot(self, contract: ExecutionContract) -> None:
        if self._snapshots is None:
            raise ExecutionRefused("snapshot_unavailable", "Snapshot repository is not configured")
        snapshot = self._snapshots.snapshot_for_action(contract.action_id)
        if snapshot is None:
            raise ExecutionRefused("snapshot_missing", "Contract snapshot does not exist")
        if snapshot.action_id != contract.action_id or snapshot.snapshot_id != contract.snapshot_id:
            raise ExecutionRefused("snapshot_mismatch", "Snapshot is bound to a different action")
        if snapshot.revision != contract.expected_plan_revision or snapshot.target_id != contract.target_id:
            raise ExecutionRefused("snapshot_stale", "Snapshot does not match the contract plan binding")
        if snapshot.environment != contract.environment:
            raise ExecutionRefused("snapshot_mismatch", "Snapshot environment does not match the contract")

    def _validate_lease(self, contract: ExecutionContract, *, now: datetime) -> None:
        lease = self._actions.active_lease(contract.action_id, now=now) if hasattr(self._actions, "active_lease") else None
        if lease is None:
            raise ExecutionRefused("lease_missing", "No active lease for the action")
        if lease.lease_id != contract.lease_id:
            raise ExecutionRefused("lease_mismatch", "Contract lease does not match the active lease")
        if lease.fencing_token != contract.fencing_token:
            raise ExecutionRefused("stale_fencing_token", "Fencing token is no longer current")
        if lease.action_version is not None and lease.action_version != contract.action_version:
            raise ExecutionRefused("stale_lease", "Lease is bound to a different action version")
        if lease.expires_at <= _utc(now):
            raise ExecutionRefused("lease_expired", "Lease has expired")

    def _validate_current_plan(self, contract: ExecutionContract) -> None:
        current = self._plans.read(contract.target_id)
        if current.revision != contract.expected_plan_revision or current.digest() != contract.expected_plan_digest:
            raise ExecutionRefused("stale_plan", "Current plan no longer matches the authorized precondition")
        return current

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, contract: ExecutionContract, *, now: datetime | None = None) -> ExecutionResult:
        """Execute one bounded plan mutation under a validated contract."""

        moment = _utc(now)
        action = self._validate_world(contract, now=moment)
        self._bind_and_check_gate(contract, action, now=moment)
        if action.state is LifecycleState.VERIFIED_SUCCESS:
            return ExecutionResult(action_id=contract.action_id, outcome=ExecutionOutcome.VERIFICATION_SUCCEEDED, lifecycle_state=action.state)
        if action.state not in {LifecycleState.SNAPSHOT_CAPTURED, LifecycleState.LEASED, LifecycleState.RUNNING, LifecycleState.EXECUTED_PENDING_VERIFICATION}:
            raise ExecutionRefused("invalid_lifecycle_state", f"Action state {action.state.value} is not executable")
        self._validate_confirmation(contract, allow_consumed=action.state is not LifecycleState.LEASED, now=moment)
        self._validate_snapshot(contract)
        self._validate_lease(contract, now=moment)
        self._validate_current_plan(contract)

        current_version = action.version
        if action.state is LifecycleState.LEASED:
            started = audit_builders_execution_started(contract, moment)
            advanced = self._actions.begin_execution(
                contract.action_id,
                expected_version=current_version,
                confirmation_id=contract.confirmation_id,
                now=moment,
                audit_drafts=(started,),
            )
            current_version = advanced.version
        action = self._actions.get_action(contract.action_id)

        if action.state is LifecycleState.RUNNING:
            succeeded, failure_code = self._actions.execute_plan_mutation(
                contract.action_id,
                expected_version=current_version,
                expected_revision=contract.expected_plan_revision,
                mutation_fields=dict(contract.mutation_fields),
                now=moment,
                audit_drafts=(audit_builders_execution_succeeded(contract, moment),),
            )
            action = succeeded
            current_version = succeeded.version
            mutated_revision = contract.expected_plan_revision + 1
        elif action.state is LifecycleState.EXECUTED_PENDING_VERIFICATION:
            current_version = action.version
            mutated_revision = contract.expected_plan_revision + 1  # resume: mutation already durable
        else:  # pragma: no cover - guarded above
            raise ExecutionRefused("invalid_lifecycle_state", "Action left the executable path")

        # Independent verification: fresh read-back, never the mutation response.
        verification_started = audit_builders_verification_started(contract, _utc(now))
        self._audit.append(verification_started)
        current_plan = self._plans.read(contract.target_id)
        expected = ExpectedState(
            target_id=contract.target_id,
            environment=contract.environment,
            revision=mutated_revision,
            canonical_digest=None,
            enabled=current_plan.enabled,
            fields=contract.mutation_fields,
        )
        result = verify(expected, observed_from_plan(current_plan), action_id=contract.action_id)
        finished = audit_builders_verification_finished(contract, result, _utc(now))
        final_action = self._actions.record_verification_outcome(
            contract.action_id,
            expected_version=current_version,
            result=result,
            now=_utc(now),
            audit_drafts=(finished,),
        )
        # Durable lease release: terminal outcome frees the lease.
        self._actions.release_lease(contract.action_id, lease_id=contract.lease_id, fencing_token=contract.fencing_token, now=_utc(now))
        return ExecutionResult(
            action_id=contract.action_id,
            outcome=ExecutionOutcome.VERIFICATION_SUCCEEDED if result.success else ExecutionOutcome.VERIFICATION_FAILED,
            lifecycle_state=final_action.state,
            mutated_revision=mutated_revision,
            verification_success=result.success,
            verification_id=result.verification_id,
        )

    # ------------------------------------------------------------------
    # Reconciliation (UNKNOWN_OUTCOME safety)
    # ------------------------------------------------------------------

    def reconcile(self, contract: ExecutionContract, *, now: datetime | None = None) -> ExecutionResult:
        """Classify an action with an UNKNOWN_OUTCOME by independent observation.

        Blind retry is never performed here: the observation either
        establishes the post-state (mutation succeeded), the pre-state (the
        mutation did not happen; retry becomes a policy decision with the
        outcome back at not-started), or neither (the outcome stays unknown).
        """

        moment = _utc(now)
        action = self._actions.get_action(contract.action_id)
        if action is None:
            raise ExecutionRefused("action_missing", "Contract action does not exist")
        outcome = self._actions.outcome_for_action(contract.action_id)
        if outcome != ExecutionOutcome.UNKNOWN_OUTCOME.value:
            return ExecutionResult(action_id=contract.action_id, outcome=ExecutionOutcome(outcome), lifecycle_state=action.state)
        current = self._plans.read(contract.target_id)
        post_fields = dict(contract.mutation_fields)
        current_fields = {"title": current.title, "objective": current.objective}
        post_state = current.revision == contract.expected_plan_revision + 1 and all(
            current_fields.get(name) == value for name, value in post_fields.items()
        )
        pre_state = current.revision == contract.expected_plan_revision and current.digest() == contract.expected_plan_digest
        if post_state:
            final = self._actions.mark_reconciled(
                contract.action_id,
                expected_version=action.version,
                to_state=LifecycleState.EXECUTED_PENDING_VERIFICATION,
                outcome=ExecutionOutcome.MUTATION_SUCCEEDED,
                now=moment,
            )
            return ExecutionResult(action_id=contract.action_id, outcome=ExecutionOutcome.MUTATION_SUCCEEDED, lifecycle_state=final.state, mutated_revision=current.revision)
        if pre_state:
            self._actions.mark_reconciled(
                contract.action_id,
                expected_version=action.version,
                to_state=LifecycleState.RECONCILIATION_REQUIRED,
                outcome=ExecutionOutcome.MUTATION_NOT_STARTED,
                now=moment,
            )
            return ExecutionResult(action_id=contract.action_id, outcome=ExecutionOutcome.MUTATION_NOT_STARTED, lifecycle_state=LifecycleState.RECONCILIATION_REQUIRED)
        return ExecutionResult(action_id=contract.action_id, outcome=ExecutionOutcome.UNKNOWN_OUTCOME, lifecycle_state=action.state)

    # ------------------------------------------------------------------
    # Rollback execution
    # ------------------------------------------------------------------

    def execute_rollback(self, *, rollback_action_id: str, original_action_id: str, now: datetime | None = None) -> ExecutionResult:
        """Execute the bounded inverse mutation for a failed original action.

        Everything is loaded from durable state by identity: the rollback
        action, the original action, the immutable pre-mutation snapshot, and
        the rollback action's own lease/confirmation/kill-switch bindings.
        The original action is never mutated here. The FinalExecutionGate
        validates the rollback action's own contract before any mutation.
        """

        moment = _utc(now)
        rollback_action = self._actions.get_action(rollback_action_id)
        if rollback_action is None:
            raise ExecutionRefused("action_missing", "Rollback action does not exist")
        original = self._actions.get_action(original_action_id)
        if original is None:
            raise ExecutionRefused("action_missing", "Original action does not exist")
        if rollback_action.rollback_of_action_id != original_action_id:
            raise ExecutionRefused("binding_mismatch", "Rollback action does not reference the original action")
        if original.state not in {LifecycleState.VERIFICATION_FAILED, LifecycleState.EXECUTION_FAILED, LifecycleState.ROLLBACK_REQUESTED}:
            raise ExecutionRefused("invalid_original_state", "Original action is not in a rollback-eligible state")
        if rollback_action.operation.value != "rollback_project_plan":
            raise ExecutionRefused("operation_mismatch", "Rollback action has the wrong operation")
        if rollback_action.is_expired(moment) or original.is_expired(moment):
            raise ExecutionRefused("action_expired", "Rollback or original action has expired")
        if self._kill_switches is not None and not self._kill_switches.permits(rollback_action.scope.environment):
            raise ExecutionRefused("kill_switch_engaged", "Kill switch denies rollback execution")

        if rollback_action.state is LifecycleState.SNAPSHOT_CAPTURED:
            self._actions.acquire_lease(
                rollback_action_id,
                expected_version=rollback_action.version,
                now=moment,
            )
            rollback_action = self._actions.get_action(rollback_action_id)
        contract = self._contract_for_action(rollback_action, moment)
        self._validate_lease(contract, now=moment)

        # Final execution gate: rollback must independently satisfy the same
        # authoritative boundary as the original mutation. The gate validates
        # the rollback action's own contract (digest, confirmation, snapshot,
        # capability, kill switch, lease, plan state, expiry).
        from aipm.control_plane.gate import FinalExecutionGate

        gate = FinalExecutionGate(
            actions=self._actions, plans=self._plans, confirmations=self._confirmations,
            snapshots=self._snapshots, kill_switches=self._kill_switches,
        )
        gate_decision = gate.evaluate(contract, now=moment)
        if not gate_decision.allowed:
            raise ExecutionRefused(gate_decision.reason.value, f"Rollback gate denied: {gate_decision.reason.value}")

        # Bind rollback contract digest durably (immutable, independent from the original)
        self._actions.bind_contract_evidence(
            rollback_action_id,
            expected_version=rollback_action.version,
            contract_version=contract.contract_version,
            capability_version=contract.capability_version,
            contract_digest=contract.digest(),
            now=moment,
        )
        current_version = rollback_action.version
        if rollback_action.state is LifecycleState.LEASED:
            advanced = self._actions.begin_execution(
                rollback_action_id,
                expected_version=current_version,
                confirmation_id=contract.confirmation_id,
                now=moment,
                audit_drafts=(audit_builders_execution_started(contract, moment),),
            )
            current_version = advanced.version
            rollback_action = self._actions.get_action(rollback_action_id)

        # The bounded inverse mutation: restore the snapshot's mutable fields.
        snapshot = self._load_snapshot_for_original(original_action_id)
        restore_fields = self._snapshot_restore_fields(snapshot)
        current = self._plans.read(rollback_action.scope.target_id)
        if current.revision != original.plan_revision + 1:
            raise ExecutionRefused("stale_plan", "Current state is not the failed mutation's post-condition")
        if rollback_action.state is LifecycleState.RUNNING:
            succeeded, _failure = self._actions.execute_plan_mutation(
                rollback_action_id,
                expected_version=current_version,
                expected_revision=current.revision,
                mutation_fields=restore_fields,
                now=moment,
                audit_drafts=(audit_builders_execution_succeeded(contract, moment),),
            )
            current_version = succeeded.version
            rollback_action = succeeded

        # Independent rollback verification against the snapshot state.
        verification_started = audit_builders_verification_started(contract, _utc(now))
        self._audit.append(verification_started)
        current_plan = self._plans.read(rollback_action.scope.target_id)
        snapshot_fields = self._snapshot_field_values(snapshot)
        expected = ExpectedState(
            target_id=rollback_action.scope.target_id,
            environment=rollback_action.scope.environment,
            revision=current_plan.revision,
            canonical_digest=None,
            enabled=current_plan.enabled,
            fields=snapshot_fields,
        )
        result = verify(expected, observed_from_plan(current_plan), action_id=rollback_action_id)
        finished = audit_builders_verification_finished(contract, result, _utc(now))
        final = self._actions.record_verification_outcome(
            rollback_action_id,
            expected_version=current_version,
            result=result,
            now=_utc(now),
            audit_drafts=(finished,),
        )
        if result.success:
            restored = self._actions.advance_rollback_state(
                original_action_id,
                expected_version=original.version,
                from_states={LifecycleState.ROLLBACK_REQUESTED},
                to_state=LifecycleState.ROLLED_BACK,
                now=_utc(now),
                audit_drafts=(audit_builders_rollback_finished(original, success=True, moment=_utc(now), contract_digest=contract.digest()),),
            )
            # Durable lease release: terminal outcome frees the lease.
            self._actions.release_lease(rollback_action_id, lease_id=contract.lease_id, fencing_token=contract.fencing_token, now=_utc(now))
            return ExecutionResult(action_id=rollback_action_id, outcome=ExecutionOutcome.ROLLBACK_SUCCEEDED, lifecycle_state=restored.state, verification_success=True, verification_id=result.verification_id)
        self._actions.advance_rollback_state(
            original_action_id,
            expected_version=original.version,
            from_states={LifecycleState.ROLLBACK_REQUESTED},
            to_state=LifecycleState.ROLLBACK_FAILED,
            now=_utc(now),
            audit_drafts=(audit_builders_rollback_finished(original, success=False, moment=_utc(now), contract_digest=contract.digest()),),
        )
        self._actions.release_lease(rollback_action_id, lease_id=contract.lease_id, fencing_token=contract.fencing_token, now=_utc(now))
        return ExecutionResult(action_id=rollback_action_id, outcome=ExecutionOutcome.ROLLBACK_FAILED, lifecycle_state=final.state, verification_success=False, verification_id=result.verification_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _contract_for_action(self, action: ActionLifecycle, moment: datetime) -> ExecutionContract:
        decision = self._actions.get_decision(action.decision_id) if action.decision_id else None
        if decision is None or decision.request is None:
            raise ExecutionRefused("binding_missing", "Action has no durable decision binding")
        lease = self._actions.active_lease(action.action_id, now=moment) if hasattr(self._actions, "active_lease") else None
        if lease is None:
            raise ExecutionRefused("lease_missing", "Action has no active lease")
        confirmation_id = None
        for binding in self._confirmations.store.values():
            if binding.action_id == action.action_id and binding.state.value in {"confirmed", "consumed"}:
                confirmation_id = binding.confirmation_id
                break
        if confirmation_id is None:
            raise ExecutionRefused("confirmation_missing", "Action has no confirmation binding")
        snapshot = self._load_snapshot_for_original(action.rollback_of_action_id) if action.rollback_of_action_id else None
        if action.rollback_of_action_id and snapshot is not None:
            # The rollback contract's bounded mutation is the snapshot restore.
            mutation_fields = tuple(sorted(self._snapshot_restore_fields(snapshot).items()))
        else:
            mutation_fields = tuple(decision.request.metadata)
        kill_switch_epoch = self._kill_switches.switch(action.scope.environment).epoch if self._kill_switches is not None else 1
        return ExecutionContract(
            contract_version=EXECUTION_CONTRACT_VERSION,
            action_id=action.action_id,
            action_version=action.version,
            operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
            target_id=action.scope.target_id,
            environment=action.scope.environment,
            plan_id=action.plan_id,
            expected_plan_revision=action.plan_revision,
            expected_plan_digest=decision.action_identity.target_digest if decision.action_identity else "0" * 64,
            mutation_fields=mutation_fields,
            snapshot_id=snapshot.snapshot_id if snapshot else (action.snapshot_id or "unknown"),
            decision_id=decision.decision_id,
            confirmation_id=confirmation_id,
            policy_version=action.scope.policy_version,
            verification_version=_verification_version(),
            kill_switch_epoch=kill_switch_epoch,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            expires_at=action.expires_at,
            capability_version="1",
        )

    def _load_snapshot_for_original(self, original_action_id: str):
        if self._snapshots is None:
            raise ExecutionRefused("snapshot_unavailable", "Snapshot repository is not configured")
        snapshot = self._snapshots.snapshot_for_action(original_action_id)
        if snapshot is None:
            raise ExecutionRefused("snapshot_missing", "Original action has no pre-mutation snapshot")
        return snapshot

    def _snapshot_restore_fields(self, snapshot) -> dict[str, str]:
        import json

        payload = json.loads(snapshot.payload_canonical)
        return {"title": payload["title"], "objective": payload["objective"]}

    def _snapshot_field_values(self, snapshot) -> tuple[tuple[str, str], ...]:
        import json

        payload = json.loads(snapshot.payload_canonical)
        return (("objective", payload["objective"]), ("title", payload["title"]))


def _verification_version() -> str:
    from aipm.control_plane.verification import VERIFICATION_VERSION

    return VERIFICATION_VERSION


def audit_builders_execution_started(contract: ExecutionContract, moment: datetime):
    from aipm.control_plane.audit import builders as audit_builders

    event = audit_builders.execution_started(
        actor_subject=SYSTEM_ACTOR,
        occurred_at=moment,
        action_id=contract.action_id,
        plan_id=contract.plan_id,
        target_id=contract.target_id,
        environment=contract.environment,
        lease_id=contract.lease_id,
        fencing_token=contract.fencing_token,
    )
    from dataclasses import replace

    return replace(event, result_code=event.result_code + ":cd=" + contract.digest()[:16])


def audit_builders_execution_succeeded(contract: ExecutionContract, moment: datetime):
    from aipm.control_plane.audit import builders as audit_builders

    return audit_builders.execution_finished(
        actor_subject=SYSTEM_ACTOR,
        occurred_at=moment,
        action_id=contract.action_id,
        plan_id=contract.plan_id,
        target_id=contract.target_id,
        environment=contract.environment,
        success=True,
    )


def audit_builders_verification_started(contract: ExecutionContract, moment: datetime):
    from aipm.control_plane.audit import builders as audit_builders

    return audit_builders.verification_started(
        actor_subject=SYSTEM_ACTOR,
        occurred_at=moment,
        action_id=contract.action_id,
        plan_id=contract.plan_id,
        plan_revision=contract.expected_plan_revision,
        plan_digest=None,
        target_id=contract.target_id,
        environment=contract.environment,
    )


def audit_builders_verification_finished(contract: ExecutionContract, result, moment: datetime):
    from aipm.control_plane.audit import builders as audit_builders

    return audit_builders.verification_finished(
        actor_subject=SYSTEM_ACTOR,
        occurred_at=moment,
        action_id=contract.action_id,
        plan_id=contract.plan_id,
        plan_revision=contract.expected_plan_revision,
        plan_digest=None,
        target_id=contract.target_id,
        environment=contract.environment,
        verification_id=result.verification_id,
        success=result.success,
        reason_code=result.reason_code.value,
        verification_version=result.verification_version,
    )


def audit_builders_rollback_finished(original: ActionLifecycle, *, success: bool, moment: datetime, contract_digest: str | None = None):
    from aipm.control_plane.audit import builders as audit_builders

    return audit_builders.rollback_finished(
        actor_subject=SYSTEM_ACTOR,
        occurred_at=moment,
        action_id=original.action_id,
        plan_id=original.plan_id,
        target_id=original.scope.target_id,
        environment=original.scope.environment,
        success=success,
        contract_digest=contract_digest,
    )

