from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from aipm.control_plane.approval import ApprovalService
from aipm.control_plane.audit import ActionAuditRepository
from aipm.control_plane.models import (
    ActionPlan,
    ActionRequest,
    ApprovalState,
    ControlPlaneError,
    EvidenceSource,
    EvidenceState,
    EvidenceSummary,
    OperationKind,
    PlanState,
    PlanningErrorCode,
    RiskLevel,
)
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import allowed_operations, validate_operation


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def request(**overrides):
    values = {
        "operation": OperationKind.UPDATE_PROJECT_PLAN,
        "target_id": "project-demo",
        "idempotency_key": "idem-001",
        "metadata": (("reason", "operator review"),),
    }
    values.update(overrides)
    return ActionRequest(**values)


def planner(*, now=NOW, targets=None):
    return PlanOnlyPlanner(clock=lambda: now, target_allow_list=targets or {"project-demo"})


def make_plan(**overrides):
    return planner(**overrides).plan(request())


def forged_plan(source, *, evidence_state=EvidenceState.OBSERVED, source_value=EvidenceSource.MISSION_CONTROL_OBSERVATION):
    issued = source.plan(request())
    draft = ActionPlan(
        plan_id=issued.plan_id,
        request=issued.request,
        risk=issued.risk,
        evidence=EvidenceSummary(evidence_state, (("status", "caller-forged"),)),
        evidence_source=source_value,
        expected_effect=issued.expected_effect,
        expires_at=issued.expires_at,
        created_at=issued.created_at,
        state=PlanState.PLANNED,
    )
    return replace(draft, digest=draft.computed_digest())


def test_action_request_is_closed_typed_and_canonical():
    value = request(metadata=(("zeta", "last"), ("alpha", "first")))
    assert value.operation is OperationKind.UPDATE_PROJECT_PLAN
    assert value.canonical() == '{"idempotency_key":"idem-001","metadata":[["alpha","first"],["zeta","last"]],"operation":"update_project_plan","target_id":"project-demo"}'
    assert set(allowed_operations()) == {OperationKind.UPDATE_PROJECT_PLAN}


def test_request_rejects_unsafe_fields_and_unbounded_values():
    for key in ("path", "command", "url", "token", "secret", "password", "provider"):
        with pytest.raises(ControlPlaneError, match="Unsafe request metadata"):
            request(metadata=((key, "value"),))
    with pytest.raises(ControlPlaneError):
        request(target_id="x" * 129)
    with pytest.raises(ControlPlaneError):
        request(idempotency_key="x" * 129)
    with pytest.raises(ControlPlaneError):
        request(metadata=tuple((f"key{i}", "v") for i in range(9)))


def test_request_rejects_control_characters_and_duplicate_metadata():
    with pytest.raises(ControlPlaneError):
        request(target_id="demo\npath")
    with pytest.raises(ControlPlaneError):
        request(metadata=(("reason", "one"), ("reason", "two")))


def test_equivalent_requests_have_identical_canonical_forms():
    left = request(metadata=(("b", "two"), ("a", "one")))
    right = request(metadata=(("a", "one"), ("b", "two")))
    assert left.canonical() == right.canonical()


def test_plan_is_low_risk_plan_only_and_deterministic():
    first = make_plan()
    second = make_plan()
    assert first.risk is RiskLevel.LOW
    assert first.plan_id == second.plan_id
    assert first.digest == second.digest
    assert first.digest == first.computed_digest()
    assert "No runtime effect" in first.expected_effect
    assert first.expires_at - first.created_at == timedelta(minutes=15)


def test_plan_digest_changes_for_security_relevant_fields():
    base = planner().plan(request())
    changed_target = planner(targets={"project-demo", "project-other"}).plan(request(target_id="project-other"))
    changed_idempotency = planner().plan(request(idempotency_key="idem-002"))
    assert base.digest != changed_target.digest
    assert base.digest != changed_idempotency.digest


def test_target_allow_list_and_unsupported_operation_fail_closed():
    with pytest.raises(ControlPlaneError) as error:
        planner(targets={"other-project"}).plan(request())
    assert error.value.code is PlanningErrorCode.UNAVAILABLE_TARGET
    with pytest.raises(ValueError):
        validate_operation(SimpleNamespace(value="arbitrary_operation"))


