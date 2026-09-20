"""MC-6.16-D2.2 Gate B / B2: FinalExecutionGate Protocol Enforcement Tests.

Tests the authoritative pre-mutation action_protocol enforcement in FinalExecutionGate:
A. modern action + modern binding => accepted at the protocol layer
B. legacy action + legacy binding => accepted only where operation category permits it
C. modern action + legacy binding => rejected
D. legacy action + modern binding => rejected
E. missing protocol => rejected
F. invalid protocol => rejected
G. caller attempts to select legacy for a modern action => rejected
H. action record protocol is immutable
I. binding protocol mismatch => rejected
"""
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aipm.control_plane.gate import (
    FinalExecutionGate,
    GateCode,
    OperationCategory,
    ExecutionGateDecision,
)
from aipm.control_plane.registration import (
    ProjectRegistration,
    RegistrationStatus,
)
from aipm.control_plane.models import (
    LifecycleState,
    UpdateExecutionBinding,
)
from aipm.control_plane.executor import (
    EXECUTION_CONTRACT_VERSION,
    ExecutionContract,
    ExecutorCapability,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
ACTION_ID = "a" * 64
PLAN_DIGEST = "b" * 64
CONTRACT_DIGEST = "c" * 64
CONFIRMATION_ID = "d" * 32
LEASE_ID = "e" * 32
FENCING_TOKEN = 7
TARGET_ID = "test-project"
REGISTRATION_ID = "550e8400-e29b-41d4-a716-446655440001"
REGISTRATION_DIGEST = "9" * 64


class _StubRegistrationStore:
    def __init__(self, registration=None):
        if registration is None:
            self._reg = ProjectRegistration(
                registration_id=REGISTRATION_ID,
                target_id=TARGET_ID,
                environment="staging",
                status=RegistrationStatus.REGISTERED,
                canonical_project_path="/home/ubuntu/test-project",
                runtime_mode="compose",
                registration_digest=REGISTRATION_DIGEST,
                registration_version="mc616-reg-v1",
                registered_by="operator",
                registered_at=NOW,
            )
        else:
            self._reg = registration

    def get(self, target_id: str, environment: str):
        if self._reg and self._reg.target_id == target_id and self._reg.environment == environment:
            if self._reg.status in (RegistrationStatus.REGISTERED, RegistrationStatus.DISABLED):
                return self._reg
        return None

    def get_by_registration_id(self, registration_id: str):
        if self._reg and self._reg.registration_id == registration_id:
            return self._reg
        return None


class _StubAction:
    def __init__(self, action_protocol="mc616d2-v1", state="leased", version=1, outcome=None):
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
            "target_id": TARGET_ID,
            "environment": "staging",
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


def _make_contract(contract_digest=CONTRACT_DIGEST):
    return ExecutionContract(
        contract_version=EXECUTION_CONTRACT_VERSION,
        action_id=ACTION_ID,
        action_version=1,
        operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
        target_id=TARGET_ID,
        environment="staging",
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


def _make_binding(
    action_protocol="mc616d2-v1",
    registration_id=REGISTRATION_ID,
    registration_digest=REGISTRATION_DIGEST,
):
    return UpdateExecutionBinding(
        project_name=TARGET_ID,
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


def _build_gate(action, contract=None, action_repo=None, registrations=None):
    contract_digest = contract.digest() if contract else CONTRACT_DIGEST
    repo = action_repo if action_repo is not None else _StubActionRepo(action, contract_digest=contract_digest)
    reg_store = registrations if registrations is not None else _StubRegistrationStore()
    return FinalExecutionGate(
        actions=repo,
        plans=_StubPlans(),
        confirmations=_StubConfirmations(),
        registrations=reg_store,
    )


# ---------------------------------------------------------------------------
# Requirement 9 Tests: A through I
# ---------------------------------------------------------------------------


def test_a_modern_action_modern_binding_accepted():
    """A. modern action + modern binding => accepted at the protocol layer."""
    action = _StubAction(action_protocol="mc616d2-v1")
    binding = _make_binding(action_protocol="mc616d2-v1")
    contract = _make_contract()
    gate = _build_gate(action, contract)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
    )
    assert decision.allowed is True
    assert decision.reason is GateCode.ALLOWED
    assert decision.action_protocol == "mc616d2-v1"


def test_b_legacy_action_legacy_binding_semantics():
    """B. legacy action + legacy binding => accepted ONLY where operation category permits it."""
    # When action is in genuine reconciliation state:
    action = _StubAction(action_protocol="legacy-v1", state=LifecycleState.RECONCILIATION_REQUIRED)
    binding = _make_binding(action_protocol="legacy-v1")
    contract = _make_contract()
    gate = _build_gate(action, contract)

    # Mutation is BLOCKED even in reconciliation-required state
    decision_mut = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
    )
    assert decision_mut.allowed is False
    assert decision_mut.reason is GateCode.LEGACY_MUTATION_BLOCKED
    assert decision_mut.action_protocol == "legacy-v1"

    # Reconciliation is ALLOWED for genuine reconciliation-eligible action
    decision_recon = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.RECONCILIATION,
        execution_binding=binding,
    )
    assert decision_recon.allowed is True
    assert decision_recon.reason is GateCode.ALLOWED
    assert decision_recon.action_protocol == "legacy-v1"

    # Passive observation is ALLOWED at the protocol layer
    decision_obs = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.PASSIVE_OBSERVATION,
        execution_binding=binding,
    )
    assert decision_obs.allowed is True
    assert decision_obs.reason is GateCode.ALLOWED
    assert decision_obs.action_protocol == "legacy-v1"


