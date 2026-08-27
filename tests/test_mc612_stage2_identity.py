from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from aipm.control_plane.identity import IdentityError, PrincipalVerification, VerifiedPrincipal

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def principal(**overrides):
    values = {
        "subject": "human-alice",
        "issuer": "identity.example",
        "tenant": "tenant-a",
        "verification": PrincipalVerification.VERIFIED,
        "authenticated_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "roles": ("approver", "requester"),
    }
    values.update(overrides)
    return VerifiedPrincipal(**values)


def test_verified_principal_is_immutable_and_roles_are_canonical():
    value = principal(roles=("requester", "approver", "requester"))
    assert value.roles == ("approver", "requester")
    assert value.is_usable(NOW + timedelta(minutes=9))
    assert value.safe_dict()["verification"] == "verified"
    with pytest.raises((FrozenInstanceError, AttributeError)):
        value.subject = "other"


def test_principal_canonical_form_is_stable_for_role_order():
    left = principal(roles=("requester", "approver"))
    right = principal(roles=("approver", "requester"))
    assert left.canonical() == right.canonical()
    assert left.safe_dict()["roles"] == ["approver", "requester"]


def test_only_verified_unexpired_principal_is_usable():
    assert not principal(verification=PrincipalVerification.UNVERIFIED).is_usable(NOW)
    assert not principal(verification=PrincipalVerification.REVOKED).is_usable(NOW)
    assert not principal().is_usable(NOW + timedelta(minutes=10))
    assert principal().is_usable(NOW + timedelta(minutes=9, seconds=59))


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject", ""),
        ("issuer", "issuer with spaces"),
        ("tenant", "tenant\nvalue"),
        ("identity_version", "unknown-v2"),
    ],
)
def test_invalid_identity_values_fail_closed(field, value):
    with pytest.raises(IdentityError):
        principal(**{field: value})


def test_expiry_must_follow_authentication_time():
    with pytest.raises(IdentityError, match="expiry"):
        principal(expires_at=NOW)


def test_identity_does_not_accept_provider_tokens_or_raw_headers():
    with pytest.raises(TypeError):
        VerifiedPrincipal(**{
            "subject": "human-alice",
            "issuer": "identity.example",
            "tenant": "tenant-a",
            "verification": PrincipalVerification.VERIFIED,
            "authenticated_at": NOW,
            "expires_at": NOW + timedelta(minutes=10),
            "token": "secret-token",
        })