def test_caller_supplied_evidence_provider_is_rejected():
    with pytest.raises(TypeError):
        planner().plan(request(), evidence_provider=object())


def test_not_observed_evidence_is_explicit_and_safe():
    plan = planner().plan(request())
    assert plan.evidence.state is EvidenceState.NOT_OBSERVED
    assert plan.risk is RiskLevel.LOW


def test_plan_expiry_uses_injected_clock():
    plan = make_plan()
    assert not plan.is_expired(NOW + timedelta(minutes=14, seconds=59))
    assert plan.is_expired(NOW + timedelta(minutes=15))


def test_approval_intent_binds_plan_and_digest_without_claiming_authentication():
    source = planner()
    plan = source.plan(request())
    service = ApprovalService(clock=lambda: NOW + timedelta(minutes=1), target_allow_list=source.target_allow_list)
    binding = service.request(plan.request, actor_id="local-operator")
    expected = PlanOnlyPlanner(clock=lambda: NOW + timedelta(minutes=1), target_allow_list=source.target_allow_list).plan(plan.request)
    assert binding.state is ApprovalState.APPROVAL_REQUESTED
    assert binding.plan_id == expected.plan_id
    assert binding.plan_digest == expected.digest
    approved = service.approve(binding)
    assert approved.state is ApprovalState.APPROVED
    service.validate(approved)
    assert approved.expires_at <= plan.expires_at


def test_approval_mismatch_is_rejected():
    source = planner(targets={"project-demo", "project-other"})
    plan = source.plan(request())
    service = ApprovalService(clock=lambda: NOW + timedelta(minutes=1), target_allow_list=source.target_allow_list)
    binding = service.approve(service.request(plan.request, actor_id="operator"))
    changed = replace(binding, request=request(target_id="project-other"), plan_id="changed-plan", plan_digest="0" * 64)
    with pytest.raises(ControlPlaneError) as error:
        service.validate(changed)
    assert error.value.code is PlanningErrorCode.APPROVAL_MISMATCH


def test_approval_expires_with_plan_and_plan_expiry_is_rejected():
    source = planner()
    plan = source.plan(request())
    clock = [NOW + timedelta(minutes=1)]
    service = ApprovalService(clock=lambda: clock[0], target_allow_list=source.target_allow_list)
    binding = service.request(plan.request, actor_id="operator")
    clock[0] = NOW + timedelta(minutes=15)
    with pytest.raises(ControlPlaneError) as error:
        service.validate(binding)
    assert error.value.code is PlanningErrorCode.EXPIRED_PLAN


def test_approval_cannot_be_approved_twice_or_consumed_as_execution():
    source = planner()
    plan = source.plan(request())
    service = ApprovalService(clock=lambda: NOW + timedelta(minutes=1), target_allow_list=source.target_allow_list)
    binding = service.request(plan.request, actor_id="operator")
    approved = service.approve(binding)
    with pytest.raises(ControlPlaneError):
        service.approve(approved)
    assert approved.state is ApprovalState.APPROVED


def test_audit_records_are_safe_bounded_and_append_only():
    source = planner()
    plan = source.plan(request())
    service = ApprovalService(clock=lambda: NOW + timedelta(minutes=1), target_allow_list=source.target_allow_list)
    binding = service.request(plan.request, actor_id="operator")
    audit = ActionAuditRepository(target_allow_list=source.target_allow_list, max_records=3)
    audit.append_plan(plan.request, actor_id="system", now=NOW)
    audit.append_approval_requested(binding, now=NOW)
    audit.append_approved(service.approve(binding), now=NOW)
    assert [record.state.value for record in audit.records()] == ["planned", "approval_requested", "approved"]
    safe = audit.safe_records()
    assert all("path" not in row and "command" not in row and "secret" not in row for row in safe)
    with pytest.raises(ValueError, match="bound"):
        audit.append_plan(plan.request, now=NOW)