def test_c_modern_action_legacy_binding_rejected():
    """C. modern action + legacy binding => rejected."""
    action = _StubAction(action_protocol="mc616d2-v1")
    binding = _make_binding(action_protocol="legacy-v1")
    gate = _build_gate(action)
    contract = _make_contract()

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.ACTION_PROTOCOL_MISMATCH


def test_d_legacy_action_modern_binding_rejected():
    """D. legacy action + modern binding => rejected."""
    action = _StubAction(action_protocol="legacy-v1")
    binding = _make_binding(action_protocol="mc616d2-v1")
    gate = _build_gate(action)
    contract = _make_contract()

    # Even in reconciliation mode, the mismatch fails closed
    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.RECONCILIATION,
        execution_binding=binding,
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.ACTION_PROTOCOL_MISMATCH


def test_e_missing_protocol_rejected():
    """E. missing protocol => rejected."""
    action = _StubAction(action_protocol=None)
    gate = _build_gate(action)
    contract = _make_contract()

    decision = gate.evaluate(contract, now=NOW)
    assert decision.allowed is False
    assert decision.reason is GateCode.ACTION_PROTOCOL_MISSING

    # Empty string protocol is also missing
    action_empty = _StubAction(action_protocol="")
    gate_empty = _build_gate(action_empty)
    decision_empty = gate_empty.evaluate(contract, now=NOW)
    assert decision_empty.allowed is False
    assert decision_empty.reason is GateCode.ACTION_PROTOCOL_MISSING


def test_f_invalid_protocol_rejected():
    """F. invalid protocol => rejected."""
    for invalid_val in ["unknown-protocol", "v2", "mc616d2-v2", 123]:
        action = _StubAction(action_protocol=invalid_val)
        gate = _build_gate(action)
        contract = _make_contract()

        decision = gate.evaluate(
            contract,
            now=NOW,
            operation_category=OperationCategory.MUTATION,
        )
        assert decision.allowed is False
        assert decision.reason is GateCode.ACTION_PROTOCOL_INVALID


def test_g_caller_selects_legacy_for_modern_action_rejected():
    """G. caller attempts to select legacy for a modern action => rejected."""
    action = _StubAction(action_protocol="mc616d2-v1")
    gate = _build_gate(action)
    contract = _make_contract()

    # Caller attempts to pass expected_protocol="legacy-v1" to downgrade
    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        expected_protocol="legacy-v1",
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.ACTION_PROTOCOL_MISMATCH


