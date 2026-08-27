from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aipm.control_plane.identity import PrincipalVerification, VerifiedPrincipal
from aipm.control_plane.models import ActorRole, OperationKind
from aipm.control_plane.policy import AuthorizationPolicy, PolicyCode

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def principal(**overrides):
    values = {
        "subject": "human-alice",
        "issuer": "identity.example",
        "tenant": "tenant-a",
        "verification": PrincipalVerification.VERIFIED,
        "authenticated_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "roles": ("requester", "approver"),
    }
    values.update(overrides)
    return VerifiedPrincipal(**values)


def policy():
    return AuthorizationPolicy(policy_version="policy-v1", allowed_scopes=frozenset({("project-demo", "staging")}))


def evaluate(**overrides):
    values = {
        "principal": principal(),
        "actor_role": ActorRole.REQUESTER,
        "operation": OperationKind.UPDATE_PROJECT_PLAN,
        "target_id": "project-demo",
        "environment": "staging",
        "now": NOW,
    }
    values.update(overrides)
    return policy().evaluate(**values)


def test_policy_allows_only_verified_role_and_allow_listed_scope():
    decision = evaluate()
    assert decision.allowed is True
    assert decision.code is PolicyCode.ALLOWED
    assert decision.scope.environment == "staging"
    assert decision.safe_dict()["policy_version"] == "policy-v1"


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"principal": None}, PolicyCode.UNVERIFIED_IDENTITY),
        ({"principal": principal(verification=PrincipalVerification.UNVERIFIED)}, PolicyCode.UNVERIFIED_IDENTITY),
        ({"principal": principal(expires_at=NOW + timedelta(seconds=1)), "now": NOW + timedelta(seconds=1)}, PolicyCode.EXPIRED_IDENTITY),
        ({"actor_role": ActorRole.AUDITOR}, PolicyCode.MISSING_ROLE),
        ({"operation": "arbitrary_operation"}, PolicyCode.UNSUPPORTED_OPERATION),
        ({"target_id": "project-other"}, PolicyCode.TARGET_NOT_ALLOWED),
        ({"environment": "production"}, PolicyCode.ENVIRONMENT_NOT_ALLOWED),
    ],
)
def test_policy_denies_invalid_identity_role_operation_or_scope(overrides, code):
    decision = evaluate(**overrides)
    assert decision.allowed is False
    assert decision.code is code


def test_policy_denies_self_approval():
    decision = evaluate(
        actor_role=ActorRole.APPROVER,
        requester_subject="human-alice",
        approver_subject="human-alice",
    )
    assert decision.allowed is False
    assert decision.code is PolicyCode.SELF_APPROVAL


def test_policy_accepts_distinct_requester_and_approver():
    decision = evaluate(
        actor_role=ActorRole.APPROVER,
        requester_subject="human-alice",
        approver_subject="human-bob",
    )
    assert decision.allowed is True


def test_policy_cannot_be_configured_with_another_operation_or_self_approval_rule():
    with pytest.raises(ValueError):
        AuthorizationPolicy(policy_version="policy-v1", allowed_scopes={("project-demo", "staging")}, allowed_operations=frozenset())
    with pytest.raises(ValueError):
        AuthorizationPolicy(policy_version="policy-v1", allowed_scopes={("project-demo", "staging")}, require_distinct_requester_approver=False)