def test_audit_rejects_raw_sensitive_values_at_contract_boundary():
    source = planner()
    plan = source.plan(request())
    audit = ActionAuditRepository(target_allow_list=source.target_allow_list)
    with pytest.raises(ControlPlaneError):
        audit.append_plan(plan.request, actor_id="/home/ubuntu/.ssh/id_rsa", now=NOW)


def test_plan_only_package_has_no_execution_or_provider_boundary():
    root = Path("src/aipm/control_plane")
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    for forbidden in (
        "import subprocess",
        "os.system",
        "systemctl",
        "docker.",
        "compose.",
        "git.",
        "socket.",
        "requests.",
        "httpx.",
        "urllib.",
        "UpdateEngine",
        "NotificationRunner",
        "BackupEngine",
        "INSERT ",
        "UPDATE ",
        "DELETE ",
        "CREATE TABLE",
        "ALTER TABLE",
    ):
        assert forbidden not in source


def test_audit_does_not_touch_filesystem_or_database(tmp_path: Path):
    before = sorted(tmp_path.iterdir())
    source = planner()
    plan = source.plan(request())
    ActionAuditRepository(target_allow_list=source.target_allow_list).append_plan(plan.request, now=NOW)
    assert sorted(tmp_path.iterdir()) == before


def test_planner_requires_explicit_target_allow_list():
    with pytest.raises(ValueError, match="allow-list"):
        PlanOnlyPlanner(clock=lambda: NOW)


def test_forged_plan_digest_is_rejected():
    plan = make_plan()
    with pytest.raises(ControlPlaneError, match="digest"):
        replace(plan, digest="0" * 64)


def test_approval_binding_defaults_to_non_approved_intent():
    source = planner()
    plan = source.plan(request())
    service = ApprovalService(clock=lambda: NOW + timedelta(minutes=1), target_allow_list=source.target_allow_list)
    binding = service.request(plan.request, actor_id="operator")
    assert binding.state is ApprovalState.APPROVAL_REQUESTED


def test_caller_created_observed_evidence_is_not_authoritative():
    with pytest.raises(TypeError):
        planner().plan(request(), evidence=EvidenceSummary(EvidenceState.OBSERVED, (("health", "fresh"),)))


def test_no_producer_defaults_to_not_observed_and_no_state_is_upgraded():
    plan = planner().plan(request())
    assert plan.evidence.state is EvidenceState.NOT_OBSERVED
    with pytest.raises(TypeError):
        planner().plan(request(), evidence=EvidenceSummary(EvidenceState.OBSERVED, (("health", "fresh"),)))


def test_unicode_nfc_and_decomposed_forms_have_same_canonical_and_digest():
    nfc = request(metadata=(("reason", "é"),))
    decomposed = request(metadata=(("reason", "e\u0301"),))
    assert nfc.canonical() == decomposed.canonical()
    first = planner().plan(nfc)
    second = planner().plan(decomposed)
    assert first.digest == second.digest


def test_different_unicode_content_and_bounds_remain_distinct_and_bounded():
    assert request(metadata=(("reason", "é"),)).canonical() != request(metadata=(("reason", "ê"),)).canonical()
    with pytest.raises(ControlPlaneError):
        request(metadata=(("reason", "é" * 129),))
    with pytest.raises(ControlPlaneError):
        request(target_id="é-project")


def test_request_identity_is_distinct_from_concrete_plan_identity():
    same_request_now = planner(now=NOW).plan(request())
    later = planner(now=NOW + timedelta(minutes=1)).plan(request())
    changed_metadata = planner(now=NOW).plan(request(metadata=(("reason", "changed"),)))
    assert same_request_now.request_identity == later.request_identity
    assert same_request_now.plan_id != later.plan_id
    assert same_request_now.plan_id != changed_metadata.plan_id
    assert same_request_now.digest != later.digest
    assert same_request_now.digest != changed_metadata.digest


def test_audit_action_ids_distinguish_concrete_plan_instances():
    source = planner(now=NOW)
    first = source.plan(request())
    later = source.plan(request(idempotency_key="idem-002"))
    audit = ActionAuditRepository(target_allow_list=source.target_allow_list)
    first_record = audit.append_plan(first.request, now=NOW)
    later_record = audit.append_plan(later.request, now=NOW + timedelta(minutes=1))
    assert first_record.action_id != later_record.action_id
    assert first_record.plan_digest != later_record.plan_digest