def test_h_action_record_protocol_is_immutable(tmp_path: Path):
    """H. action record protocol is immutable."""
    from tests.test_mc612_stage12_contract_evidence import build_service, request, SECRET

    service, db, ledger, plans, clock = build_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity

    action = service._actions.get_action(identity.action_id)
    assert action is not None
    assert action.action_protocol == "mc616d2-v1"

    # Verify no store method exists to mutate action_protocol
    assert not hasattr(service._actions, "update_action_protocol")
    assert not hasattr(service._actions, "set_action_protocol")

    # Verify frozen model prevents attribute assignment
    with pytest.raises((AttributeError, TypeError)):
        action.action_protocol = "legacy-v1"  # dataclass is frozen

    # Verify raw database column is physical TEXT NOT NULL
    cursor = db.connection.cursor()
    cursor.execute("PRAGMA table_info(actions)")
    columns = {row[1]: {"notnull": row[3], "type": row[2]} for row in cursor.fetchall()}
    assert columns["action_protocol"]["notnull"] == 1
    assert "TEXT" in columns["action_protocol"]["type"].upper()

    db.close()


def test_i_binding_protocol_mismatch_rejected():
    """I. binding protocol mismatch => rejected."""
    action = _StubAction(action_protocol="mc616d2-v1")
    binding = _make_binding(action_protocol="mc616d2-v1")
    gate = _build_gate(action)
    contract = _make_contract()

    # Conflicting expected_protocol vs binding action_protocol
    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
        expected_protocol="legacy-v1",
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.ACTION_PROTOCOL_MISMATCH


# =========================================================================
# MC-6.16-D2.2 Gate B2.1: Reconciliation Authorization Hardening Tests
# =========================================================================

def test_b2_1_a_legacy_mutation_denied():
    """A. legacy + mutation -> DENY."""
    # Even if action is in reconciliation state, mutation is strictly blocked
    action = _StubAction(action_protocol="legacy-v1", state=LifecycleState.RECONCILIATION_REQUIRED)
    binding = _make_binding(action_protocol="legacy-v1")
    contract = _make_contract()
    gate = _build_gate(action, contract)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.MUTATION,
        execution_binding=binding,
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.LEGACY_MUTATION_BLOCKED


def test_b2_1_b_legacy_reconciliation_genuine_unknown_outcome_allowed():
    """B. legacy + reconciliation + genuine UNKNOWN_OUTCOME -> allowed."""
    binding = _make_binding(action_protocol="legacy-v1")
    contract = _make_contract()

    # Case 1: action state is RECONCILIATION_REQUIRED
    action1 = _StubAction(action_protocol="legacy-v1", state=LifecycleState.RECONCILIATION_REQUIRED)
    gate1 = _build_gate(action1, contract)
    d1 = gate1.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.RECONCILIATION,
        execution_binding=binding,
    )
    assert d1.allowed is True
    assert d1.reason is GateCode.ALLOWED
    assert d1.action_protocol == "legacy-v1"

    # Case 2: action durable outcome is UNKNOWN_OUTCOME
    action2 = _StubAction(action_protocol="legacy-v1", state=LifecycleState.LEASED, outcome="unknown_outcome")
    gate2 = _build_gate(action2, contract)
    d2 = gate2.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.RECONCILIATION,
        execution_binding=binding,
    )
    assert d2.allowed is True
    assert d2.reason is GateCode.ALLOWED
    assert d2.action_protocol == "legacy-v1"

    # Case 3: repo returns UNKNOWN_OUTCOME via outcome_for_action
    action3 = _StubAction(action_protocol="legacy-v1", state=LifecycleState.LEASED)
    repo3 = _StubActionRepo(action3, contract_digest=contract.digest())
    repo3.outcome_for_action = lambda aid: "unknown_outcome"
    gate3 = _build_gate(action3, contract, action_repo=repo3)
    d3 = gate3.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.RECONCILIATION,
        execution_binding=binding,
    )
    assert d3.allowed is True
    assert d3.reason is GateCode.ALLOWED


def test_b2_1_c_legacy_reconciliation_ordinary_active_state_denied():
    """C. legacy + reconciliation + ordinary active state -> DENY."""
    binding = _make_binding(action_protocol="legacy-v1")
    contract = _make_contract()

    for active_state in [LifecycleState.LEASED, LifecycleState.CONFIRMED, LifecycleState.RUNNING]:
        action = _StubAction(action_protocol="legacy-v1", state=active_state, outcome=None)
        gate = _build_gate(action, contract)
        decision = gate.evaluate(
            contract,
            now=NOW,
            operation_category=OperationCategory.RECONCILIATION,
            execution_binding=binding,
        )
        assert decision.allowed is False
        assert decision.reason is GateCode.RECONCILIATION_INELIGIBLE


