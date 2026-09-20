"""MC-6.16-D2.2 Gate B / B3: Registration Enforcement at FinalExecutionGate Tests.

Validates the authoritative pre-mutation registration enforcement for modern actions:
A. modern + valid REGISTERED registration -> allowed through registration gate
B. modern + missing registration -> denied (GateCode.REGISTRATION_MISSING / REGISTRATION_ID_MISSING)
C. modern + REVOKED registration -> denied (GateCode.REGISTRATION_REVOKED)
D. modern + binding registration_id mismatch -> denied (GateCode.REGISTRATION_ID_MISMATCH)
E. modern + binding registration_digest mismatch -> denied (GateCode.REGISTRATION_DIGEST_MISMATCH)
F. modern + target mismatch -> denied (GateCode.REGISTRATION_TARGET_MISMATCH)
G. modern + environment mismatch -> denied (GateCode.REGISTRATION_ENVIRONMENT_MISMATCH)
H. modern + tampered registration digest -> denied (GateCode.REGISTRATION_DIGEST_MISMATCH)
I. legacy + MUTATION -> denied regardless of registration (GateCode.LEGACY_MUTATION_BLOCKED)
J. genuine legacy + RECONCILIATION -> preserve B2.1 behavior (GateCode.ALLOWED)
K. caller cannot select a different registration_id to authorize execution
L. caller cannot select a different registration_digest to authorize execution
M. registration history does not allow an old REVOKED registration to become the active authorization
N. DISABLED registration -> blocks modern mutation (GateCode.REGISTRATION_DISABLED)
O. Concurrency & immutability: binding tied to registration_id and registration_digest prevents switching
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from aipm.control_plane.executor import (
    EXECUTION_CONTRACT_VERSION,
    ExecutionContract,
    ExecutorCapability,
)
from aipm.control_plane.gate import (
    FinalExecutionGate,
    GateCode,
    OperationCategory,
)
from aipm.control_plane.models import (
    LifecycleState,
    UpdateExecutionBinding,
)
from aipm.control_plane.registration import (
    ProjectRegistration,
    RegistrationStatus,
    compute_registration_digest,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
ACTION_ID = "a" * 64
PLAN_DIGEST = "b" * 64
CONTRACT_DIGEST = "c" * 64
CONFIRMATION_ID = "d" * 32
LEASE_ID = "e" * 32
FENCING_TOKEN = 7
TARGET_ID = "production-app"
CANONICAL_PATH = "/home/ubuntu/production-app"
ENVIRONMENT = "staging"

REGISTRATION_ID = "550e8400-e29b-41d4-a716-446655440010"
COMPOSE_FILE_HASHES = ["1" * 64, "2" * 64]
CANONICAL_REG_DIGEST = compute_registration_digest(
    target_id=TARGET_ID,
    canonical_project_path=CANONICAL_PATH,
    runtime_mode="compose",
    environment=ENVIRONMENT,
    compose_project_name="prod_app",
    compose_file_hashes=COMPOSE_FILE_HASHES,
    registered_at_iso=NOW.isoformat(),
)


class _StubAction:
    def __init__(self, action_protocol="mc616d2-v1", state="leased", version=1, target_id=TARGET_ID, environment=ENVIRONMENT, outcome=None):
        self.action_id = ACTION_ID
        self.version = version
        self.state = LifecycleState(state) if isinstance(state, str) else state
        self.action_protocol = action_protocol
        self.outcome = outcome
        self.decision_id = "decision-01"
        self.plan_id = "plan-01"
        self.plan_revision = 1
        self.expires_at = NOW + timedelta(minutes=10)
        self.scope = type("Scope", (), {
            "policy_version": "policy-v1",
            "target_id": target_id,
            "environment": environment,
        })()

    def is_expired(self, now):
        return False


class _StubLease:
    def __init__(self):
        self.lease_id = LEASE_ID
        self.fencing_token = FENCING_TOKEN
        self.expires_at = NOW + timedelta(minutes=10)


class _StubConfirmationBinding:
    def __init__(self):
        self.confirmation_id = CONFIRMATION_ID
        self.action_id = ACTION_ID
        self.target_digest = PLAN_DIGEST
        self.state = type("State", (), {"value": "confirmed"})()

    def is_expired(self, now):
        return False


class _StubPlan:
    def __init__(self):
        self.revision = 1

    def digest(self):
        return PLAN_DIGEST


class _StubActionRepo:
    def __init__(self, action, contract_digest=CONTRACT_DIGEST):
        self._action = action
        self._contract_digest = contract_digest
        self._lease = _StubLease()

    def get_action(self, action_id: str):
        return self._action if self._action and self._action.action_id == action_id else None

    def get_contract_evidence(self, action_id: str):
        return {"contract_digest": self._contract_digest, "capability_version": "1"}

    def active_lease(self, action_id: str, now: datetime = None):
        return self._lease

    def outcome_for_action(self, action_id: str):
        return getattr(self._action, "outcome", None)

    def get_decision(self, decision_id: str):
        return None


class _StubPlans:
    def read(self, target_id: str):
        return _StubPlan()


class _StubConfirmations:
    def __init__(self):
        self.store = {CONFIRMATION_ID: _StubConfirmationBinding()}


class _MultiRegistrationStore:
    """In-memory registration store capable of holding active and historical registrations."""

    def __init__(self, registrations: list[ProjectRegistration] | None = None):
        self._by_id: dict[str, ProjectRegistration] = {}
        self._registrations: list[ProjectRegistration] = []
        for r in (registrations or []):
            self.add(r)

    def add(self, reg: ProjectRegistration):
        self._by_id[reg.registration_id] = reg
        self._registrations.append(reg)

    def get(self, target_id: str, environment: str) -> ProjectRegistration | None:
        """Returns the most recent active registration (REGISTERED or DISABLED)."""
        for reg in reversed(self._registrations):
            if reg.target_id == target_id and reg.environment == environment:
                if reg.status in (RegistrationStatus.REGISTERED, RegistrationStatus.DISABLED):
                    return reg
        return None

    def get_by_registration_id(self, registration_id: str) -> ProjectRegistration | None:
        return self._by_id.get(registration_id)


def _make_contract(target_id=TARGET_ID, environment=ENVIRONMENT):
    return ExecutionContract(
        contract_version=EXECUTION_CONTRACT_VERSION,
        action_id=ACTION_ID,
        action_version=1,
        operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
        target_id=target_id,
        environment=environment,
        plan_id="plan-01",
        expected_plan_revision=1,
        expected_plan_digest=PLAN_DIGEST,
        mutation_fields=(("title", "Updated"),),
        snapshot_id="snapshot-01",
        decision_id="decision-01",
        confirmation_id=CONFIRMATION_ID,
        policy_version="policy-v1",
        verification_version="v1",
        kill_switch_epoch=1,
        lease_id=LEASE_ID,
        fencing_token=FENCING_TOKEN,
        expires_at=NOW + timedelta(minutes=10),
        capability_version="1",
    )


def _make_valid_registration(
    registration_id=REGISTRATION_ID,
    target_id=TARGET_ID,
    environment=ENVIRONMENT,
    status=RegistrationStatus.REGISTERED,
    registration_digest=CANONICAL_REG_DIGEST,
    compose_file_hashes=COMPOSE_FILE_HASHES,
):
    reg = ProjectRegistration(
        registration_id=registration_id,
        target_id=target_id,
        environment=environment,
        status=status,
        canonical_project_path=CANONICAL_PATH,
        runtime_mode="compose",
        registration_digest=registration_digest,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=NOW,
        compose_project_name="prod_app",
    )
    if compose_file_hashes is not None:
        object.__setattr__(reg, "compose_file_hashes", compose_file_hashes)
    return reg


def _make_binding(
    action_protocol="mc616d2-v1",
    registration_id=REGISTRATION_ID,
    registration_digest=CANONICAL_REG_DIGEST,
    target_id=TARGET_ID,
):
    return UpdateExecutionBinding(
        project_name=target_id,
        plan_digest=PLAN_DIGEST,
        confirmation_id=CONFIRMATION_ID,
        action_id=ACTION_ID,
        contract_digest=CONTRACT_DIGEST,
        lease_id=LEASE_ID,
        fencing_token=FENCING_TOKEN,
        action_protocol=action_protocol,
        registration_id=registration_id,
        registration_digest=registration_digest,
    )


def _build_gate(action, contract=None, registrations=None):
    contract_digest = contract.digest() if contract else CONTRACT_DIGEST
    repo = _StubActionRepo(action, contract_digest=contract_digest)
    return FinalExecutionGate(
        actions=repo,
        plans=_StubPlans(),
        confirmations=_StubConfirmations(),
        registrations=registrations,
    )


# =========================================================================
# Matrix Tests A through O
# =========================================================================

def test_a_modern_valid_registered_registration_allowed():
    """A. modern + valid REGISTERED registration -> allowed through registration gate."""
    reg = _make_valid_registration()
    store = _MultiRegistrationStore([reg])
    action = _StubAction(action_protocol="mc616d2-v1")
    contract = _make_contract()
    binding = _make_binding(
        action_protocol="mc616d2-v1",
        registration_id=reg.registration_id,
        registration_digest=reg.registration_digest,
    )
    gate = _build_gate(action, contract, registrations=store)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
    )
    assert decision.allowed is True
    assert decision.reason is GateCode.ALLOWED
    assert decision.action_protocol == "mc616d2-v1"


def test_b_modern_missing_registration_denied():
    """B. modern + missing registration -> denied."""
    # Sub-case B1: registration store has no registration for target
    empty_store = _MultiRegistrationStore([])
    action = _StubAction(action_protocol="mc616d2-v1")
    contract = _make_contract()
    binding = _make_binding(action_protocol="mc616d2-v1")
    gate = _build_gate(action, contract, registrations=empty_store)

    d1 = gate.evaluate(contract, now=NOW, operation_category=OperationCategory.MUTATION, execution_binding=binding)
    assert d1.allowed is False
    assert d1.reason is GateCode.REGISTRATION_MISSING

    # Sub-case B2: gate has no registrations authority (registrations is None)
    gate_no_store = _build_gate(action, contract, registrations=None)
    d2 = gate_no_store.evaluate(contract, now=NOW, operation_category=OperationCategory.MUTATION, execution_binding=binding)
    assert d2.allowed is False
    assert d2.reason is GateCode.REGISTRATION_MISSING

    # Sub-case B3: binding missing registration_id
    binding_no_id = UpdateExecutionBinding(
        project_name=TARGET_ID,
        plan_digest=PLAN_DIGEST,
        confirmation_id=CONFIRMATION_ID,
        action_id=ACTION_ID,
        contract_digest=CONTRACT_DIGEST,
        lease_id=LEASE_ID,
        fencing_token=FENCING_TOKEN,
        action_protocol="mc616d2-v1",
        registration_id=None,
        registration_digest=CANONICAL_REG_DIGEST,
    )
    reg = _make_valid_registration()
    gate_with_reg = _build_gate(action, contract, registrations=_MultiRegistrationStore([reg]))
    d3 = gate_with_reg.evaluate(contract, now=NOW, operation_category=OperationCategory.MUTATION, execution_binding=binding_no_id)
    assert d3.allowed is False
    assert d3.reason is GateCode.REGISTRATION_ID_MISSING

    # Sub-case B4: binding missing registration_digest
    binding_no_digest = UpdateExecutionBinding(
        project_name=TARGET_ID,
        plan_digest=PLAN_DIGEST,
        confirmation_id=CONFIRMATION_ID,
        action_id=ACTION_ID,
        contract_digest=CONTRACT_DIGEST,
        lease_id=LEASE_ID,
        fencing_token=FENCING_TOKEN,
        action_protocol="mc616d2-v1",
        registration_id=REGISTRATION_ID,
        registration_digest=None,
    )
    d4 = gate_with_reg.evaluate(contract, now=NOW, operation_category=OperationCategory.MUTATION, execution_binding=binding_no_digest)
    assert d4.allowed is False
    assert d4.reason is GateCode.REGISTRATION_DIGEST_MISSING


def test_c_modern_revoked_registration_denied():
    """C. modern + REVOKED registration -> denied (GateCode.REGISTRATION_REVOKED)."""
    revoked_reg = _make_valid_registration(status=RegistrationStatus.REVOKED)
    store = _MultiRegistrationStore([revoked_reg])
    action = _StubAction(action_protocol="mc616d2-v1")
    contract = _make_contract()
    binding = _make_binding(
        action_protocol="mc616d2-v1",
        registration_id=revoked_reg.registration_id,
        registration_digest=revoked_reg.registration_digest,
    )
    gate = _build_gate(action, contract, registrations=store)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.REGISTRATION_REVOKED


def test_d_modern_binding_registration_id_mismatch_denied():
    """D. modern + binding registration_id mismatch -> denied (GateCode.REGISTRATION_ID_MISMATCH)."""
    reg = _make_valid_registration(registration_id="550e8400-e29b-41d4-a716-446655440011")
    store = _MultiRegistrationStore([reg])
    action = _StubAction(action_protocol="mc616d2-v1")
    contract = _make_contract()
    # Binding points to a different registration_id
    binding = _make_binding(
        action_protocol="mc616d2-v1",
        registration_id="550e8400-e29b-41d4-a716-446655440099",
        registration_digest=reg.registration_digest,
    )
    gate = _build_gate(action, contract, registrations=store)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.REGISTRATION_ID_MISMATCH


def test_e_modern_binding_registration_digest_mismatch_denied():
    """E. modern + binding registration_digest mismatch -> denied (GateCode.REGISTRATION_DIGEST_MISMATCH)."""
    reg = _make_valid_registration()
    store = _MultiRegistrationStore([reg])
    action = _StubAction(action_protocol="mc616d2-v1")
    contract = _make_contract()
    # Binding carries wrong registration_digest
    binding = _make_binding(
        action_protocol="mc616d2-v1",
        registration_id=reg.registration_id,
        registration_digest="f" * 64,
    )
    gate = _build_gate(action, contract, registrations=store)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.REGISTRATION_DIGEST_MISMATCH


def test_f_modern_target_mismatch_denied():
    """F. modern + target mismatch -> denied (GateCode.REGISTRATION_TARGET_MISMATCH)."""
    # Registration is for "target-alpha", contract is for "production-app"
    reg = _make_valid_registration(target_id="target-alpha")
    store = _MultiRegistrationStore([reg])
    action = _StubAction(action_protocol="mc616d2-v1", target_id="target-alpha")
    contract = _make_contract(target_id="production-app")  # contract target mismatch!
    binding = _make_binding(
        action_protocol="mc616d2-v1",
        registration_id=reg.registration_id,
        registration_digest=reg.registration_digest,
        target_id="production-app",
    )
    gate = _build_gate(action, contract, registrations=store)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
    )
    assert decision.allowed is False
    assert decision.reason in (GateCode.REGISTRATION_MISSING, GateCode.REGISTRATION_TARGET_MISMATCH)


def test_g_modern_environment_mismatch_denied():
    """G. modern + environment mismatch -> denied (GateCode.REGISTRATION_ENVIRONMENT_MISMATCH)."""
    # Target registered in development, execution contract is staging
    dev_reg = _make_valid_registration(environment="development")
    store = _MultiRegistrationStore([dev_reg])
    action = _StubAction(action_protocol="mc616d2-v1", environment="staging")
    contract = _make_contract(environment="staging")
    binding = _make_binding(
        action_protocol="mc616d2-v1",
        registration_id=dev_reg.registration_id,
        registration_digest=dev_reg.registration_digest,
    )
    gate = _build_gate(action, contract, registrations=store)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
    )
    assert decision.allowed is False
    assert decision.reason in (GateCode.REGISTRATION_ENVIRONMENT_MISMATCH, GateCode.REGISTRATION_MISSING)


def test_h_modern_tampered_registration_digest_denied():
    """H. modern + tampered registration digest -> denied."""
    # Store registration facts were tampered (e.g., path changed without updating digest)
    reg = _make_valid_registration()
    # Tamper with path
    tampered_reg = ProjectRegistration(
        registration_id=reg.registration_id,
        target_id=reg.target_id,
        environment=reg.environment,
        status=reg.status,
        canonical_project_path="/home/ubuntu/tampered-path",  # tampered!
        runtime_mode=reg.runtime_mode,
        registration_digest=reg.registration_digest,  # old digest no longer matches
        registration_version=reg.registration_version,
        registered_by=reg.registered_by,
        registered_at=reg.registered_at,
        compose_project_name=reg.compose_project_name,
    )
    object.__setattr__(tampered_reg, "compose_file_hashes", COMPOSE_FILE_HASHES)

    store = _MultiRegistrationStore([tampered_reg])
    action = _StubAction(action_protocol="mc616d2-v1")
    contract = _make_contract()
    binding = _make_binding(
        action_protocol="mc616d2-v1",
        registration_id=tampered_reg.registration_id,
        registration_digest=tampered_reg.registration_digest,
    )
    gate = _build_gate(action, contract, registrations=store)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.REGISTRATION_DIGEST_MISMATCH


def test_i_legacy_mutation_denied_regardless_of_registration():
    """I. legacy + MUTATION -> denied regardless of registration."""
    reg = _make_valid_registration()
    store = _MultiRegistrationStore([reg])
    action = _StubAction(action_protocol="legacy-v1")
    contract = _make_contract()
    binding = _make_binding(
        action_protocol="legacy-v1",
        registration_id=reg.registration_id,
        registration_digest=reg.registration_digest,
    )
    gate = _build_gate(action, contract, registrations=store)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.LEGACY_MUTATION_BLOCKED


def test_j_genuine_legacy_reconciliation_preserves_b2_1_behavior():
    """J. genuine legacy + RECONCILIATION -> preserve B2.1 behavior."""
    # Even without registration authority configured, genuine legacy reconciliation works
    action = _StubAction(
        action_protocol="legacy-v1",
        state=LifecycleState.RECONCILIATION_REQUIRED,
    )
    contract = _make_contract()
    binding = _make_binding(action_protocol="legacy-v1")
    gate = _build_gate(action, contract, registrations=None)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.RECONCILIATION,
        execution_binding=binding,
    )
    assert decision.allowed is True
    assert decision.reason is GateCode.ALLOWED
    assert decision.action_protocol == "legacy-v1"


def test_k_caller_cannot_select_different_registration_id():
    """K. caller cannot select a different registration_id to authorize execution."""
    reg = _make_valid_registration(registration_id="550e8400-e29b-41d4-a716-446655440012")
    store = _MultiRegistrationStore([reg])
    action = _StubAction(action_protocol="mc616d2-v1")
    contract = _make_contract()
    binding = _make_binding(
        action_protocol="mc616d2-v1",
        registration_id="550e8400-e29b-41d4-a716-446655440099",  # forged foreign id
        registration_digest=reg.registration_digest,
    )
    gate = _build_gate(action, contract, registrations=store)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.REGISTRATION_ID_MISMATCH


def test_l_caller_cannot_select_different_registration_digest():
    """L. caller cannot select a different registration_digest to authorize execution."""
    reg = _make_valid_registration()
    store = _MultiRegistrationStore([reg])
    action = _StubAction(action_protocol="mc616d2-v1")
    contract = _make_contract()
    binding = _make_binding(
        action_protocol="mc616d2-v1",
        registration_id=reg.registration_id,
        registration_digest="e" * 64,  # forged foreign digest
    )
    gate = _build_gate(action, contract, registrations=store)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.REGISTRATION_DIGEST_MISMATCH


def test_m_registration_history_blocks_old_revoked_registration():
    """M. registration history does not allow an old REVOKED registration to become active authorization."""
    old_revoked_reg = _make_valid_registration(
        registration_id="550e8400-e29b-41d4-a716-446655440001",
        status=RegistrationStatus.REVOKED,
    )
    new_active_reg = _make_valid_registration(
        registration_id="550e8400-e29b-41d4-a716-446655440002",
        status=RegistrationStatus.REGISTERED,
    )
    store = _MultiRegistrationStore([old_revoked_reg, new_active_reg])
    action = _StubAction(action_protocol="mc616d2-v1")
    contract = _make_contract()

    # Attacker attempts to authorize execution using old revoked registration_id
    binding_with_old_revoked = _make_binding(
        action_protocol="mc616d2-v1",
        registration_id=old_revoked_reg.registration_id,
        registration_digest=old_revoked_reg.registration_digest,
    )
    gate = _build_gate(action, contract, registrations=store)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding_with_old_revoked,
    )
    # The active registration is new_active_reg, so old_revoked_reg ID mismatches the active one
    assert decision.allowed is False
    assert decision.reason is GateCode.REGISTRATION_ID_MISMATCH


def test_n_disabled_registration_blocks_mutation():
    """N. DISABLED registration blocks mutation (GateCode.REGISTRATION_DISABLED)."""
    disabled_reg = _make_valid_registration(status=RegistrationStatus.DISABLED)
    store = _MultiRegistrationStore([disabled_reg])
    action = _StubAction(action_protocol="mc616d2-v1")
    contract = _make_contract()
    binding = _make_binding(
        action_protocol="mc616d2-v1",
        registration_id=disabled_reg.registration_id,
        registration_digest=disabled_reg.registration_digest,
    )
    gate = _build_gate(action, contract, registrations=store)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.REGISTRATION_DISABLED


def test_o_concurrency_identity_immutable():
    """O. Concurrency & immutability: registration_id cannot be changed on binding or action."""
    binding = _make_binding(action_protocol="mc616d2-v1")
    with pytest.raises((AttributeError, TypeError)):
        binding.registration_id = "550e8400-e29b-41d4-a716-446655440099"

    with pytest.raises((AttributeError, TypeError)):
        binding.registration_digest = "f" * 64


def test_p_execution_binding_boundary_contract():
    """P. Execution-binding boundary contract:
    - modern mutation WITH execution_binding => registration verification is mandatory.
    - modern mutation WITHOUT execution_binding => evaluated without registration binding check (MC-6.12 compatibility).
    """
    action = _StubAction(action_protocol="mc616d2-v1")
    contract = _make_contract()

    # Case 1: Without execution_binding, registration verification is not triggered
    gate_no_reg = _build_gate(action, contract, registrations=None)
    decision_no_binding = gate_no_reg.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=None,
    )
    assert decision_no_binding.allowed is True
    assert decision_no_binding.reason is GateCode.ALLOWED

    # Case 2: WITH execution_binding, registration verification is MANDATORY and fails closed if missing
    decision_with_binding = gate_no_reg.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=_make_binding(action_protocol="mc616d2-v1"),
    )
    assert decision_with_binding.allowed is False
    assert decision_with_binding.reason is GateCode.REGISTRATION_MISSING


def test_q_action_lifecycle_construction_without_protocol_rejected():
    """Q. ActionLifecycle construction without explicit action_protocol must be rejected."""
    from aipm.control_plane.models import ActionLifecycle, ActionScope, OperationKind

    kwargs = {
        "action_id": ACTION_ID,
        "plan_id": "plan-01",
        "plan_digest": PLAN_DIGEST,
        "operation": OperationKind.UPDATE_PROJECT_PLAN,
        "scope": ActionScope(target_id=TARGET_ID, environment=ENVIRONMENT, policy_version="policy-v1"),
        "state": LifecycleState.REQUESTED,
        "requester_subject": "local-owner",
        "idempotency_key": "idem-01",
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=15),
    }
    with pytest.raises(TypeError, match="action_protocol"):
        ActionLifecycle(**kwargs)