def test_approval_is_consumed_once_and_consumed_binding_cannot_validate():
    source = planner()
    plan = source.plan(request())
    service = ApprovalService(clock=lambda: NOW + timedelta(minutes=1), target_allow_list=source.target_allow_list)
    approved = service.approve(service.request(plan.request, actor_id="operator"))
    consumed = service.consume(approved)
    assert consumed.state is ApprovalState.CONSUMED
    with pytest.raises(ControlPlaneError):
        service.consume(approved)
    with pytest.raises(ControlPlaneError):
        service.validate(consumed)


def test_approval_request_replay_is_rejected_by_store():
    source = planner()
    plan = source.plan(request())
    service = ApprovalService(clock=lambda: NOW + timedelta(minutes=1), target_allow_list=source.target_allow_list)
    binding = service.request(plan.request, actor_id="operator")
    with pytest.raises(ControlPlaneError):
        service.request(plan.request, actor_id="operator")
    assert service.store.get(binding.approval_id) == binding


def test_no_public_trusted_evidence_factory_or_producer_exists():
    import importlib
    from aipm.control_plane import models
    assert not hasattr(models, "TrustedPlanningEvidence")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("aipm.control_plane.evidence")
    source = planner()
    assert not hasattr(source, "_issued_plans")
    with pytest.raises(TypeError):
        vars(source)


def test_direct_action_plan_is_not_an_approval_or_audit_input():
    source = planner()
    forged = forged_plan(source)
    with pytest.raises((TypeError, ControlPlaneError)):
        ApprovalService(clock=lambda: NOW, target_allow_list=source.target_allow_list).request(forged, actor_id="operator")
    with pytest.raises((TypeError, ControlPlaneError)):
        ActionAuditRepository(target_allow_list=source.target_allow_list).append_plan(forged, now=NOW)


def test_direct_not_observed_action_plan_is_not_an_approval_or_audit_input():
    source = planner()
    forged = forged_plan(source, evidence_state=EvidenceState.NOT_OBSERVED, source_value=EvidenceSource.NONE)
    with pytest.raises((TypeError, ControlPlaneError)):
        ApprovalService(clock=lambda: NOW, target_allow_list=source.target_allow_list).request(forged, actor_id="operator")
    with pytest.raises((TypeError, ControlPlaneError)):
        ActionAuditRepository(target_allow_list=source.target_allow_list).append_plan(forged, now=NOW)


def test_copy_and_reconstruction_are_not_approval_or_audit_inputs():
    source = planner()
    issued = source.plan(request())
    candidates = (replace(issued), copy.copy(issued), copy.deepcopy(issued))
    for candidate in candidates:
        with pytest.raises((TypeError, ControlPlaneError)):
            ApprovalService(clock=lambda: NOW, target_allow_list=source.target_allow_list).request(candidate, actor_id="operator")
        with pytest.raises((TypeError, ControlPlaneError)):
            ActionAuditRepository(target_allow_list=source.target_allow_list).append_plan(candidate, now=NOW)


def test_action_plan_subclasses_are_not_approval_or_audit_inputs():
    source = planner()
    issued = source.plan(request())

    class ForgedActionPlan(ActionPlan):
        pass

    forged = ForgedActionPlan(
        plan_id=issued.plan_id,
        request=issued.request,
        risk=issued.risk,
        evidence=issued.evidence,
        evidence_source=issued.evidence_source,
        expected_effect=issued.expected_effect,
        expires_at=issued.expires_at,
        created_at=issued.created_at,
        state=issued.state,
        digest=issued.digest,
    )
    with pytest.raises((TypeError, ControlPlaneError)):
        ApprovalService(clock=lambda: NOW, target_allow_list=source.target_allow_list).request(forged, actor_id="operator")
    with pytest.raises((TypeError, ControlPlaneError)):
        ActionAuditRepository(target_allow_list=source.target_allow_list).append_plan(forged, now=NOW)


