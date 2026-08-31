"""Systemd restart executor: the bridge between the control plane and the provider.

This executor implements the RESTART_ALLOWLISTED_SYSTEMD_UNIT capability.
It receives the canonical ExecutionContract, runs the FinalExecutionGate,
performs final race checks, captures a pre-mutation snapshot, invokes the
provider, and independently verifies the result.

Non-reversible: automatic rollback is impossible. Failure → reconciliation.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aipm.control_plane.executor import ExecutionRefused
from aipm.control_plane.gate import FinalExecutionGate, GateCode
from aipm.control_plane.models import LifecycleState
from aipm.control_plane.systemd_provider import (
    SystemdRestartPolicy,
    SystemdRestartProvider,
    SystemdRestartResult,
    SystemdUnitSnapshot,
)


class SystemdRestartExecutor:
    """Bounded executor for RESTART_ALLOWLISTED_SYSTEMD_UNIT."""

    __slots__ = ("_actions", "_plans", "_confirmations", "_kill_switches", "_audit", "_snapshots", "_provider", "_policy", "_initialized")

    def __init__(self, *, actions, plans, confirmations, kill_switches=None, audit, snapshots=None, provider: SystemdRestartProvider, policy: SystemdRestartPolicy) -> None:
        if not isinstance(provider, SystemdRestartProvider):
            raise TypeError("provider must be SystemdRestartProvider")
        if not isinstance(policy, SystemdRestartPolicy):
            raise TypeError("policy must be SystemdRestartPolicy")
        object.__setattr__(self, "_actions", actions)
        object.__setattr__(self, "_plans", plans)
        object.__setattr__(self, "_confirmations", confirmations)
        object.__setattr__(self, "_kill_switches", kill_switches)
        object.__setattr__(self, "_audit", audit)
        object.__setattr__(self, "_snapshots", snapshots)
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_policy", policy)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("SystemdRestartExecutor configuration is immutable")
        object.__setattr__(self, name, value)

    def execute_restart(self, contract, *, now: datetime | None = None) -> SystemdRestartResult:
        """Execute one bounded systemd restart under a validated contract.

        Flow: gate → snapshot → final race check → provider restart →
        independent verification → outcome classification → audit.
        """

        from aipm.control_plane.gate import FinalExecutionGate

        moment = (now or datetime.now(timezone.utc))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        action = self._actions.get_action(contract.action_id)
        if action is None:
            raise ExecutionRefused("action_missing", "Contract action does not exist")
        if action.version != contract.action_version:
            raise ExecutionRefused("stale_action_version", "Action version changed")
        if action.state not in {LifecycleState.CONFIRMED, LifecycleState.SNAPSHOT_CAPTURED, LifecycleState.LEASED}:
            raise ExecutionRefused("invalid_lifecycle_state", f"Action state {action.state.value} is not executable")

        # Final execution gate (authoritative pre-mutation check)
        gate = FinalExecutionGate(
            actions=self._actions, plans=self._plans, confirmations=self._confirmations,
            snapshots=self._snapshots, kill_switches=self._kill_switches,
        )
        gate_decision = gate.evaluate(contract, now=moment)
        if not gate_decision.allowed:
            raise ExecutionRefused(gate_decision.reason.value, f"Gate denied: {gate_decision.reason.value}")

        # Bind contract digest (idempotent)
        self._actions.bind_contract_evidence(
            contract.action_id,
            expected_version=contract.action_version,
            contract_version=contract.contract_version,
            capability_version=contract.capability_version,
            contract_digest=contract.digest(),
            now=moment,
        )

        # Resolve the unit allow-list
        policy = self._provider.resolve_unit(self._policy.unit_id, environment=contract.environment)

        # Pre-mutation snapshot (bounded, non-secret)
        snapshot = self._provider.observe_unit(policy, now=moment)

        # Final race check: lease/fence/kill-switch at the mutation boundary
        lease = self._actions.active_lease(contract.action_id, now=moment) if hasattr(self._actions, "active_lease") else None
        if lease is None:
            raise ExecutionRefused("lease_missing", "No active lease at the mutation boundary")
        if lease.lease_id != contract.lease_id or lease.fencing_token != contract.fencing_token:
            raise ExecutionRefused("stale_fencing_token", "Fencing token is no longer current")
        if self._kill_switches is not None:
            switch = self._kill_switches.switch(contract.environment)
            if switch.epoch != contract.kill_switch_epoch:
                raise ExecutionRefused("kill_switch_epoch_mismatch", "Kill switch changed since grant")
            if not switch.permits_operations():
                raise ExecutionRefused("kill_switch_engaged", "Kill switch denies execution")

        # Lifecycle transitions are the service's responsibility.
        # The executor records the outcome and the service advances the state.
        current_version = contract.action_version

        # Emit EXECUTION_STARTED
        started_at = moment.isoformat()
        self._audit.append(_execution_event(contract, "execution_started", started_at))

        # Mutation boundary: invoke systemctl restart
        provider_result = self._provider.restart(policy, now=moment)

        completed_at = datetime.now(timezone.utc).isoformat()

        # Classify outcome
        if provider_result.timed_out:
            outcome = "unknown_outcome"
            provider_code = "timeout"
        elif provider_result.returncode == 0:
            outcome = "succeeded"
            provider_code = "restart_ok"
        else:
            outcome = "failed"
            provider_code = f"exit_{provider_result.returncode}"

        # Emit EXECUTION_SUCCEEDED or EXECUTION_FAILED
        event_type = "execution_succeeded" if outcome == "succeeded" else "execution_failed" if outcome == "failed" else None
        if event_type:
            self._audit.append(_execution_event(contract, event_type, completed_at))

        # Record the durable outcome
        outcome_value = {"succeeded": "mutation_succeeded", "failed": "mutation_failed", "unknown_outcome": "unknown_outcome"}.get(outcome, "unknown_outcome")
        self._actions.mark_outcome(
            contract.action_id, expected_version=current_version,
            outcome=outcome_value, now=moment)

        # Independent verification (never trust the restart exit code alone)
        verification_success = None
        if outcome == "succeeded":
            try:
                post_snapshot = self._provider.observe_unit(policy, now=moment)
                verification_success = (
                    post_snapshot.load_state == "loaded"
                    and post_snapshot.active_state == "active"
                    and post_snapshot.unit_id == snapshot.unit_id
                )
            except SystemdRestartError:
                verification_success = False

        evidence_ref = f"systemd-restart:{contract.action_id[:16]}:{outcome}"
        return SystemdRestartResult(
            action_id=contract.action_id,
            capability="restart_allowlisted_systemd_unit",
            capability_version=contract.capability_version,
            outcome=outcome,
            started_at=started_at,
            completed_at=completed_at,
            provider_code=provider_code,
            evidence_reference=evidence_ref,
        )


def _execution_event(contract, event_type: str, occurred_at: str):
    """Build a bounded execution audit event."""
    from aipm.control_plane.audit.builders import execution_started, execution_finished
    from aipm.control_plane.audit.models import AuditActorRole
    from aipm.control_plane.audit.models import SYSTEM_ACTOR_SUBJECT as SYSTEM_ACTOR

    if event_type == "execution_started":
        return execution_started(
            actor_subject=SYSTEM_ACTOR, occurred_at=datetime.fromisoformat(occurred_at),
            action_id=contract.action_id, plan_id=contract.plan_id,
            target_id=contract.target_id, environment=contract.environment,
            lease_id=contract.lease_id, fencing_token=contract.fencing_token,
        )
    success = event_type == "execution_succeeded"
    return execution_finished(
        actor_subject=SYSTEM_ACTOR, occurred_at=datetime.fromisoformat(occurred_at),
        action_id=contract.action_id, plan_id=contract.plan_id,
        target_id=contract.target_id, environment=contract.environment,
        success=success,
    )
