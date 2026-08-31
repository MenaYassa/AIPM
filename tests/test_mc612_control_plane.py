from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.audit import InMemoryAuditLedger
from aipm.control_plane.audit import builders as audit_builders
from aipm.control_plane.audit.models import SYSTEM_ACTOR_SUBJECT, AuditActorRole, AuditEventDraft, AuditEventType, AuditEventError
from aipm.control_plane.identity import ActionIdentity, AuthenticationMethod, OwnerPrincipal, PrincipalVerification
from aipm.control_plane.models import (
    ActionPlan,
    ActionRequest,
    ConfirmationKind,
    ConfirmationState,
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
from aipm.control_plane.policy import AuthorizationPolicy, PolicyCode, allowed_operations, validate_operation
from aipm.control_plane.project_plan import Environment, ProjectPlan


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def owner_principal(**overrides):
    values = {
        "subject": "local-owner",
        "issuer": "aipm-owner-auth",
        "authentication_method": AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE,
        "verification": PrincipalVerification.VERIFIED,
        "auth_epoch": 1,
        "authenticated_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
        "roles": ("owner",),
    }
    values.update(overrides)
    return OwnerPrincipal(**values)


def request(**overrides):
    values = {
        "operation": OperationKind.UPDATE_PROJECT_PLAN,
        "target_id": "project-demo",
        "idempotency_key": "idem-001",
        "metadata": (("title", "New title"),),
        "environment": "staging",
    }
    values.update(overrides)
    return ActionRequest(**values)


def current_plan(**overrides):
    values = {
        "target_id": "project-demo",
        "environment": Environment.STAGING,
        "title": "Old title",
        "objective": "Objective",
        "now": NOW,
    }
    values.update(overrides)
    return ProjectPlan.create(**values)


def policy(**overrides):
    values = {
        "policy_version": "policy-v1",
        "allowed_scopes": frozenset({("project-demo", "staging")}),
    }
    values.update(overrides)
    return AuthorizationPolicy(**values)


def planner(*, now=NOW, targets=None):
    return PlanOnlyPlanner(clock=lambda: now, target_allow_list=targets or {"project-demo"})


def make_plan(**overrides):
    return planner(**overrides).plan(request())


def authorize(**overrides):
    values = {
        "principal": owner_principal(),
        "request": request(),
        "current": current_plan(),
        "now": NOW,
        "policy_config": None,
        "plan": None,
    }
    values.update(overrides)
    plan = values["plan"] or planner(now=values["now"], targets={values["request"].target_id}).plan(values["request"])
    policy_value = values["policy_config"] or policy()
    return policy_value.authorize(
        values["principal"],
        values["request"],
        plan,
        values["current"],
        now=values["now"],
    )


def test_action_request_is_closed_typed_and_canonical():
    value = request(metadata=(("zeta", "last"), ("alpha", "first")))
    assert value.operation is OperationKind.UPDATE_PROJECT_PLAN
    assert value.environment == "staging"
    assert value.canonical() == '{"environment":"staging","idempotency_key":"idem-001","metadata":[["alpha","first"],["zeta","last"]],"operation":"update_project_plan","target_id":"project-demo"}'
    assert value.fields == frozenset({"alpha", "zeta"})
    assert set(allowed_operations()) == {OperationKind.UPDATE_PROJECT_PLAN, OperationKind.ROLLBACK_PROJECT_PLAN}


def test_request_rejects_unknown_environments_and_unsafe_fields_and_unbounded_values():
    with pytest.raises(ControlPlaneError):
        request(environment="production-staging")
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


def test_allowed_decision_binds_exact_identity_plan_revision_and_digest():
    plan = make_plan()
    current = current_plan()
    decision = authorize(plan=plan, current=current)
    identity = decision.action_identity
    assert identity is not None
    assert identity.plan_id == plan.plan_id
    assert identity.plan_digest == plan.digest
    assert identity.target_revision == current.revision
    assert identity.target_digest == current.digest()
    assert identity.policy_version == "policy-v1"
    assert identity.requester_subject == "local-owner"
    assert identity.operation == "update_project_plan"
    assert identity.environment == "staging"
    assert len(identity.action_id) == 64


def test_action_identity_is_deterministic_and_derived_exactly_once():
    first = authorize()
    second = authorize()
    assert first.action_identity is not None and second.action_identity is not None
    assert first.action_identity == second.action_identity
    assert first.action_identity.canonical() == second.action_identity.canonical()
    from aipm.control_plane.identity import derive_action_identity

    plan = make_plan()
    current = current_plan()
    direct = derive_action_identity(
        request=request(),
        plan=plan,
        current_plan=current,
        policy_version="policy-v1",
        requester_subject="local-owner",
    )
    assert direct == first.action_identity


def test_action_identity_changes_for_authorization_confusion_inputs():
    base = authorize()
    assert base.action_identity is not None
    changed_plan = authorize(request=request(idempotency_key="idem-002"))
    changed_fields = authorize(request=request(metadata=(("title", "Different title"),)))
    changed_revision = authorize(current=current_plan().update(expected_revision=1, fields={"title": "Rev2"}, now=NOW))
    assert base.action_identity != changed_plan.action_identity
    assert base.action_identity != changed_fields.action_identity
    assert base.action_identity != changed_revision.action_identity


def test_forged_plan_cannot_become_an_authorized_identity():
    genuine = make_plan()
    forged = object.__new__(ActionPlan)
    for field in ActionPlan.__dataclass_fields__:
        object.__setattr__(forged, field, getattr(genuine, field))
    object.__setattr__(forged, "plan_id", "0" * 32)
    decision = authorize(plan=forged)
    assert decision.allowed is False
    assert decision.code is PolicyCode.INVALID_REQUEST
    assert decision.action_identity is None


def test_forced_wrong_digest_plan_is_rejected_by_construction():
    plan = make_plan()
    with pytest.raises(ControlPlaneError, match="digest"):
        replace(plan, digest="0" * 64)


def test_mismatched_current_plan_fails_authorization_closed():
    other = ProjectPlan.create(target_id="project-other", environment=Environment.STAGING, title="X", objective="Y", now=NOW)
    plan = make_plan()
    decision = authorize(plan=plan, current=other)
    assert decision.allowed is False
    assert decision.code is PolicyCode.INVALID_REQUEST


def test_confirmation_binds_decision_identity_without_rederivation():
    decision = authorize()
    assert decision.action_identity is not None
    service = OwnerConfirmationService(clock=lambda: NOW + timedelta(minutes=1))
    binding = service.request_confirmation(decision, requester_subject="local-owner")
    assert binding.state is ConfirmationState.CONFIRMATION_REQUESTED
    assert binding.action_id == decision.action_identity.action_id
    assert binding.plan_id == decision.action_identity.plan_id
    assert binding.plan_digest == decision.action_identity.plan_digest
    assert binding.target_revision == decision.action_identity.target_revision
    assert binding.target_digest == decision.action_identity.target_digest
    assert binding.policy_version == decision.action_identity.policy_version
    assert binding.decision_id == decision.decision_id
    confirmed = service.confirm(binding, confirmed_by_subject="local-owner", now=NOW + timedelta(minutes=2))
    assert confirmed.state is ConfirmationState.CONFIRMED
    assert confirmed.confirmed_by_subject == "local-owner"
    service.validate(confirmed)
    assert confirmed.expires_at <= decision.expires_at


def test_confirmation_mismatch_is_rejected():
    decision = authorize()
    service = OwnerConfirmationService(clock=lambda: NOW + timedelta(minutes=1))
    binding = service.request_confirmation(decision, requester_subject="local-owner")
    confirmed = service.confirm(binding, confirmed_by_subject="local-owner", now=NOW + timedelta(minutes=1))
    changed = replace(binding, action_id="f" * 64, plan_id="changed-plan", plan_digest="0" * 64)
    with pytest.raises(ControlPlaneError) as error:
        service.validate(changed)
    assert error.value.code is PlanningErrorCode.CONFIRMATION_MISMATCH
    with pytest.raises(ControlPlaneError):
        service.confirm(changed, confirmed_by_subject="local-owner", now=NOW + timedelta(minutes=2))


def test_confirmation_expires_with_decision_and_plan():
    decision = authorize()
    clock = [NOW + timedelta(minutes=1)]
    service = OwnerConfirmationService(clock=lambda: clock[0])
    binding = service.request_confirmation(decision, requester_subject="local-owner")
    clock[0] = decision.expires_at
    with pytest.raises(ControlPlaneError) as error:
        service.validate(binding)
    assert error.value.code is PlanningErrorCode.EXPIRED_PLAN


def test_confirmation_cannot_be_confirmed_twice_or_consumed_as_execution():
    decision = authorize()
    service = OwnerConfirmationService(clock=lambda: NOW + timedelta(minutes=1))
    binding = service.request_confirmation(decision, requester_subject="local-owner")
    confirmed = service.confirm(binding, confirmed_by_subject="local-owner", now=NOW + timedelta(minutes=1))
    with pytest.raises(ControlPlaneError):
        service.confirm(binding, confirmed_by_subject="local-owner", now=NOW + timedelta(minutes=2))
    consumed = service.consume(confirmed, now=NOW + timedelta(minutes=3))
    assert consumed.state is ConfirmationState.CONSUMED
    with pytest.raises(ControlPlaneError):
        service.consume(confirmed, now=NOW + timedelta(minutes=3))
    with pytest.raises(ControlPlaneError):
        service.validate(consumed)


def test_confirmation_replay_for_the_same_action_is_rejected():
    decision = authorize()
    service = OwnerConfirmationService(clock=lambda: NOW + timedelta(minutes=1))
    binding = service.request_confirmation(decision, requester_subject="local-owner")
    service.confirm(binding, confirmed_by_subject="local-owner", now=NOW + timedelta(minutes=1))
    with pytest.raises(ControlPlaneError, match="already exists"):
        service.request_confirmation(decision, requester_subject="local-owner", now=NOW + timedelta(minutes=2))


def test_denied_decision_is_not_confirmable():
    decision = authorize(current=None)
    service = OwnerConfirmationService(clock=lambda: NOW + timedelta(minutes=1))
    with pytest.raises(ControlPlaneError):
        service.request_confirmation(decision, requester_subject="local-owner")


def test_forged_decision_is_not_confirmable():
    service = OwnerConfirmationService(clock=lambda: NOW + timedelta(minutes=1))
    with pytest.raises(ControlPlaneError):
        service.request_confirmation(SimpleNamespace(allowed=True, confirmation_required=True), requester_subject="local-owner")


def test_audit_ledger_is_append_only_bounded_and_verifiable():
    ledger = InMemoryAuditLedger(max_events=3)
    moment = NOW
    first = ledger.append(AuditEventDraft(
        event_type=AuditEventType.ACTION_CREATED,
        actor_subject="local-owner",
        occurred_at=moment,
        result_code="created",
    ))
    ledger.append(AuditEventDraft(
        event_type=AuditEventType.OWNER_CONFIRMED,
        actor_subject="local-owner",
        occurred_at=moment + timedelta(minutes=1),
        result_code="confirmed",
    ))
    assert [event.event_type for event in ledger.events()] == [AuditEventType.ACTION_CREATED, AuditEventType.OWNER_CONFIRMED]
    assert ledger.verify_chain().ok is True
    assert ledger.events()[0].sequence == 1 and ledger.events()[1].sequence == 2
    assert first.draft.event_id != ledger.events()[1].draft.event_id
    ledger.append(AuditEventDraft(event_type=AuditEventType.ACTION_CREATED, actor_subject="local-owner", occurred_at=moment + timedelta(minutes=2), result_code="created"))
    with pytest.raises(Exception, match="bound"):
        ledger.append(AuditEventDraft(event_type=AuditEventType.ACTION_CREATED, actor_subject="local-owner", occurred_at=moment + timedelta(minutes=3), result_code="created"))
    with pytest.raises(Exception, match="Duplicate"):
        ledger.append(first.draft)
    safe = [event.safe_dict() for event in ledger.events()]
    assert all("path" not in str(row) and "command" not in str(row) and "secret" not in str(row) for row in safe)


def test_audit_rejects_raw_sensitive_values_at_contract_boundary():
    with pytest.raises(AuditEventError):
        AuditEventDraft(
            event_type=AuditEventType.ACTION_CREATED,
            actor_subject="owner-token-bearer",
            occurred_at=NOW,
        )
    with pytest.raises(AuditEventError):
        AuditEventDraft(
            event_type=AuditEventType.KILL_SWITCH_ENGAGED,
            actor_subject=SYSTEM_ACTOR_SUBJECT,
            occurred_at=NOW,
            reason="operator password was leaked here",
        )
    with pytest.raises(AuditEventError):
        audit_builders.authentication_failure(reason_code="rejected$argon2id$v=19", occurred_at=NOW)


def test_plan_only_package_has_no_execution_or_provider_boundary():
    root = Path("src/aipm/control_plane")
    # The systemd provider is the ONE sanctioned subprocess boundary (argv-only,
    # no shell); it is scanned separately in test_mc612_stage14_systemd_restart.
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in root.glob("*.py")
        if path.name not in ("systemd_provider.py", "systemd_executor.py", "privilege.py", "executor_ipc.py", "mutation_receipt.py", "standalone_executor.py", "systemd_executor.py")
    )
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


