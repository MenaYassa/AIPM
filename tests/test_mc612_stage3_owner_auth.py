from datetime import datetime, timedelta, timezone

import pytest

from aipm.control_plane.identity import AuthenticationMethod, PrincipalVerification
from aipm.control_plane.owner_auth import (
    Argon2idVerifier,
    AuthenticationError,
    FailureReason,
    OwnerAuthenticator,
)


VERIFIER = "$argon2id$v=19$m=65536,t=2,p=1$c3RhZ2UzLXNhbHQtMTIzNA$zho28DBNr2G2cGbxzr0Dl6AKwhbd8hEeTkti1pn7TW0"
SECRET = "test-owner-secret"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def test_argon2id_verifier_accepts_secret_and_rejects_wrong_secret():
    verifier = Argon2idVerifier(VERIFIER)
    assert verifier.verify(SECRET) is True
    assert verifier.verify("wrong-secret") is False
    assert SECRET not in verifier.encoded_verifier


def test_malformed_argon2id_verifier_fails_closed():
    with pytest.raises(AuthenticationError):
        Argon2idVerifier("not-a-verifier")
    with pytest.raises(AuthenticationError):
        Argon2idVerifier("$argon2i$v=19$m=65536,t=2,p=1$c2FsdA$aGFzaA")


def test_successful_authentication_produces_the_canonical_principal():
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=lambda: NOW)
    result = authenticator.authenticate(SECRET, now=NOW)
    assert result.accepted is True
    assert result.reason is FailureReason.ACCEPTED
    principal = result.principal
    assert principal is not None
    assert principal.subject == "local-owner"
    assert principal.issuer == "aipm-owner-auth"
    assert principal.authentication_method is AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE
    assert principal.verification is PrincipalVerification.VERIFIED
    assert principal.auth_epoch == 1
    assert principal.roles == ("owner",)
    assert principal.is_usable(NOW + timedelta(minutes=29))
    assert not principal.is_usable(NOW + timedelta(minutes=30))


def test_authentication_result_and_principal_never_contain_the_secret():
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=lambda: NOW)
    result = authenticator.authenticate(SECRET, now=NOW)
    assert SECRET not in repr(result)
    assert SECRET not in str(result)
    principal = result.principal
    assert principal is not None
    assert SECRET not in principal.canonical()
    for key, value in principal.safe_dict().items():
        assert value != SECRET, key
        assert SECRET not in str(value)


def test_invalid_secret_fails_without_principal_or_leak():
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=lambda: NOW)
    result = authenticator.authenticate("wrong", now=NOW)
    assert result.accepted is False
    assert result.principal is None
    assert SECRET not in repr(result)


def test_authentication_has_progressive_cooldown_and_finite_lockout():
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), max_failures_before_lock=3, lockout_seconds=60)
    first = authenticator.authenticate("bad", now=NOW)
    assert first.reason is FailureReason.REJECTED
    assert first.retry_after_seconds == 1
    second = authenticator.authenticate("bad", now=NOW + timedelta(seconds=1))
    assert second.reason is FailureReason.REJECTED
    assert second.retry_after_seconds == 2
    third = authenticator.authenticate("bad", now=NOW + timedelta(seconds=3))
    assert third.reason is FailureReason.LOCKED
    blocked = authenticator.authenticate(SECRET, now=NOW + timedelta(seconds=4))
    assert blocked.reason is FailureReason.LOCKED
    accepted = authenticator.authenticate(SECRET, now=NOW + timedelta(seconds=64))
    assert accepted.accepted is True


def test_authentication_epoch_rotation_is_reflected_in_new_principals():
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=lambda: NOW)
    first = authenticator.authenticate(SECRET, now=NOW)
    assert first.principal is not None and first.principal.auth_epoch == 1
    epoch = authenticator.rotate_auth_epoch()
    assert epoch == 2 and authenticator.auth_epoch == 2
    second = authenticator.authenticate(SECRET, now=NOW + timedelta(seconds=1))
    assert second.principal is not None and second.principal.auth_epoch == 2
    assert first.principal.auth_epoch != second.principal.auth_epoch


def test_epoch_one_principals_do_not_satisfy_a_rotated_epoch():
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=lambda: NOW)
    first = authenticator.authenticate(SECRET, now=NOW)
    authenticator.rotate_auth_epoch()
    assert first.principal is not None
    assert first.principal.auth_epoch != authenticator.auth_epoch
