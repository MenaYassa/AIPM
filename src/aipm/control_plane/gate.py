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
from aipm.control_plane.verification import ExecutionOutcome
from aipm.control_plane.registration import RegistrationStatus, verify_registration_digest
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
    # MC-6.15-C.2 service plan gate codes:
    PLAN_IDENTITY_MISMATCH = "plan_identity_mismatch"
    DEPENDENCY_SCOPE_MISMATCH = "dependency_scope_mismatch"
    CURRENT_DIGEST_MISMATCH = "current_digest_mismatch"
    CANDIDATE_DIGEST_MISMATCH = "candidate_digest_mismatch"
    PROVENANCE_INVALID = "provenance_invalid"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    HEALTH_CONTRACT_MISMATCH = "health_contract_mismatch"
    LEASE_INVALID = "lease_invalid"
    CONFIRMATION_INVALID = "confirmation_invalid"
    # MC-6.16-D2.2 Gate B: Action protocol enforcement:
    ACTION_PROTOCOL_MISSING = "action_protocol_missing"
    ACTION_PROTOCOL_INVALID = "action_protocol_invalid"
    ACTION_PROTOCOL_MISMATCH = "action_protocol_mismatch"
    LEGACY_MUTATION_BLOCKED = "legacy_mutation_blocked"
    RECONCILIATION_INELIGIBLE = "reconciliation_ineligible"
    # MC-6.16-D2.2 Gate B3: Registration gate codes:
    REGISTRATION_MISSING = "registration_missing"
    REGISTRATION_ID_MISSING = "registration_id_missing"
    REGISTRATION_DIGEST_MISSING = "registration_digest_missing"
    REGISTRATION_REVOKED = "registration_revoked"
    REGISTRATION_DISABLED = "registration_disabled"
    REGISTRATION_INACTIVE = "registration_inactive"
    REGISTRATION_ID_MISMATCH = "registration_id_mismatch"
    REGISTRATION_DIGEST_MISMATCH = "registration_digest_mismatch"
    REGISTRATION_TARGET_MISMATCH = "registration_target_mismatch"
    REGISTRATION_ENVIRONMENT_MISMATCH = "registration_environment_mismatch"


class OperationCategory(str, enum.Enum):
    """Operation taxonomy: MUTATION vs RECONCILIATION vs PASSIVE_OBSERVATION.

    Explicitly distinguish operation categories to enforce protocol semantics:
    - MUTATION: State-changing mutation; legacy actions MUST NOT perform these.
    - RECONCILIATION: State-observing read-back for UNKNOWN_OUTCOME actions.
    - PASSIVE_OBSERVATION: Read-only queries, status inspection.
    """
    MUTATION = "mutation"
    RECONCILIATION = "reconciliation"
    PASSIVE_OBSERVATION = "passive_observation"