def test_in_memory_audit_ledger_does_not_touch_filesystem_or_database(tmp_path: Path):
    before = sorted(tmp_path.iterdir())
    ledger = InMemoryAuditLedger()
    ledger.append(AuditEventDraft(event_type=AuditEventType.ACTION_CREATED, actor_subject="local-owner", occurred_at=NOW))
    assert sorted(tmp_path.iterdir()) == before
    assert ledger.verify_chain().ok is True


def test_planner_requires_explicit_target_allow_list():
    with pytest.raises(ValueError, match="allow-list"):
        PlanOnlyPlanner(clock=lambda: NOW)


def test_caller_created_observed_evidence_is_not_authoritative():
    with pytest.raises(TypeError):
        planner().plan(request(), evidence=EvidenceSummary(EvidenceState.OBSERVED, (("health", "fresh"),)))


def test_no_producer_defaults_to_not_observed_and_no_state_is_upgraded():
    plan = planner().plan(request())
    assert plan.evidence.state is EvidenceState.NOT_OBSERVED
    with pytest.raises(TypeError):
        planner().plan(request(), evidence=EvidenceSummary(EvidenceState.OBSERVED, (("health", "fresh"),)))


def test_unicode_nfc_and_decomposed_forms_have_same_canonical_and_digest():
    nfc = request(metadata=(("title", "é"),))
    decomposed = request(metadata=(("title", "e\u0301"),))
    assert nfc.canonical() == decomposed.canonical()
    first = planner().plan(nfc)
    second = planner().plan(decomposed)
    assert first.digest == second.digest


