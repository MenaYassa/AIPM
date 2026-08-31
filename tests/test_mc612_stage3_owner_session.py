from datetime import datetime, timedelta, timezone

import pytest

from aipm.control_plane.identity import AuthenticationMethod, OwnerPrincipal, PrincipalVerification
from aipm.control_plane.session import OwnerSessionStore


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def principal(**overrides):
    values = {
        "subject": "local-owner",
        "issuer": "aipm-owner-auth",
        "authentication_method": AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE,
        "verification": PrincipalVerification.VERIFIED,
        "auth_epoch": 1,
        "authenticated_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
    }
    values.update(overrides)
    return OwnerPrincipal(**values)


def test_session_cookie_policy_is_secure_and_opaque():
    store = OwnerSessionStore()
    session = store.create(principal=principal(), now=NOW)
    assert session.cookie.secure is True
    assert session.cookie.http_only is True
    assert session.cookie.same_site == "Strict"
    assert session.cookie.path == "/"
    assert session.session_id != "test-owner-secret"
    assert len(session.session_id) >= 32
    assert len(session.csrf_token) >= 32
    assert session.session_id != session.csrf_token


def test_session_references_the_canonical_principal_and_carries_no_secret():
    owner = principal()
    store = OwnerSessionStore()
    session = store.create(principal=owner, now=NOW)
    assert session.principal is owner
    assert session.auth_epoch == 1
    assert session.is_active(NOW)
    dump = [str(value) for value in (session.session_id, session.csrf_token, session.auth_epoch, session.created_at.isoformat())]
    assert all("test-owner-secret" not in value for value in dump)
    with pytest.raises(TypeError):
        store.create(principal={"subject": "local-owner"}, now=NOW)


def test_session_absolute_lifetime_wins_over_activity_refresh():
    store = OwnerSessionStore()
    session = store.create(principal=principal(), now=NOW)
    refreshed = store.get(session.session_id, now=NOW + timedelta(minutes=9))
    assert refreshed is not None
    assert refreshed.expires_at == NOW + timedelta(minutes=30)
    assert store.get(session.session_id, now=NOW + timedelta(minutes=30)) is None


def test_session_inactivity_timeout_and_refresh():
    store = OwnerSessionStore()
    session = store.create(principal=principal(), now=NOW)
    refreshed = store.get(session.session_id, now=NOW + timedelta(minutes=5))
    assert refreshed is not None
    assert refreshed.inactivity_expires_at == NOW + timedelta(minutes=15)
    assert store.get(session.session_id, now=NOW + timedelta(minutes=16)) is None


def test_expired_principal_deactivates_its_session():
    store = OwnerSessionStore()
    session = store.create(principal=principal(expires_at=NOW + timedelta(minutes=1)), now=NOW)
    assert store.get(session.session_id, now=NOW + timedelta(minutes=2)) is None


def test_logout_and_rotation_revoke_sessions():
    store = OwnerSessionStore()
    first = store.create(principal=principal(), now=NOW)
    second = store.create(principal=principal(), now=NOW)
    store.revoke(first.session_id)
    assert store.get(first.session_id, now=NOW) is None
    assert store.get(second.session_id, now=NOW) is not None
    store.revoke_all()
    assert store.get(second.session_id, now=NOW) is None


def test_rotated_session_gets_a_fresh_identifier_and_token():
    store = OwnerSessionStore()
    session = store.create(principal=principal(), now=NOW)
    rotated = store.rotate(session.session_id, now=NOW + timedelta(minutes=1))
    assert rotated is not None
    assert rotated.session_id != session.session_id
    assert rotated.csrf_token != session.csrf_token
    assert store.get(session.session_id, now=NOW + timedelta(minutes=1)) is None
    assert store.get(rotated.session_id, now=NOW + timedelta(minutes=1)) is not None


def test_rotated_authentication_epoch_revokes_every_issued_session():
    store = OwnerSessionStore()
    first = store.create(principal=principal(), now=NOW)
    second = store.create(principal=principal(), now=NOW)
    epoch = store.rotate_auth_epoch()
    assert epoch == 2
    assert store.auth_epoch == 2
    assert store.get(first.session_id, now=NOW) is None
    assert store.get(second.session_id, now=NOW) is None
    fresh = store.create(principal=principal(auth_epoch=2), now=NOW)
    assert fresh.auth_epoch == 2
    assert store.get(fresh.session_id, now=NOW) is not None


def test_active_count_does_not_refresh_inactivity_windows():
    store = OwnerSessionStore()
    session = store.create(principal=principal(), now=NOW)
    for _ in range(5):
        assert store.active_count(now=NOW + timedelta(minutes=3)) == 1
    late = NOW + timedelta(minutes=11)
    assert store.active_count(now=late) == 0
    assert store.get(session.session_id, now=late) is None


def test_csrf_verification_is_constant_time_safe_and_ascii_tolerant():
    store = OwnerSessionStore()
    session = store.create(principal=principal(), now=NOW)
    assert session.verify_csrf(session.csrf_token) is True
    assert session.verify_csrf("wrong-token") is False
    assert session.verify_csrf(None) is False
    assert session.verify_csrf("") is False
    assert session.verify_csrf("é-unicode-token") is False