def verify_service_evidence(
    authorized: Any,
    fresh: Any,
    expected_digest: str | None = None,
) -> GateCode:
    """Compare authorized service evidence against freshly re-observed evidence.

    Deterministic fail-closed verification:
    - Provenance check
    - Project, Compose, and service identity
    - Requested and derived execution scopes
    - Atomicity classification
    - Current runtime digest
    - Target candidate digest and platform child digest
    - Candidate lookup key (including architecture/platform)
    - Health contract summary
    - Dependency scope and dependency health/readiness
    - Canonical plan digest
    """
    if not getattr(fresh, "provenance_verified", False):
        return GateCode.PROVENANCE_INVALID

    if (
        getattr(authorized, "project_name", None) != getattr(fresh, "project_name", None)
        or getattr(authorized, "compose_identity", None) != getattr(fresh, "compose_identity", None)
        or getattr(authorized, "service_name", None) != getattr(fresh, "service_name", None)
    ):
        return GateCode.PLAN_IDENTITY_MISMATCH

    if tuple(getattr(authorized, "requested_scope", ())) != tuple(getattr(fresh, "requested_scope", ())):
        return GateCode.DEPENDENCY_SCOPE_MISMATCH

    if tuple(getattr(authorized, "derived_execution_scope", ())) != tuple(getattr(fresh, "derived_execution_scope", ())):
        return GateCode.DEPENDENCY_SCOPE_MISMATCH

    if getattr(authorized, "atomicity", None) != getattr(fresh, "atomicity", None):
        return GateCode.PLAN_IDENTITY_MISMATCH

    if getattr(authorized, "current_runtime_digest", None) != getattr(fresh, "current_runtime_digest", None):
        return GateCode.CURRENT_DIGEST_MISMATCH

    if getattr(authorized, "target_candidate_digest", None) != getattr(fresh, "target_candidate_digest", None):
        return GateCode.CANDIDATE_DIGEST_MISMATCH

    if getattr(authorized, "target_candidate_child_digest", None) != getattr(fresh, "target_candidate_child_digest", None):
        return GateCode.CANDIDATE_DIGEST_MISMATCH

    if getattr(authorized, "candidate_lookup_key", None) != getattr(fresh, "candidate_lookup_key", None):
        return GateCode.CANDIDATE_DIGEST_MISMATCH

    if getattr(authorized, "health_contract_summary", None) != getattr(fresh, "health_contract_summary", None):
        return GateCode.HEALTH_CONTRACT_MISMATCH

    auth_deps = tuple(getattr(authorized, "dependency_scope", ()))
    fresh_deps = tuple(getattr(fresh, "dependency_scope", ()))
    if auth_deps != fresh_deps:
        for item in fresh_deps:
            if "status:missing" in item or "health:unhealthy" in item or "state:missing" in item or "state:error" in item:
                return GateCode.DEPENDENCY_BLOCKED
        return GateCode.DEPENDENCY_SCOPE_MISMATCH

    fresh_digest = getattr(fresh, "plan_digest", None)
    auth_digest = getattr(authorized, "plan_digest", None)
    if expected_digest is not None and fresh_digest != expected_digest:
        return GateCode.PLAN_IDENTITY_MISMATCH
    if auth_digest != fresh_digest:
        return GateCode.PLAN_IDENTITY_MISMATCH

    return GateCode.ALLOWED


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
    action_protocol: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", bounded_reference(self.action_id, field="action id"))
        object.__setattr__(self, "capability_id", bounded_reference(self.capability_id, field="capability id", maximum=64))
        object.__setattr__(self, "capability_version", bounded_reference(self.capability_version, field="capability version", maximum=64))
        object.__setattr__(self, "contract_digest", bounded_reference(self.contract_digest, field="contract digest", maximum=64))
        object.__setattr__(self, "target_id", bounded_reference(self.target_id, field="target id"))
        if self.action_protocol is not None:
            object.__setattr__(self, "action_protocol", bounded_reference(self.action_protocol, field="action protocol", maximum=32))
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
            "action_protocol": self.action_protocol,
        }


