from datetime import datetime, timedelta, timezone

from aipm.control_plane.session import OwnerSessionStore


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def test_session_cookie_policy_is_secure_and_opaque():
    store = OwnerSessionStore()
    session = store.create(now=NOW)
    assert session.cookie.secure is True
    assert session.cookie.http_only is True
    assert session.cookie.same_site == "Strict"
    assert session.cookie.path == "/"
    assert session.session_id != "test-owner-secret"


def test_session_absolute_lifetime_wins_over_activity_refresh():
    store = OwnerSessionStore()
    session = store.create(now=NOW)
    refreshed = store.get(session.session_id, now=NOW + timedelta(minutes=9))
    assert refreshed is not None
    assert refreshed.expires_at == NOW + timedelta(minutes=30)
    assert store.get(session.session_id, now=NOW + timedelta(minutes=30)) is None


def test_logout_and_rotation_revoke_sessions():
    store = OwnerSessionStore()
    first = store.create(now=NOW)
    second = store.create(now=NOW)
    store.revoke(first.session_id)
    assert store.get(first.session_id, now=NOW) is None
    assert store.get(second.session_id, now=NOW) is not None
    store.revoke_all()
    assert store.get(second.session_id, now=NOW) is None
