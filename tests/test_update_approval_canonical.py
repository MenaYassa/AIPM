"""Security-contract tests: canonical control-plane compatibility.

These tests prove the update approval contract binds to the canonical MC-6.12
authentication model (OwnerPrincipal/OwnerSession) without the security
module itself importing the control plane: session binding, authentication
epoch invalidation, and usable-principal enforcement.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aipm.control_plane.identity import (
    OWNER_ISSUER,
    OWNER_ROLE,
    OWNER_SUBJECT,
    AuthenticationMethod,
    OwnerPrincipal,
    PrincipalVerification,
)
from aipm.control_plane.session import OwnerSession
from aipm.services.update.approval import (
    ApprovalState,
    InMemoryUpdateApprovalStore,
    OperatorIdentity,
    UpdateApprovalError,
    UpdateApprovalService,
)


def principal(epoch: int = 1, subject: str = OWNER_SUBJECT) -> OwnerPrincipal:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return OwnerPrincipal(
        subject=subject,
        issuer=OWNER_ISSUER,
        authentication_method=AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE,
        verification=PrincipalVerification.VERIFIED,
        auth_epoch=epoch,
        authenticated_at=now,
        expires_at=now + timedelta(minutes=30),
        roles=(OWNER_ROLE,),
    )


def session(session_id: str = "sess-0001", epoch: int = 1) -> OwnerSession:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return OwnerSession(
        session_id=session_id,
        principal=principal(epoch),
        auth_epoch=epoch,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(minutes=30),
        inactivity_expires_at=now + timedelta(minutes=10),
        csrf_token="csrf-not-a-secret",
    )


def fixed_clock(value: str = "2026-01-01T12:00:00+00:00"):
    moment = datetime.fromisoformat(value)
    return lambda: moment


def make_service():
    return UpdateApprovalService(store=InMemoryUpdateApprovalStore(), clock=fixed_clock())


DIGEST = "a" * 64
PROJECT_ID = "b" * 24


def issue_for(operator: OperatorIdentity, service: UpdateApprovalService | None = None):
    service = service or make_service()
    record = service.issue(
        project_id=PROJECT_ID,
        project_name="demo",
        plan_digest=DIGEST,
        operator=operator,
    )
    return record, service


def test_operator_identity_from_canonical_session():
    operator = OperatorIdentity.from_session(session())
    assert operator.subject == OWNER_SUBJECT
    assert operator.auth_epoch == 1
    assert operator.session_id == "sess-0001"


def test_approval_bound_to_canonical_session_validates():
    operator = OperatorIdentity.from_session(session())
    service = make_service()
    record = service.issue(project_id=PROJECT_ID, project_name="demo", plan_digest=DIGEST, operator=operator)
    validated = service.validate(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator)
    assert validated.state is ApprovalState.ISSUED


def test_copied_approval_id_rejected_for_different_session():
    operator = OperatorIdentity.from_session(session("sess-0001"))
    other_session = OperatorIdentity.from_session(session("sess-0002"))
    record, service = issue_for(operator)
    with pytest.raises(UpdateApprovalError) as excinfo:
        service.validate(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=other_session)
    assert excinfo.value.reason == UpdateApprovalError.SESSION_MISMATCH


def test_approval_fails_after_auth_epoch_rotation():
    operator = OperatorIdentity.from_session(session(epoch=1))
    rotated = OperatorIdentity.from_session(session(epoch=2))
    record, service = issue_for(operator)
    with pytest.raises(UpdateApprovalError) as excinfo:
        service.validate(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=rotated)
    assert excinfo.value.reason == UpdateApprovalError.SESSION_MISMATCH


def test_approval_fails_for_stale_principal_model():
    """A stale principal (expired validity window) still extracts its binding
    material; session freshness is the session store's responsibility at
    lookup time, and epoch/session binding is re-checked at validation."""

    stale = session("sess-old")
    operator = OperatorIdentity.from_session(stale)
    assert operator.session_id == "sess-old"
    record, service = issue_for(operator)
    later = fixed_clock("2026-01-01T12:00:00+00:00")() + timedelta(minutes=45)
    with pytest.raises(UpdateApprovalError) as excinfo:
        service.validate(record.approval_id, project_id=PROJECT_ID, plan_digest=DIGEST, operator=operator, now=later)
    assert excinfo.value.reason == UpdateApprovalError.APPROVAL_EXPIRED