class FinalExecutionGate:
    """Single authoritative pre-execution check; re-reads current world state."""

    __slots__ = ("_actions", "_plans", "_confirmations", "_snapshots", "_kill_switches", "_capability_registry", "_service_evidence_verifier", "_registrations", "_initialized")

    def __init__(self, *, actions, plans, confirmations, snapshots=None, kill_switches=None, capability_registry: CapabilityRegistry | None = None, service_evidence_verifier=None, registrations=None) -> None:
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
        object.__setattr__(self, "_service_evidence_verifier", service_evidence_verifier)
        reg_store = registrations
        if reg_store is None and hasattr(actions, "_db"):
            try:
                from aipm.control_plane.storage.sqlite_store import SQLiteProjectRegistrationStore
                reg_store = SQLiteProjectRegistrationStore(actions._db)
            except Exception:
                reg_store = None
        elif reg_store is None and hasattr(actions, "registrations"):
            reg_store = actions.registrations
        object.__setattr__(self, "_registrations", reg_store)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("FinalExecutionGate configuration is immutable")
        object.__setattr__(self, name, value)

    def _is_reconciliation_eligible(self, action: Any) -> bool:
        """Check if action has a durable reconciliation-eligible state or outcome.

        Reconciliation is valid only when:
        1. Action lifecycle state is RECONCILIATION_REQUIRED, OR
        2. Action durable outcome is UNKNOWN_OUTCOME.
        """
        state = getattr(action, "state", None)
        if state is LifecycleState.RECONCILIATION_REQUIRED or state == "reconciliation_required":
            return True
        if hasattr(state, "value") and state.value == "reconciliation_required":
            return True

        outcome = getattr(action, "outcome", None)
        if outcome in ("unknown_outcome", ExecutionOutcome.UNKNOWN_OUTCOME):
            return True
        if hasattr(outcome, "value") and outcome.value == "unknown_outcome":
            return True

        if hasattr(self._actions, "outcome_for_action"):
            action_id = getattr(action, "action_id", None)
            if action_id:
                repo_outcome = self._actions.outcome_for_action(action_id)
                if repo_outcome in ("unknown_outcome", ExecutionOutcome.UNKNOWN_OUTCOME):
                    return True
                if hasattr(repo_outcome, "value") and repo_outcome.value == "unknown_outcome":
                    return True

        return False

    def evaluate(
        self,
        contract: "ExecutionContract",
        *,
        now: datetime | None = None,
        service_evidence: Any | None = None,
        service_scope: tuple[str, ...] | None = None,
        operation_category: OperationCategory = OperationCategory.MUTATION,
        execution_binding: Any | None = None,
        expected_protocol: str | None = None,
    ) -> ExecutionGateDecision:
        """Re-read current world state and produce a typed gate decision."""

        moment = contract.expires_at if contract.expires_at.tzinfo is not None else contract.expires_at.replace(tzinfo=timezone.utc)
        evaluated_at = now if now is not None and now.tzinfo is not None else (now.replace(tzinfo=timezone.utc) if now else datetime.now(timezone.utc))
        current_protocol: str | None = None

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
                action_protocol=current_protocol,
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

            # 1b. Action protocol enforcement (MC-6.16-D2.2 Gate B / AD-01)
            protocol = getattr(action, "action_protocol", None)
            if not protocol:
                return deny(GateCode.ACTION_PROTOCOL_MISSING)
            if not isinstance(protocol, str) or protocol not in ("legacy-v1", "mc616d2-v1"):
                return deny(GateCode.ACTION_PROTOCOL_INVALID)
            current_protocol = protocol

            # Validate against expected protocol or execution binding if provided
            req_protocol = expected_protocol
            if execution_binding is not None:
                binding_protocol = getattr(execution_binding, "action_protocol", None)
                if req_protocol is not None and req_protocol != binding_protocol:
                    return deny(GateCode.ACTION_PROTOCOL_MISMATCH)
                req_protocol = binding_protocol

            if req_protocol is not None and req_protocol != protocol:
                return deny(GateCode.ACTION_PROTOCOL_MISMATCH)

            # Operation taxonomy check: MUTATION vs RECONCILIATION vs PASSIVE_OBSERVATION
            try:
                op_cat = OperationCategory(operation_category)
            except (ValueError, TypeError):
                return deny(GateCode.INTERNAL_ERROR)

            if protocol == "legacy-v1" and op_cat is OperationCategory.MUTATION:
                return deny(GateCode.LEGACY_MUTATION_BLOCKED)

            if op_cat is OperationCategory.RECONCILIATION:
                if not self._is_reconciliation_eligible(action):
                    return deny(GateCode.RECONCILIATION_INELIGIBLE)

            # 1c. Modern mutation registration enforcement (MC-6.16-D2.2 Gate B3)
            # Execution-binding boundary:
            # Registration enforcement is scoped to modern mutations where an UpdateExecutionBinding
            # is present, because internal MC-6.12 UPDATE_PROJECT_PLAN gate calls do not carry an
            # UpdateExecutionBinding.
            #
            # When an execution binding is present for a modern mutation, registration verification
            # is mandatory:
            # - missing binding registration_id => deny (REGISTRATION_ID_MISSING)
            # - missing binding registration_digest => deny (REGISTRATION_DIGEST_MISSING)
            # - missing authoritative registration => deny (REGISTRATION_MISSING)
            # - revoked registration => deny (REGISTRATION_REVOKED)
            # - disabled registration => deny (REGISTRATION_DISABLED)
            # - inactive registration => deny (REGISTRATION_INACTIVE)
            # - registration ID mismatch => deny (REGISTRATION_ID_MISMATCH)
            # - registration digest mismatch => deny (REGISTRATION_DIGEST_MISMATCH)
            # - target mismatch => deny (REGISTRATION_TARGET_MISMATCH)
            # - environment mismatch => deny (REGISTRATION_ENVIRONMENT_MISMATCH)
            # - tampered authoritative registration => deny (REGISTRATION_DIGEST_MISMATCH)
            if protocol == "mc616d2-v1" and op_cat is OperationCategory.MUTATION:
                if execution_binding is not None:
                    binding_reg_id = getattr(execution_binding, "registration_id", None)
                    if not binding_reg_id:
                        return deny(GateCode.REGISTRATION_ID_MISSING)

                    binding_reg_digest = getattr(execution_binding, "registration_digest", None)
                    if not binding_reg_digest:
                        return deny(GateCode.REGISTRATION_DIGEST_MISSING)

                    if self._registrations is None:
                        return deny(GateCode.REGISTRATION_MISSING)

                    # Lookup authoritative registration for contract target and environment
                    target_id = contract.target_id
                    environment = contract.environment

                    authoritative_reg = self._registrations.get(target_id, environment)

                    if authoritative_reg is None:
                        # Check if a registration exists by ID (e.g. historical REVOKED or target/env mismatch)
                        reg_by_id = None
                        if hasattr(self._registrations, "get_by_registration_id"):
                            reg_by_id = self._registrations.get_by_registration_id(binding_reg_id)

                        if reg_by_id is not None:
                            status_val = reg_by_id.status.value if hasattr(reg_by_id.status, "value") else str(reg_by_id.status)
                            if status_val == "REVOKED":
                                return deny(GateCode.REGISTRATION_REVOKED)
                            if status_val == "DISABLED":
                                return deny(GateCode.REGISTRATION_DISABLED)
                            if reg_by_id.target_id != target_id:
                                return deny(GateCode.REGISTRATION_TARGET_MISMATCH)
                            if reg_by_id.environment != environment:
                                return deny(GateCode.REGISTRATION_ENVIRONMENT_MISMATCH)

                        return deny(GateCode.REGISTRATION_MISSING)

                    # Registration lifecycle check
                    status_val = authoritative_reg.status.value if hasattr(authoritative_reg.status, "value") else str(authoritative_reg.status)
                    if status_val == "REVOKED":
                        return deny(GateCode.REGISTRATION_REVOKED)
                    if status_val == "DISABLED":
                        return deny(GateCode.REGISTRATION_DISABLED)
                    if not authoritative_reg.is_active():
                        return deny(GateCode.REGISTRATION_INACTIVE)

                    # Environment and target isolation
                    if authoritative_reg.environment != environment:
                        return deny(GateCode.REGISTRATION_ENVIRONMENT_MISMATCH)
                    if authoritative_reg.target_id != target_id:
                        return deny(GateCode.REGISTRATION_TARGET_MISMATCH)

                    # Binding match
                    if binding_reg_id != authoritative_reg.registration_id:
                        return deny(GateCode.REGISTRATION_ID_MISMATCH)
                    if binding_reg_digest != authoritative_reg.registration_digest:
                        return deny(GateCode.REGISTRATION_DIGEST_MISMATCH)

                    # Registration digest integrity verification (tamper detection)
                    if not verify_registration_digest(authoritative_reg):
                        return deny(GateCode.REGISTRATION_DIGEST_MISMATCH)

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
            effective_evidence = service_evidence or getattr(contract, "service_evidence", None)
            if effective_evidence is not None:
                # Security invariant: Durable metadata carries authorized data; it does NOT create authority.
                # If metadata claims a service_scope or service_name, it must match canonical plan evidence.
                if service_scope is not None:
                    if tuple(service_scope) != tuple(getattr(effective_evidence, "derived_execution_scope", ())):
                        return deny(GateCode.DEPENDENCY_SCOPE_MISMATCH)

                if hasattr(self._actions, "get_decision") and getattr(action, "decision_id", None):
                    decision = self._actions.get_decision(action.decision_id)
                    if decision is not None and getattr(decision, "request", None) is not None:
                        dec_metadata = dict(getattr(decision.request, "metadata", ()))
                        if "service_scope" in dec_metadata:
                            dec_scope = tuple(s.strip() for s in dec_metadata["service_scope"].split(",") if s.strip())
                            if dec_scope != tuple(getattr(effective_evidence, "derived_execution_scope", ())):
                                return deny(GateCode.DEPENDENCY_SCOPE_MISMATCH)
                        if "service_name" in dec_metadata:
                            dec_svc = dec_metadata["service_name"].strip()
                            if dec_svc != getattr(effective_evidence, "service_name", None):
                                return deny(GateCode.PLAN_IDENTITY_MISMATCH)

                mutation_map = dict(contract.mutation_fields)
                if "service_scope" in mutation_map:
                    meta_scope = tuple(s.strip() for s in mutation_map["service_scope"].split(",") if s.strip())
                    if meta_scope != tuple(getattr(effective_evidence, "derived_execution_scope", ())):
                        return deny(GateCode.DEPENDENCY_SCOPE_MISMATCH)
                if "service_name" in mutation_map:
                    meta_svc = mutation_map["service_name"].strip()
                    if meta_svc != getattr(effective_evidence, "service_name", None):
                        return deny(GateCode.PLAN_IDENTITY_MISMATCH)

                if self._service_evidence_verifier is not None:
                    decision_code = (
                        self._service_evidence_verifier.verify(contract, effective_evidence, now=evaluated_at)
                        if hasattr(self._service_evidence_verifier, "verify")
                        else self._service_evidence_verifier(contract, effective_evidence, now=evaluated_at)
                    )
                    if decision_code is not GateCode.ALLOWED:
                        return deny(decision_code)
                else:
                    mutation_map = dict(contract.mutation_fields)
                    exp_digest = mutation_map.get("update_plan_digest") or contract.expected_plan_digest
                    decision_code = verify_service_evidence(
                        effective_evidence,
                        effective_evidence,
                        expected_digest=exp_digest,
                    )
                    if decision_code is not GateCode.ALLOWED:
                        return deny(decision_code)
            else:
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
                action_protocol=protocol,
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