def test_service_owned_planning_accepts_request_and_audit_records_not_observed():
    source = planner()
    request_value = request()
    service = ApprovalService(clock=lambda: NOW, target_allow_list=source.target_allow_list)
    binding = service.request(request_value, actor_id="operator")
    record = ActionAuditRepository(target_allow_list=source.target_allow_list).append_plan(request_value, now=NOW)
    assert binding.request == request_value
    assert record.evidence_state is EvidenceState.NOT_OBSERVED
    assert record.operation is OperationKind.UPDATE_PROJECT_PLAN


def test_planner_injection_constructor_is_not_available():
    source = planner()
    with pytest.raises(TypeError):
        ApprovalService(planner=source)
    with pytest.raises(TypeError):
        ActionAuditRepository(planner=source)


def test_services_expose_no_mutable_planner_dependency():
    source = planner()
    approval = ApprovalService(clock=lambda: NOW, target_allow_list=source.target_allow_list)
    audit = ActionAuditRepository(target_allow_list=source.target_allow_list)
    assert not hasattr(approval, "planner")
    assert not hasattr(audit, "planner")
    with pytest.raises(TypeError):
        vars(approval)
    with pytest.raises(TypeError):
        vars(audit)


def test_external_planner_behavior_mutation_cannot_redirect_service_planning():
    source = planner()
    observed = forged_plan(source)
    with pytest.raises(TypeError):
        source.plan = lambda _request: observed
    approval = ApprovalService(clock=lambda: NOW, target_allow_list=source.target_allow_list)
    audit = ActionAuditRepository(target_allow_list=source.target_allow_list)
    binding = approval.request(request(idempotency_key="after-instance-mutation"), actor_id="operator")
    record = audit.append_plan(request(idempotency_key="after-instance-mutation-audit"), now=NOW)
    assert binding.request.idempotency_key == "after-instance-mutation"
    assert record.evidence_state is EvidenceState.NOT_OBSERVED
    assert record.outcome_code == "plan_created"


def test_class_level_plan_replacement_cannot_redirect_service_planning():
    source = planner()
    observed = forged_plan(source)
    original = PlanOnlyPlanner.plan
    malicious = lambda _self, _request: observed
    try:
        type.__setattr__(PlanOnlyPlanner, "plan", malicious)
        with pytest.raises(TypeError):
            PlanOnlyPlanner.plan = original
        approval = ApprovalService(clock=lambda: NOW, target_allow_list=source.target_allow_list)
        audit = ActionAuditRepository(target_allow_list=source.target_allow_list)
        binding = approval.request(request(idempotency_key="class-mutation"), actor_id="operator")
        record = audit.append_plan(request(idempotency_key="class-mutation-audit"), now=NOW)
        assert binding.request.idempotency_key == "class-mutation"
        assert record.evidence_state is EvidenceState.NOT_OBSERVED
    finally:
        type.__setattr__(PlanOnlyPlanner, "plan", original)


def test_planner_configuration_is_immutable_and_bounded():
    source = planner()
    for name, value in (("_clock", lambda: NOW + timedelta(days=1)), ("_target_allow_list", frozenset({"attacker"}))):
        with pytest.raises(TypeError):
            setattr(source, name, value)
    assert source.target_allow_list == frozenset({"project-demo"})


def test_service_instance_behavior_replacement_is_rejected():
    approval = ApprovalService(clock=lambda: NOW, target_allow_list={"project-demo"})
    audit = ActionAuditRepository(target_allow_list={"project-demo"})
    malicious = lambda _request: forged_plan(planner())
    for obj in (approval, audit):
        for setter in (
            lambda obj=obj: setattr(obj, "plan", malicious),
            lambda obj=obj: object.__setattr__(obj, "plan", malicious),
            lambda obj=obj: setattr(obj, "_planner", malicious),
            lambda obj=obj: object.__setattr__(obj, "_planner", malicious),
        ):
            with pytest.raises(AttributeError):
                setter()


