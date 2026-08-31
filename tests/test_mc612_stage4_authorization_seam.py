"""Shot 4 (MC-6.12 canonical identity + authorization seam) end-to-end tests.

Covers the canonical flow: owner credential → OwnerAuthenticator → canonical
OwnerPrincipal → opaque session → AuthorizationPolicy → AuthorizationDecision →
bounded ActionRequest → explicit owner confirmation → canonical action identity
— plus the bypass tests proving no alternate path can authorize an action.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.audit import AuditEventType, InMemoryAuditLedger
from aipm.control_plane.identity import (
    AuthenticationMethod,
    OwnerPrincipal,
    PrincipalVerification,
    derive_action_identity,
)
from aipm.control_plane.lifecycle import IMPLEMENTED_STATES
from aipm.control_plane.models import (
    ActionRequest,
    ConfirmationState,
    ControlPlaneError,
    LifecycleState,
    OperationKind,
    PlanningErrorCode,
)
from aipm.control_plane.owner_auth import Argon2idVerifier, OwnerAuthenticator
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import AuthorizationDecision, AuthorizationPolicy, PolicyCode
from aipm.control_plane.project_plan import InMemoryProjectPlanStore, ProjectPlan
from aipm.control_plane.service import OwnerControlPlaneService
from aipm.control_plane.session import OwnerSessionStore

VERIFIER = "$argon2id$v=19$m=65536,t=2,p=1$c3RhZ2UzLXNhbHQtMTIzNA$zho28DBNr2G2cGbxzr0Dl6AKwhbd8hEeTkti1pn7TW0"
SECRET = "test-owner-secret"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value


def build_service(*, plan=None, policy_overrides=None):
    clock = _Clock(NOW)
    targets = {"project-demo"}
    planner_targets = {"project-demo", "project-other"}
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=clock)
    sessions = OwnerSessionStore(clock=clock)
    policy = AuthorizationPolicy(
        policy_version="policy-v1",
        allowed_scopes=frozenset({(target, "staging") for target in targets}),
        **(policy_overrides or {}),
    )
    confirmations = OwnerConfirmationService(clock=clock)
    plans = InMemoryProjectPlanStore()
    current = plan or ProjectPlan.create(
        target_id="project-demo",
        environment=plan_env(),
        title="Old title",
        objective="Objective",
        now=NOW,
    )
    plans.create(current)
    planner = PlanOnlyPlanner(clock=clock, target_allow_list=planner_targets)
    service = OwnerControlPlaneService(
        authenticator=authenticator,
        sessions=sessions,
        policy=policy,
        confirmations=confirmations,
        plans=plans,
        planner=planner,
        audit=InMemoryAuditLedger(),
        execution_mode='test',
        clock=clock,
    )
    return service, plans, clock


def plan_env():
    from aipm.control_plane.project_plan import Environment

    return Environment.STAGING


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


def login(service, *, now=None):
    return service.login(SECRET, now=now)


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------


def test_valid_owner_authentication_yields_canonical_principal_session():
    service, _plans, _clock = build_service()
    session = login(service)
    assert session.principal.subject == "local-owner"
    assert session.principal.authentication_method is AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE
    assert session.principal.verification is PrincipalVerification.VERIFIED
    assert session.principal.has_role("owner")


def test_invalid_secret_fails_closed_without_session():
    service, _plans, _clock = build_service()
    with pytest.raises(ControlPlaneError) as error:
        service.login("wrong-secret")
    assert error.value.code is PlanningErrorCode.AUTHENTICATION_REJECTED


def test_malformed_verifier_fails_closed():
    with pytest.raises(ValueError):
        OwnerAuthenticator(Argon2idVerifier("not-a-verifier"))


def test_expired_principal_deactivates_the_session():
    service, _plans, _clock = build_service()
    session = login(service)
    _clock.value = NOW + timedelta(minutes=31)
    with pytest.raises(ControlPlaneError):
        service.authorize(session.session_id, request(), now=_clock.value)


def test_principal_is_immutable():
    service, _plans, _clock = build_service()
    session = login(service)
    with pytest.raises(AttributeError):
        session.principal.subject = "someone-else"


def test_no_secret_leaks_into_session_decision_or_audit():
    service, _plans, clock = build_service()
    session = login(service)
    decision = service.authorize(session.session_id, request())
    surfaces = [repr(session), str(session), session.principal.canonical(), decision.safe_dict()]
    for surface in surfaces:
        assert SECRET not in surface


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def test_session_creation_absolute_expiry_inactivity_and_logout():
    service, _plans, clock = build_service()
    session = login(service)
    assert len(session.session_id) >= 32
    assert session.expires_at == NOW + timedelta(minutes=30)
    assert session.inactivity_expires_at == NOW + timedelta(minutes=10)
    clock.value = NOW + timedelta(minutes=5)
    assert service.session(session.session_id, now=clock.value) is not None
    clock.value = NOW + timedelta(minutes=30)
    with pytest.raises(ControlPlaneError):
        service.session(session.session_id, now=clock.value)
    second = login(service, now=NOW + timedelta(minutes=31))
    clock.value = NOW + timedelta(minutes=43)
    with pytest.raises(ControlPlaneError):
        service.session(second.session_id, now=clock.value)
    third = login(service, now=clock.value)
    service.logout(third.session_id)
    with pytest.raises(ControlPlaneError):
        service.session(third.session_id, now=clock.value)


def test_revoked_authentication_epoch_invalidates_outstanding_sessions():
    service, _plans, clock = build_service()
    session = login(service)
    service.rotate_credentials()
    with pytest.raises(ControlPlaneError):
        service.session(session.session_id, now=clock.value)
    fresh = login(service, now=clock.value)
    assert fresh.auth_epoch == 2
    assert fresh.principal.auth_epoch == 2


def test_session_identifier_is_opaque_and_store_holds_no_secret():
    service, _plans, _clock = build_service()
    session = login(service)
    raw = repr(session.__dict__) if hasattr(session, "__dict__") else ""
    assert SECRET not in raw
    assert SECRET not in session.session_id
    assert session.session_id != session.csrf_token


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def test_valid_owner_receives_allowed_confirmable_decision():
    service, _plans, _clock = build_service()
    session = login(service)
    decision = service.authorize(session.session_id, request())
    assert decision.allowed is True
    assert decision.confirmation_required is True
    assert decision.action_identity is not None
    assert decision.principal_subject == "local-owner"
    lifecycle = service.lifecycle(decision.action_identity.action_id)
    assert lifecycle is not None
    assert lifecycle.state is LifecycleState.CONFIRMATION_REQUIRED


def test_authorization_without_a_session_is_refused():
    service, _plans, _clock = build_service()
    with pytest.raises(ControlPlaneError) as error:
        service.authorize("fabricated-session-id", request())
    assert error.value.code is PlanningErrorCode.SESSION_INVALID


def test_wrong_target_wrong_environment_and_forbidden_fields_are_denied():
    service, _plans, _clock = build_service()
    session = login(service)
    decision = service.authorize(session.session_id, request(target_id="project-other"))
    assert decision.allowed is False and decision.code.value == "target_not_allowed"
    decision = service.authorize(session.session_id, request(environment="production"))
    assert decision.allowed is False and decision.code.value == "environment_not_allowed"
    decision = service.authorize(session.session_id, request(metadata=(("objective", "x"), ("nickname", "y"))))
    assert decision.allowed is False and decision.code.value == "field_not_allowed"


def test_malformed_requests_never_reach_the_policy():
    with pytest.raises(ControlPlaneError):
        request(metadata=(("command", "value"),))
    with pytest.raises(ValueError):
        request(operation="reboot_host")
    with pytest.raises(ControlPlaneError):
        request(environment="production-staging")


def test_missing_and_disabled_plans_are_denied():
    service, plans, _clock = build_service()
    session = login(service)
    plans._plans.clear()
    decision = service.authorize(session.session_id, request())
    assert decision.allowed is False and decision.code.value == "plan_missing"
    service2, plans2, _clock2 = build_service()
    from dataclasses import replace as dc_replace

    disabled_target = list(plans2._plans.values())[0]
    plans2._plans[disabled_target.target_id] = dc_replace(disabled_target, enabled=False)
    session2 = login(service2)
    decision2 = service2.authorize(session2.session_id, request())
    assert decision2.allowed is False and decision2.code.value == "plan_disabled"


def test_policy_denial_is_deterministic():
    service, _plans, _clock = build_service()
    session = login(service)
    first = service.authorize(session.session_id, request(metadata=(("objective", "x"), ("nickname", "y"))))
    second = service.authorize(session.session_id, request(metadata=(("objective", "x"), ("nickname", "y"))))
    assert first.decision_id == second.decision_id


def test_expired_decision_cannot_be_confirmed():
    service, _plans, clock = build_service()
    session = login(service)
    decision = service.authorize(session.session_id, request())
    clock.value = decision.expires_at
    with pytest.raises(ControlPlaneError) as error:
        service.confirm(session.session_id, decision.decision_id, now=clock.value)
    assert error.value.code is PlanningErrorCode.EXPIRED_PLAN


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def test_correct_owner_confirmation_completes_the_flow():
    service, _plans, clock = build_service()
    session = login(service)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    binding = service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    assert binding.state is ConfirmationState.CONFIRMED
    assert binding.action_id == identity.action_id
    assert binding.target_revision == identity.target_revision
    assert binding.target_digest == identity.target_digest
    assert binding.policy_version == identity.policy_version
    lifecycle = service.lifecycle(identity.action_id)
    assert lifecycle is not None
    assert lifecycle.state is LifecycleState.CONFIRMED
    assert lifecycle.approver_subject == "local-owner"


def test_confirmation_with_unknown_action_id_is_refused():
    service, _plans, _clock = build_service()
    session = login(service)
    with pytest.raises(ControlPlaneError, match="Unknown"):
        service.confirm(session.session_id, "f" * 32)


def test_confirmation_cannot_be_replayed():
    service, _plans, clock = build_service()
    session = login(service)
    decision = service.authorize(session.session_id, request())
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    with pytest.raises(ControlPlaneError):
        service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=2))


def test_confirmation_does_not_authorize_a_changed_request():
    service, _plans, clock = build_service()
    session = login(service)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    changed = service.authorize(
        session.session_id,
        request(metadata=(("title", "Changed title"),), idempotency_key="idem-002"),
        now=NOW + timedelta(minutes=2),
    )
    changed_identity = changed.action_identity
    assert changed_identity is not None
    assert changed_identity.action_id != identity.action_id
    assert changed_identity.plan_digest != identity.plan_digest
    with pytest.raises(ControlPlaneError):
        service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=3))


def test_same_idempotency_key_with_changed_request_is_a_conflict():
    service, _plans, clock = build_service()
    session = login(service)
    decision = service.authorize(session.session_id, request())
    assert decision.allowed is True
    with pytest.raises(ControlPlaneError) as error:
        service.authorize(session.session_id, request(metadata=(("title", "Different request"),)))
    assert error.value.code is PlanningErrorCode.IDEMPOTENCY_CONFLICT


def test_confirmation_binds_plan_revision_and_detects_revision_drift():
    service, plans, clock = build_service()
    session = login(service)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    plans.update("project-demo", expected_revision=1, fields={"title": "Concurrent edit"}, now=NOW)
    binding = service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    assert binding.target_revision == identity.target_revision == 1
    current = plans.read("project-demo")
    assert current.revision == 2
    next_decision = service.authorize(session.session_id, request(idempotency_key="idem-002"), now=NOW + timedelta(minutes=2))
    next_identity = next_decision.action_identity
    assert next_identity is not None
    assert next_identity.target_revision == 2
    assert next_identity.action_id != identity.action_id


def test_confirmation_requires_an_authenticated_session():
    service, _plans, _clock = build_service()
    with pytest.raises(ControlPlaneError) as error:
        service.confirm("no-such-session", "f" * 32)
    assert error.value.code is PlanningErrorCode.SESSION_INVALID


def test_confirmation_by_a_different_subject_is_refused():
    service, _plans, clock = build_service()
    session = login(service)
    decision = service.authorize(session.session_id, request())
    binding = service._confirmations.request_confirmation(decision, requester_subject="local-owner", now=NOW)
    with pytest.raises(ControlPlaneError, match="owner"):
        service._confirmations.confirm(binding, confirmed_by_subject="someone-else", now=NOW)


# ---------------------------------------------------------------------------
# Identity consistency
# ---------------------------------------------------------------------------


def test_planner_policy_confirmation_and_audit_resolve_the_same_identity():
    service, _plans, clock = build_service()
    session = login(service)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    plan = service._planner.plan(request())
    direct = derive_action_identity(
        request=request(),
        plan=plan,
        current_plan=service._plans.read("project-demo"),
        policy_version="policy-v1",
        requester_subject="local-owner",
    )
    assert direct == identity
    created_events = [event for event in service._audit.events() if event.event_type is AuditEventType.ACTION_CREATED]
    assert len(created_events) == 1
    record = created_events[0]
    assert record.draft.action_id == identity.action_id
    assert record.draft.plan_id == identity.plan_id
    assert record.draft.plan_digest == identity.plan_digest
    binding = service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    assert binding.action_id == identity.action_id


def test_audit_trail_records_the_full_canonical_flow():
    service, _plans, _clock = build_service()
    session = login(service)
    decision = service.authorize(session.session_id, request())
    service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    types = [event.event_type for event in service._audit.events()]
    assert types == [
        AuditEventType.AUTHENTICATION_SUCCESS,
        AuditEventType.SESSION_CREATED,
        AuditEventType.AUTHORIZATION_ALLOWED,
        AuditEventType.ACTION_CREATED,
        AuditEventType.LIFECYCLE_TRANSITION,
        AuditEventType.LIFECYCLE_TRANSITION,
        AuditEventType.OWNER_CONFIRMATION_REQUESTED,
        AuditEventType.OWNER_CONFIRMED,
        AuditEventType.LIFECYCLE_TRANSITION,
    ]
    subjects = {event.draft.actor_subject for event in service._audit.events()}
    assert subjects == {"local-owner"}
    assert service.verify_audit_chain().ok is True


# ---------------------------------------------------------------------------
# Bypass attempts
# ---------------------------------------------------------------------------


def test_raw_actor_string_cannot_authorize():
    service, _plans, _clock = build_service()
    with pytest.raises(ControlPlaneError) as error:
        service.authorize("local-owner", request())
    assert error.value.code is PlanningErrorCode.SESSION_INVALID


def raw_session_id() -> str:
    store = OwnerSessionStore()
    session = store.create(principal=owner_principal())
    return session.session_id


def owner_principal() -> OwnerPrincipal:
    return OwnerPrincipal(
        subject="local-owner",
        issuer="aipm-owner-auth",
        authentication_method=AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE,
        verification=PrincipalVerification.VERIFIED,
        auth_epoch=1,
        authenticated_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
    )


def test_raw_session_object_cannot_authorize():
    service, _plans, _clock = build_service()
    with pytest.raises(ControlPlaneError):
        service.authorize(raw_session_id(), request())


def test_boolean_or_flag_style_authorization_surface_does_not_exist():
    service, _plans, _clock = build_service()
    names = {name for name in dir(service) if not name.startswith("_")}
    forbidden = {"approve", "execute", "apply", "run", "exec", "update", "force", "skip"}
    assert names.isdisjoint(forbidden)
    session = login(service)
    with pytest.raises(TypeError):
        service.authorize(session.session_id, request(), approve=True)  # type: ignore[call-arg]


def test_reconstructed_plan_identity_cannot_be_confirmed():
    service, _plans, _clock = build_service()
    session = login(service)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    from aipm.control_plane.identity import ActionIdentity

    forged = ActionIdentity(
        action_id=identity.action_id,
        plan_id=identity.plan_id,
        plan_digest=identity.plan_digest,
        target_revision=identity.target_revision + 5,
        target_digest=identity.target_digest,
        policy_version=identity.policy_version,
        requester_subject=identity.requester_subject,
        operation=identity.operation,
        target_id=identity.target_id,
        environment=identity.environment,
    )
    assert forged != identity
    with pytest.raises(ControlPlaneError, match="Unknown"):
        service.confirm(session.session_id, "0" * 32)


def test_forged_decision_object_cannot_be_confirmed():
    from aipm.control_plane.identity import ActionIdentity

    service, _plans, _clock = build_service()
    session = login(service)
    decision = service.authorize(session.session_id, request())
    identity = decision.action_identity
    assert identity is not None
    forged_identity = ActionIdentity(
        action_id="e" * 64,
        plan_id=identity.plan_id,
        plan_digest=identity.plan_digest,
        target_revision=identity.target_revision,
        target_digest=identity.target_digest,
        policy_version="attacker-policy",
        requester_subject="local-owner",
        operation=identity.operation,
        target_id=identity.target_id,
        environment=identity.environment,
    )
    forged = AuthorizationDecision(
        decision_id="f" * 32,
        allowed=True,
        code=PolicyCode.ALLOWED,
        operation=OperationKind.UPDATE_PROJECT_PLAN,
        target_id="project-demo",
        environment="staging",
        policy_version="attacker-policy",
        principal_subject="local-owner",
        confirmation_required=True,
        decided_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        action_identity=forged_identity,
        plan_revision=forged_identity.target_revision,
        plan_digest=forged_identity.plan_digest,
        request=request(),
    )
    with pytest.raises(ControlPlaneError, match="Unknown"):
        service.confirm(session.session_id, forged.decision_id)


def test_service_never_gains_arbitrary_execution_authority():
    # Shot 6 adds the single bounded execute_action verb; no arbitrary
    # execution surface exists and the unimplemented control states stay out.
    service, _plans, _clock = build_service()
    names = {name for name in dir(service) if not name.startswith("_")}
    assert names.isdisjoint({"exec", "run", "spawn", "subprocess", "execute_command", "execute_script"})
    from aipm.control_plane.lifecycle import implemented_states
    for reserved in (LifecycleState.CANCEL_REQUESTED, LifecycleState.TIMED_OUT, LifecycleState.INTERRUPTED, LifecycleState.ROLLBACK_UNAVAILABLE):
        assert reserved not in implemented_states()
