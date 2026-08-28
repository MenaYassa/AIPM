from datetime import datetime, timedelta, timezone

from aipm.control_plane.models import OperationKind
from aipm.control_plane.owner_auth import (
    Argon2idVerifier,
    AuthenticationError,
    FailureReason,
    OwnerAuthenticator,
    SingleOwnerActionGuard,
)
from aipm.control_plane.project_plan import Environment
from aipm.control_plane.session import OwnerSessionStore


VERIFIER = "$argon2id$v=19$m=65536,t=2,p=1$c3RhZ2UzLXNhbHQtMTIzNA$zho28DBNr2G2cGbxzr0Dl6AKwhbd8hEeTkti1pn7TW0"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def test_argon2id_verifier_accepts_secret_and_rejects_wrong_secret():
    verifier = Argon2idVerifier(VERIFIER)
    assert verifier.verify("test-owner-secret") is True
    assert verifier.verify("wrong-secret") is False
    assert "test-owner-secret" not in verifier.encoded_verifier


def test_malformed_argon2id_verifier_fails_closed():
    try:
        Argon2idVerifier("not-a-verifier")
    except AuthenticationError:
        pass
    else:
        raise AssertionError("malformed verifier must be rejected")


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
    blocked = authenticator.authenticate("test-owner-secret", now=NOW + timedelta(seconds=4))
    assert blocked.reason is FailureReason.LOCKED
    accepted = authenticator.authenticate("test-owner-secret", now=NOW + timedelta(seconds=64))
    assert accepted.accepted is True


def test_owner_session_is_opaque_short_lived_and_revocable():
    sessions = OwnerSessionStore()
    session = sessions.create(now=NOW)
    assert session.authenticated is True
    assert len(session.session_id) >= 32
    assert session.expires_at == NOW + timedelta(minutes=30)
    assert session.inactivity_expires_at == NOW + timedelta(minutes=10)
    refreshed = sessions.get(session.session_id, now=NOW + timedelta(minutes=5))
    assert refreshed is not None
    assert refreshed.inactivity_expires_at == NOW + timedelta(minutes=15)
    assert sessions.get(session.session_id, now=NOW + timedelta(minutes=16)) is None
    sessions.revoke_all()
    assert sessions.active_count(now=NOW) == 0


def test_owner_action_guard_allows_only_staging_allow_list():
    sessions = OwnerSessionStore()
    session = sessions.create(now=NOW)
    guard = SingleOwnerActionGuard(target_ids={"plan-stage-1"})
    allowed = guard.authorize(
        session,
        operation=OperationKind.UPDATE_PROJECT_PLAN,
        target_id="plan-stage-1",
        environment=Environment.STAGING,
        fields=("title", "objective"),
    )
    assert allowed.allowed is True
    production = guard.authorize(
        session,
        operation=OperationKind.UPDATE_PROJECT_PLAN,
        target_id="plan-stage-1",
        environment=Environment.PRODUCTION,
        fields=("title",),
    )
    assert production.code == "DENY_PRODUCTION"
    unknown_field = guard.authorize(
        session,
        operation=OperationKind.UPDATE_PROJECT_PLAN,
        target_id="plan-stage-1",
        environment=Environment.STAGING,
        fields=("title", "metadata"),
    )
    assert unknown_field.code == "DENY_FIELD_SET"
    no_session = guard.authorize(
        None,
        operation=OperationKind.UPDATE_PROJECT_PLAN,
        target_id="plan-stage-1",
        environment=Environment.STAGING,
        fields=("title",),
    )
    assert no_session.code == "DENY_NO_OWNER_SESSION"