def test_module_builder_replacement_cannot_redirect_services():
    from aipm.control_plane import audit as audit_module
    from aipm.control_plane import approval as approval_module
    from aipm.control_plane import planner as planner_module

    source = planner()
    observed = forged_plan(source)
    original = planner_module._build_evidence_neutral_plan

    def malicious(*_args, **_kwargs):
        return observed

    try:
        planner_module._build_evidence_neutral_plan = malicious
        assert "_build_evidence_neutral_plan" not in vars(approval_module)
        assert "_build_evidence_neutral_plan" not in vars(audit_module)
        service = ApprovalService(clock=lambda: NOW, target_allow_list=source.target_allow_list)
        audit = ActionAuditRepository(clock=lambda: NOW, target_allow_list=source.target_allow_list)
        binding = service.request(request(idempotency_key="module-replacement"), actor_id="operator")
        record = audit.append_plan(request(idempotency_key="module-replacement-audit"), now=NOW)
        assert binding.request.idempotency_key == "module-replacement"
        assert record.evidence_state is EvidenceState.NOT_OBSERVED
    finally:
        planner_module._build_evidence_neutral_plan = original


def test_service_modules_have_no_mutable_global_plan_construction_helpers():
    from aipm.control_plane import approval as approval_module
    from aipm.control_plane import audit as audit_module

    forbidden_globals = {
        "replace",
        "hashlib",
        "json",
        "datetime",
        "timezone",
        "timedelta",
        "ActionRequest",
        "ActionPlan",
        "EvidenceSummary",
        "EvidenceState",
        "EvidenceSource",
        "OperationKind",
        "RiskLevel",
        "risk_for",
        "validate_operation",
        "_build_evidence_neutral_plan",
    }
    assert forbidden_globals.isdisjoint(vars(approval_module))
    assert forbidden_globals.isdisjoint(vars(audit_module))


def test_replaced_model_constructors_cannot_inject_observed_evidence(monkeypatch):
    from aipm.control_plane import models as models_module

    class ObservedRecord:
        evidence_state = EvidenceState.OBSERVED

    monkeypatch.setattr(models_module, "EvidenceSummary", lambda *_args, **_kwargs: EvidenceSummary(EvidenceState.OBSERVED))
    monkeypatch.setattr(models_module, "ActionPlan", lambda *_args, **_kwargs: ObservedRecord())
    approval = ApprovalService(clock=lambda: NOW, target_allow_list={"project-demo"})
    audit = ActionAuditRepository(clock=lambda: NOW, target_allow_list={"project-demo"})
    binding = approval.request(request(idempotency_key="model-replacement-approval"), actor_id="operator")
    assert binding.plan_digest
    record = audit.append_plan(request(idempotency_key="model-replacement-audit"), now=NOW)
    assert record.evidence_state is EvidenceState.NOT_OBSERVED

    monkeypatch.setattr(models_module, "ActionAuditRecord", lambda **_kwargs: ObservedRecord())
    with pytest.raises(ControlPlaneError, match="evidence"):
        audit.append_plan(request(idempotency_key="record-replacement"), now=NOW)
    assert not audit.records()[-1].evidence_state is EvidenceState.OBSERVED


def test_service_class_mutation_is_rejected_or_fails_closed():
    approval_methods = ("request", "approve", "validate", "consume")
    audit_methods = ("append_plan", "append_approval_requested", "append_approved", "_append")
    for cls, names in ((ApprovalService, approval_methods), (ActionAuditRepository, audit_methods)):
        originals = {name: getattr(cls, name) for name in names}
        try:
            for name in names:
                with pytest.raises(TypeError):
                    setattr(cls, name, lambda *_args, **_kwargs: forged_plan(planner()))
                type.__delattr__(cls, name)
                with pytest.raises(AttributeError):
                    getattr(cls(clock=lambda: NOW, target_allow_list={"project-demo"}), name)
                type.__setattr__(cls, name, originals[name])
        finally:
            for name, method in originals.items():
                type.__setattr__(cls, name, method)