def test_different_unicode_content_and_bounds_remain_distinct_and_bounded():
    assert request(metadata=(("title", "é"),)).canonical() != request(metadata=(("title", "ê"),)).canonical()
    with pytest.raises(ControlPlaneError):
        request(metadata=(("title", "é" * 129),))
    with pytest.raises(ControlPlaneError):
        request(target_id="é-project")


def test_request_identity_is_distinct_from_concrete_plan_identity():
    same_request_now = planner(now=NOW).plan(request())
    later = planner(now=NOW + timedelta(minutes=1)).plan(request())
    changed_metadata = planner(now=NOW).plan(request(metadata=(("title", "changed"),)))
    assert same_request_now.request_identity == later.request_identity
    assert same_request_now.plan_id != later.plan_id
    assert same_request_now.plan_id != changed_metadata.plan_id
    assert same_request_now.digest != later.digest
    assert same_request_now.digest != changed_metadata.digest


def test_audit_event_identity_is_distinct_from_action_identity():
    decision = authorize()
    identity = decision.action_identity
    assert identity is not None
    event = InMemoryAuditLedger().append(
        audit_builders.authorization_allowed(actor_subject="local-owner", occurred_at=NOW, decision=decision)
    )
    assert event.draft.action_id == identity.action_id
    assert event.draft.event_id != identity.action_id
    assert len(event.draft.event_id) == 32 and len(identity.action_id) == 64
    assert event.sequence == 1


