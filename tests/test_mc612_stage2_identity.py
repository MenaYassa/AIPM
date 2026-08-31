from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from aipm.control_plane.identity import (
    ACTION_IDENTITY_VERSION,
    PRINCIPAL_IDENTITY_VERSION,
    AuthenticationMethod,
    IdentityError,
    OwnerPrincipal,
    PrincipalVerification,
)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def principal(**overrides):
    values = {
        "subject": "local-owner",
        "issuer": "aipm-owner-auth",
        "authentication_method": AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE,
        "verification": PrincipalVerification.VERIFIED,
        "auth_epoch": 1,
        "authenticated_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
        "roles": ("owner",),
    }
    values.update(overrides)
    return OwnerPrincipal(**values)


def test_owner_principal_is_immutable_and_roles_are_canonical():
    value = principal(roles=("owner", "owner", "auditor"))
    assert value.roles == ("auditor", "owner")
    assert value.is_usable(NOW + timedelta(minutes=9))
    assert value.safe_dict()["verification"] == "verified"
    assert value.safe_dict()["authentication_method"] == "owner_passphrase_argon2id"
    assert value.has_role("owner")
    assert not value.has_role("approver")
    with pytest.raises((FrozenInstanceError, AttributeError)):
        value.subject = "other"


def test_principal_canonical_form_is_stable_for_role_order():
    left = principal(roles=("owner", "auditor"))
    right = principal(roles=("auditor", "owner"))
    assert left.canonical() == right.canonical()
    assert left.safe_dict()["roles"] == ["auditor", "owner"]


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
        ("subject", "subject\nvalue"),
        ("identity_version", "unknown-v2"),
        ("auth_epoch", 0),
        ("auth_epoch", -1),
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
        OwnerPrincipal(**{
            "subject": "local-owner",
            "issuer": "aipm-owner-auth",
            "authentication_method": AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE,
            "verification": PrincipalVerification.VERIFIED,
            "auth_epoch": 1,
            "authenticated_at": NOW,
            "expires_at": NOW + timedelta(minutes=10),
            "token": "secret-token",
        })


def test_principal_carries_no_secret_shaped_fields():
    value = principal()
    dump = value.safe_dict()
    forbidden = {"secret", "password", "token", "cookie", "verifier", "credential", "session_id", "csrf_token"}
    assert forbidden.isdisjoint(dump)
    assert all("secret" not in key and "token" not in key for key in dump)
    assert "secret" not in value.canonical()
    assert PRINCIPAL_IDENTITY_VERSION == "mc612-canonical-principal-v1"