def test_plan_id_alignment_across_planner_approval_and_audit():
    import hashlib
    import json

    plan = planner(now=NOW).plan(request())
    approval = ApprovalService(clock=lambda: NOW, target_allow_list={"project-demo"})
    binding = approval.request(request(), actor_id="operator")
    audit_record = ActionAuditRepository(target_allow_list={"project-demo"}).append_plan(request(), now=NOW)

    assert binding.plan_id == plan.plan_id
    assert audit_record.plan_id == plan.plan_id
    assert binding.plan_digest == plan.digest
    assert audit_record.plan_digest == plan.digest

    expected_payload = {
        "request_identity": plan.request_identity,
        "operation": plan.request.operation.value,
        "target_id": plan.request.target_id,
        "evidence_source": plan.evidence_source.value,
        "evidence_state": plan.evidence.state.value,
        "evidence": plan.evidence.canonical(),
        "risk": plan.risk.value,
        "expected_effect": plan.expected_effect,
        "created_at": plan.created_at.isoformat(),
        "expires_at": plan.expires_at.isoformat(),
        "state": plan.state.value,
    }
    expected_id = hashlib.sha256(
        json.dumps(expected_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]
    assert plan.plan_id == expected_id


def test_plan_id_historical_evidence_field_regression():
    plan = planner(now=NOW).plan(request())
    approval = ApprovalService(clock=lambda: NOW, target_allow_list={"project-demo"})
    binding = approval.request(request(), actor_id="operator")

    assert plan.plan_id == binding.plan_id
    assert plan.digest == binding.plan_digest
    assert plan.evidence.state is EvidenceState.NOT_OBSERVED
    assert plan.evidence.canonical() == []


def test_plan_id_identity_fields_are_all_bound():
    import hashlib
    import json

    plan = planner(now=NOW).plan(request())
    base = {
        "request_identity": plan.request_identity,
        "operation": plan.request.operation.value,
        "target_id": plan.request.target_id,
        "evidence_source": plan.evidence_source.value,
        "evidence_state": plan.evidence.state.value,
        "evidence": plan.evidence.canonical(),
        "risk": plan.risk.value,
        "expected_effect": plan.expected_effect,
        "created_at": plan.created_at.isoformat(),
        "expires_at": plan.expires_at.isoformat(),
        "state": plan.state.value,
    }

    def plan_id(payload):
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]

    original = plan_id(base)
    mutations = {
        "request_identity": "f" * 64,
        "operation": "other_operation",
        "target_id": "project-other",
        "evidence_source": EvidenceSource.MISSION_CONTROL_OBSERVATION.value,
        "evidence_state": EvidenceState.STALE.value,
        "evidence": [["status", "fresh"]],
        "risk": RiskLevel.MEDIUM.value,
        "expected_effect": "different bounded effect",
        "created_at": "2026-08-20T12:00:01+00:00",
        "expires_at": "2026-08-20T12:15:01+00:00",
        "state": PlanState.INVALID.value,
    }
    for field, value in mutations.items():
        changed = dict(base)
        changed[field] = value
        assert plan_id(changed) != original, field


def test_plan_id_and_digest_are_distinct_and_canonicalization_is_exact():
    import json
    import re

    plan = planner(now=NOW).plan(request(metadata=(("reason", "e\u0301"),)))
    assert re.fullmatch(r"[0-9a-f]{32}", plan.plan_id)
    assert re.fullmatch(r"[0-9a-f]{64}", plan.digest)
    assert plan.plan_id != plan.digest
    assert plan.evidence.canonical() == []
    canonical = json.dumps(plan.canonical_payload(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert canonical.encode("utf-8") == plan.canonical().encode("utf-8")
    assert "\\u00e9" not in canonical
    assert plan.created_at.isoformat() in canonical
    assert plan.expires_at.isoformat() in canonical
    assert set(plan.canonical_payload()) == {
        "plan_id",
        "request",
        "risk",
        "evidence_source",
        "evidence_state",
        "evidence",
        "expected_effect",
        "created_at",
        "expires_at",
        "state",
    }


def test_evidence_items_are_deterministically_ordered_before_plan_identity():
    left = EvidenceSummary(EvidenceState.NOT_OBSERVED, (("zeta", "last"), ("alpha", "first")))
    right = EvidenceSummary(EvidenceState.NOT_OBSERVED, (("alpha", "first"), ("zeta", "last")))
    assert left.canonical() == right.canonical()
    assert left.canonical() == [["alpha", "first"], ["zeta", "last"]]