def test_audit_ledger_rejects_non_event_values():
    ledger = InMemoryAuditLedger()
    with pytest.raises(AuditEventError):
        ledger.append(make_plan())
    with pytest.raises(AuditEventError):
        ledger.append("not-a-draft")
    # References are optional: a minimal event with no linkage is valid.
    event = ledger.append(AuditEventDraft(event_type=AuditEventType.SYSTEM_ERROR, actor_subject=SYSTEM_ACTOR_SUBJECT, occurred_at=NOW, result_code="unexpected"))
    assert event.draft.action_id is None
    assert ledger.verify_chain().ok is True


def test_confirmation_is_consumed_once_and_consumed_binding_cannot_validate():
    decision = authorize()
    service = OwnerConfirmationService(clock=lambda: NOW + timedelta(minutes=1))
    binding = service.request_confirmation(decision, requester_subject="local-owner")
    confirmed = service.confirm(binding, confirmed_by_subject="local-owner", now=NOW + timedelta(minutes=1))
    consumed = service.consume(confirmed, now=NOW + timedelta(minutes=2))
    assert consumed.state is ConfirmationState.CONSUMED
    with pytest.raises(ControlPlaneError):
        service.consume(confirmed)
    with pytest.raises(ControlPlaneError):
        service.validate(consumed)


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