def test_b2_1_d_legacy_reconciliation_terminal_state_denied():
    """D. legacy + reconciliation + terminal/non-reconciliation state -> DENY."""
    binding = _make_binding(action_protocol="legacy-v1")
    contract = _make_contract()

    for terminal_state in [LifecycleState.VERIFIED_SUCCESS, LifecycleState.EXECUTION_FAILED, LifecycleState.ROLLED_BACK]:
        action = _StubAction(action_protocol="legacy-v1", state=terminal_state, outcome=None)
        gate = _build_gate(action, contract)
        decision = gate.evaluate(
            contract,
            now=NOW,
            operation_category=OperationCategory.RECONCILIATION,
            execution_binding=binding,
        )
        assert decision.allowed is False
        # Terminal states are caught by terminal check (prior to reconciliation check)
        assert decision.reason is GateCode.ACTION_TERMINAL


def test_b2_1_e_modern_reconciliation_semantics_preserved():
    """E. modern + existing reconciliation state -> semantics preserved."""
    binding = _make_binding(action_protocol="mc616d2-v1")
    contract = _make_contract()

    # Modern + RECONCILIATION_REQUIRED -> ALLOWED
    action1 = _StubAction(action_protocol="mc616d2-v1", state=LifecycleState.RECONCILIATION_REQUIRED)
    gate1 = _build_gate(action1, contract)
    d1 = gate1.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.RECONCILIATION,
        execution_binding=binding,
    )
    assert d1.allowed is True
    assert d1.reason is GateCode.ALLOWED
    assert d1.action_protocol == "mc616d2-v1"

    # Modern + UNKNOWN_OUTCOME -> ALLOWED
    action2 = _StubAction(action_protocol="mc616d2-v1", state=LifecycleState.LEASED, outcome="unknown_outcome")
    gate2 = _build_gate(action2, contract)
    d2 = gate2.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.RECONCILIATION,
        execution_binding=binding,
    )
    assert d2.allowed is True
    assert d2.reason is GateCode.ALLOWED

    # Modern + active state (no unknown outcome) under RECONCILIATION -> DENIED
    action3 = _StubAction(action_protocol="mc616d2-v1", state=LifecycleState.LEASED, outcome=None)
    gate3 = _build_gate(action3, contract)
    d3 = gate3.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.RECONCILIATION,
        execution_binding=binding,
    )
    assert d3.allowed is False
    assert d3.reason is GateCode.RECONCILIATION_INELIGIBLE


def test_b2_1_f_caller_cannot_turn_ordinary_legacy_into_reconciliation():
    """F. caller cannot turn an ordinary legacy action into reconciliation merely by changing operation_category."""
    # An attacker or buggy caller with an ordinary legacy action (e.g., leased) tries
    # to pass operation_category=OperationCategory.RECONCILIATION to bypass the mutation block.
    action = _StubAction(action_protocol="legacy-v1", state=LifecycleState.LEASED)
    binding = _make_binding(action_protocol="legacy-v1")
    contract = _make_contract()
    gate = _build_gate(action, contract)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.RECONCILIATION,
        execution_binding=binding,
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.RECONCILIATION_INELIGIBLE


def test_b2_1_g_protocol_binding_mismatch_remains_rejected_under_reconciliation():
    """G. protocol/binding mismatch remains rejected under reconciliation."""
    action = _StubAction(action_protocol="legacy-v1", state=LifecycleState.RECONCILIATION_REQUIRED)
    binding = _make_binding(action_protocol="mc616d2-v1")  # mismatch!
    contract = _make_contract()
    gate = _build_gate(action, contract)

    decision = gate.evaluate(
        contract,
        now=NOW,
        operation_category=OperationCategory.RECONCILIATION,
        execution_binding=binding,
    )
    assert decision.allowed is False
    assert decision.reason is GateCode.ACTION_PROTOCOL_MISMATCH
