"""Final execution gate and typed decision for the control plane.

The gate is the single authoritative pre-execution check: it re-reads
current world state (action, plan, confirmation, snapshot, lease, kill
switch, policy, capability, expiry) and returns a typed immutable decision.
No external mutation occurs unless the gate allows it.

The control plane is the ONLY authority; providers and executors are
mechanisms. This gate must not be duplicated in transport, executor,
provider, dashboard, and the legacy updater.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aipm.control_plane.capabilities_registry import CapabilityId, CapabilityRegistry, CapabilityPolicyError
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from aipm.control_plane.executor import ExecutionContract
from aipm.control_plane.models import LifecycleState
from aipm.control_plane.audit.sanitize import bounded_reference

GATE_VERSION = "mc612-execution-gate-v1"

_TERMINAL_STATES = frozenset({
    LifecycleState.VERIFIED_SUCCESS,
    LifecycleState.EXECUTION_FAILED,
    LifecycleState.ROLLED_BACK,
    LifecycleState.ROLLBACK_FAILED,
    LifecycleState.REJECTED,
    LifecycleState.EXPIRED,
    LifecycleState.INVALIDATED,
})


class GateCode(enum.Enum):
    ALLOWED = "allowed"
    ACTION_MISSING = "action_missing"
    ACTION_TERMINAL = "action_terminal"
    STALE_ACTION_VERSION = "stale_action_version"
    STALE_PLAN = "stale_plan"
    CONTRACT_EXPIRED = "contract_expired"
    ACTION_EXPIRED = "action_expired"
    CONTRACT_DIGEST_MISMATCH = "contract_digest_mismatch"
    CAPABILITY_DISABLED = "capability_disabled"
    CAPABILITY_UNKNOWN = "capability_unknown"
    CAPABILITY_VERSION_MISMATCH = "capability_version_mismatch"
    ENVIRONMENT_DENIED = "environment_denied"
    POLICY_MISMATCH = "policy_mismatch"
    CONFIRMATION_MISSING = "confirmation_missing"
    CONFIRMATION_CONSUMED = "confirmation_consumed"
    CONFIRMATION_EXPIRED = "confirmation_expired"
    SNAPSHOT_MISSING = "snapshot_missing"
    SNAPSHOT_MISMATCH = "snapshot_mismatch"
    TARGET_MISMATCH = "target_mismatch"
    LEASE_MISSING = "lease_missing"
    LEASE_EXPIRED = "lease_expired"
    LEASE_FENCE_MISMATCH = "lease_fence_mismatch"
    KILL_SWITCH_ENGAGED = "kill_switch_engaged"
    KILL_SWITCH_EPOCH_MISMATCH = "kill_switch_epoch_mismatch"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class ExecutionGateDecision:
    """Typed immutable gate decision; the only authoritative pre-mutation output."""

    allowed: bool
    reason: GateCode
    action_id: str
    action_version: int
    capability_id: str
    capability_version: str
    contract_digest: str
    policy_version: str
    kill_switch_epoch: int
    target_id: str
    evaluated_at: datetime
    gate_version: str = GATE_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", bounded_reference(self.action_id, field="action id"))
        object.__setattr__(self, "capability_id", bounded_reference(self.capability_id, field="capability id", maximum=64))
        object.__setattr__(self, "capability_version", bounded_reference(self.capability_version, field="capability version", maximum=64))
        object.__setattr__(self, "contract_digest", bounded_reference(self.contract_digest, field="contract digest", maximum=64))
        object.__setattr__(self, "target_id", bounded_reference(self.target_id, field="target id"))
        if self.allowed != (self.reason is GateCode.ALLOWED):
            raise ValueError("Gate decision allowed/reason disagree")

    def safe_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason.value,
            "action_id": self.action_id,
            "action_version": self.action_version,
            "capability_id": self.capability_id,
            "capability_version": self.capability_version,
            "contract_digest": self.contract_digest,
            "policy_version": self.policy_version,
            "kill_switch_epoch": self.kill_switch_epoch,
            "target_id": self.target_id,
            "evaluated_at": self.evaluated_at.isoformat(),
            "gate_version": self.gate_version,
        }


class FinalExecutionGate:
    """Single authoritative pre-execution check; re-reads current world state."""

    __slots__ = ("_actions", "_plans", "_confirmations", "_snapshots", "_kill_switches", "_capability_registry", "_initialized")

    def __init__(self, *, actions, plans, confirmations, snapshots=None, kill_switches=None, capability_registry: CapabilityRegistry | None = None) -> None:
        if actions is None or not hasattr(actions, "get_action"):
            raise TypeError("gate requires the action repository")
        if plans is None or not hasattr(plans, "read"):
            raise TypeError("gate requires the plan store")
        if confirmations is None or not hasattr(confirmations, "store"):
            raise TypeError("gate requires the confirmation service")
        object.__setattr__(self, "_actions", actions)
        object.__setattr__(self, "_plans", plans)
        object.__setattr__(self, "_confirmations", confirmations)
        object.__setattr__(self, "_snapshots", snapshots)
        object.__setattr__(self, "_kill_switches", kill_switches)
        object.__setattr__(self, "_capability_registry", capability_registry or __import__("aipm.control_plane.capabilities_registry", fromlist=["DEFAULT_CAPABILITY_REGISTRY"]).DEFAULT_CAPABILITY_REGISTRY)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("FinalExecutionGate configuration is immutable")
        object.__setattr__(self, name, value)

    def evaluate(self, contract: "ExecutionContract", *, now: datetime | None = None) -> ExecutionGateDecision:
        """Re-read current world state and produce a typed gate decision."""

        moment = contract.expires_at if contract.expires_at.tzinfo is not None else contract.expires_at.replace(tzinfo=timezone.utc)
        evaluated_at = now if now is not None and now.tzinfo is not None else (now.replace(tzinfo=timezone.utc) if now else datetime.now(timezone.utc))

        def deny(reason: GateCode) -> ExecutionGateDecision:
            return ExecutionGateDecision(
                allowed=False, reason=reason, action_id=contract.action_id,
                action_version=contract.action_version,
                capability_id="apply_project_plan",
                capability_version=contract.capability_version,
                contract_digest=contract.digest(),
                policy_version=contract.policy_version,
                kill_switch_epoch=contract.kill_switch_epoch,
                target_id=contract.target_id,
                evaluated_at=evaluated_at,
            )

        try:
            # 1. Action exists, version matches, not terminal
            action = self._actions.get_action(contract.action_id)
            if action is None:
                return deny(GateCode.ACTION_MISSING)
            if action.state in _TERMINAL_STATES:
                return deny(GateCode.ACTION_TERMINAL)
            if action.version != contract.action_version:
                return deny(GateCode.STALE_ACTION_VERSION)
            if action.is_expired(evaluated_at):
                return deny(GateCode.ACTION_EXPIRED)

            # 2. Contract digest matches durable binding
            stored = self._actions.get_contract_evidence(action_id=contract.action_id)
            if stored is not None and stored.get("contract_digest") and stored["contract_digest"] != contract.digest():
                return deny(GateCode.CONTRACT_DIGEST_MISMATCH)
            if stored is not None and stored.get("capability_version") and stored["capability_version"] != contract.capability_version:
                return deny(GateCode.CAPABILITY_VERSION_MISMATCH)

            # 3. Capability registry
            # The executor capability "update_project_plan" maps to the
            # registry capability "apply_project_plan" (same bounded mutation).
            registry_capability = "apply_project_plan" if contract.operation.value == "update_project_plan" else contract.operation.value
            try:
                definition = self._capability_registry.require_executable(
                    registry_capability,
                    environment=contract.environment,
                    version=contract.capability_version,
                )
            except CapabilityPolicyError as exc:
                message = str(exc).lower()
                if "unknown" in message:
                    return deny(GateCode.CAPABILITY_UNKNOWN)
                if "version" in message:
                    return deny(GateCode.CAPABILITY_VERSION_MISMATCH)
                if "environment" in message:
                    return deny(GateCode.ENVIRONMENT_DENIED)
                return deny(GateCode.CAPABILITY_DISABLED)

            # 4. Policy version
            if action.scope.policy_version != contract.policy_version:
                return deny(GateCode.POLICY_MISMATCH)

            # 5. Confirmation exists, bound to action, unexpired, unconsumed
            binding = self._confirmations.store.get(contract.confirmation_id)
            if binding is None:
                return deny(GateCode.CONFIRMATION_MISSING)
            if binding.action_id != contract.action_id:
                return deny(GateCode.CONFIRMATION_MISSING)
            if binding.is_expired(evaluated_at):
                return deny(GateCode.CONFIRMATION_EXPIRED)
            if binding.state.value == "consumed":
                return deny(GateCode.CONFIRMATION_CONSUMED)

            # 6. Snapshot exists and matches
            if self._snapshots is not None:
                snapshot = self._snapshots.snapshot_for_action(contract.action_id)
                if snapshot is None:
                    return deny(GateCode.SNAPSHOT_MISSING)
                if snapshot.revision != contract.expected_plan_revision or snapshot.target_id != contract.target_id:
                    return deny(GateCode.SNAPSHOT_MISMATCH)

            # 7. Current plan re-check (TOCTOU)
            try:
                current_plan = self._plans.read(contract.target_id)
            except Exception:
                return deny(GateCode.STALE_PLAN)
            if current_plan.revision != contract.expected_plan_revision or current_plan.digest() != contract.expected_plan_digest:
                return deny(GateCode.STALE_PLAN)

            # 8. Lease active, bound, current fence
            lease = self._actions.active_lease(contract.action_id, now=evaluated_at) if hasattr(self._actions, "active_lease") else None
            if lease is None:
                return deny(GateCode.LEASE_MISSING)
            if lease.lease_id != contract.lease_id or lease.fencing_token != contract.fencing_token:
                return deny(GateCode.LEASE_FENCE_MISMATCH)
            if lease.expires_at <= evaluated_at:
                return deny(GateCode.LEASE_EXPIRED)

            # 9. Kill switch
            if self._kill_switches is not None:
                switch = self._kill_switches.switch(contract.environment)
                if switch.epoch != contract.kill_switch_epoch:
                    return deny(GateCode.KILL_SWITCH_EPOCH_MISMATCH)
                if not switch.permits_operations():
                    return deny(GateCode.KILL_SWITCH_ENGAGED)

            # 10. Contract not expired
            if contract.is_expired(evaluated_at):
                return deny(GateCode.CONTRACT_EXPIRED)

            return ExecutionGateDecision(
                allowed=True, reason=GateCode.ALLOWED, action_id=contract.action_id,
                action_version=contract.action_version,
                capability_id="apply_project_plan",
                capability_version=contract.capability_version,
                contract_digest=contract.digest(),
                policy_version=contract.policy_version,
                kill_switch_epoch=contract.kill_switch_epoch,
                target_id=contract.target_id,
                evaluated_at=evaluated_at,
            )
        except (TypeError, ValueError, AttributeError) as exc:
            return ExecutionGateDecision(
                allowed=False, reason=GateCode.INTERNAL_ERROR, action_id=contract.action_id,
                action_version=contract.action_version,
                capability_id=contract.operation.value if hasattr(contract.operation, "value") else str(contract.operation),
                capability_version=contract.capability_version,
                contract_digest=contract.digest(),
                policy_version=contract.policy_version,
                kill_switch_epoch=contract.kill_switch_epoch,
                target_id=contract.target_id,
                evaluated_at=evaluated_at,
            )