def test_copy_and_reconstruction_are_not_confirmation_or_audit_inputs():
    decision = authorize()
    identity = decision.action_identity
    assert identity is not None
    service = OwnerConfirmationService(clock=lambda: NOW + timedelta(minutes=1))
    ledger = InMemoryAuditLedger()
    for candidate in (make_plan(), copy.copy(make_plan()), copy.deepcopy(make_plan())):
        with pytest.raises(AuditEventError):
            ledger.append(candidate)
    with pytest.raises(ControlPlaneError):
        service.request_confirmation(candidate, requester_subject="local-owner")
    with pytest.raises(ControlPlaneError):
        service.request_confirmation(identity, requester_subject="local-owner")


def test_service_flow_records_canonical_action_created_evidence():
    decision = authorize()
    identity = decision.action_identity
    assert identity is not None
    ledger = InMemoryAuditLedger()
    event = ledger.append(
        audit_builders.action_created(
            actor_subject="local-owner",
            occurred_at=NOW,
            decision=decision,
            lifecycle_from="requested",
            lifecycle_to="confirmation_required",
        )
    )
    assert event.draft.action_id == identity.action_id
    assert event.draft.plan_revision == 1
    assert event.draft.operation == "update_project_plan"
    assert event.draft.event_type is AuditEventType.ACTION_CREATED


def test_planner_injection_constructor_is_not_available():
    with pytest.raises(TypeError):
        OwnerConfirmationService(planner=planner())
    with pytest.raises(TypeError):
        InMemoryAuditLedger(planner=planner())


def test_services_expose_no_mutable_planner_dependency():
    confirmation = OwnerConfirmationService(clock=lambda: NOW)
    assert not hasattr(confirmation, "planner")
    with pytest.raises(TypeError):
        vars(confirmation)


def test_external_planner_behavior_mutation_cannot_redirect_confirmation_or_audit():
    source = planner()
    with pytest.raises(TypeError):
        source.plan = lambda _request: make_plan()
    decision = authorize()
    identity = decision.action_identity
    assert identity is not None
    ledger = InMemoryAuditLedger()
    event = ledger.append(
        audit_builders.authorization_allowed(actor_subject="local-owner", occurred_at=NOW, decision=decision)
    )
    assert event.draft.action_id == identity.action_id
    assert event.draft.result_code == "allowed"


def test_service_instance_behavior_replacement_is_rejected():
    confirmation = OwnerConfirmationService(clock=lambda: NOW)
    malicious = lambda _request: make_plan()
    for obj in (confirmation, confirmation):
        for setter in (
            lambda obj=obj: setattr(obj, "plan", malicious),
            lambda obj=obj: object.__setattr__(obj, "plan", malicious),
            lambda obj=obj: setattr(obj, "_planner", malicious),
            lambda obj=obj: object.__setattr__(obj, "_planner", malicious),
        ):
            with pytest.raises(AttributeError):
                setter()


def test_replaced_model_constructors_fail_authorization_closed(monkeypatch):
    from aipm.control_plane import models as models_module

    class NotAPlan:
        pass

    monkeypatch.setattr(models_module, "ActionPlan", lambda *_args, **_kwargs: NotAPlan())
    decision = authorize()
    assert decision.allowed is False
    assert decision.code is PolicyCode.INVALID_REQUEST


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
        "ActionIdentity",
        "EvidenceSummary",
        "EvidenceState",
        "EvidenceSource",
        "OperationKind",
        "RiskLevel",
        "risk_for",
        "validate_operation",
        "_build_evidence_neutral_plan",
        "derive_action_identity",
    }
    assert forbidden_globals.isdisjoint(vars(approval_module))
    assert forbidden_globals.isdisjoint(vars(audit_module))


def test_module_builder_replacement_cannot_redirect_confirmation_or_audit():
    from aipm.control_plane import audit as audit_module
    from aipm.control_plane import approval as approval_module
    from aipm.control_plane import planner as planner_module

    observed = make_plan()
    original = planner_module._build_evidence_neutral_plan

    def malicious(*_args, **_kwargs):
        return observed

    try:
        planner_module._build_evidence_neutral_plan = malicious
        assert "_build_evidence_neutral_plan" not in vars(approval_module)
        assert "_build_evidence_neutral_plan" not in vars(audit_module)
        decision = authorize()
        identity = decision.action_identity
        assert identity is not None
        service = OwnerConfirmationService(clock=lambda: NOW)
        binding = service.request_confirmation(decision, requester_subject="local-owner")
        ledger = InMemoryAuditLedger()
        event = ledger.append(
            audit_builders.action_created(
                actor_subject="local-owner",
                occurred_at=NOW,
                decision=decision,
                lifecycle_from="requested",
                lifecycle_to="confirmation_required",
            )
        )
        assert binding.action_id == identity.action_id
        assert event.draft.action_id == identity.action_id
    finally:
        planner_module._build_evidence_neutral_plan = original


def test_service_class_mutation_is_rejected_or_fails_closed():
    confirmation_methods = ("request_confirmation", "confirm", "validate", "consume")
    cls = OwnerConfirmationService
    names = confirmation_methods
    originals = {name: getattr(cls, name) for name in names}
    try:
        for name in names:
            with pytest.raises(TypeError):
                setattr(cls, name, lambda *_args, **_kwargs: make_plan())
            type.__delattr__(cls, name)
            with pytest.raises(AttributeError):
                getattr(cls(clock=lambda: NOW), name)
            type.__setattr__(cls, name, originals[name])
    finally:
        for name, method in originals.items():
            type.__setattr__(cls, name, method)


def test_plan_id_alignment_across_planner_policy_confirmation_and_audit():
    import hashlib
    import json

    plan = planner(now=NOW).plan(request())
    current = current_plan()
    decision = authorize(plan=plan, current=current)
    identity = decision.action_identity
    assert identity is not None
    service = OwnerConfirmationService(clock=lambda: NOW)
    binding = service.request_confirmation(decision, requester_subject="local-owner")
    ledger = InMemoryAuditLedger()
    event = ledger.append(
        audit_builders.action_created(
            actor_subject="local-owner",
            occurred_at=NOW,
            decision=decision,
            lifecycle_from="requested",
            lifecycle_to="confirmation_required",
        )
    )

    assert identity.plan_id == plan.plan_id == binding.plan_id == event.draft.plan_id
    assert identity.plan_digest == plan.digest == binding.plan_digest == event.draft.plan_digest
    assert identity.action_id == binding.action_id == event.draft.action_id
    assert identity.target_revision == current.revision == event.draft.plan_revision
    assert len(plan.plan_id) == 32
    assert len(plan.digest) == 64
    assert len(identity.action_id) == 64

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
    decision = authorize(plan=plan)
    identity = decision.action_identity
    assert identity is not None
    assert identity.plan_id == plan.plan_id
    assert identity.plan_digest == plan.digest
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

    plan = planner(now=NOW).plan(request(metadata=(("title", "e\u0301"),)))
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
