"""Staging-only server-side owner session contract.

Sessions are represented in memory for this non-production slice. This module
has no HTTP, cookie transport, persistence, or external-system integration.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True, slots=True)
class SessionCookiePolicy:
    secure: bool = True
    http_only: bool = True
    same_site: str = "Strict"
    path: str = "/"


@dataclass(frozen=True, slots=True)
class OwnerSession:
    session_id: str
    authenticated: bool
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    inactivity_expires_at: datetime
    cookie: SessionCookiePolicy = SessionCookiePolicy()

    def is_active(self, now: datetime) -> bool:
        current = _utc(now)
        return self.authenticated and current < self.expires_at and current < self.inactivity_expires_at


class OwnerSessionStore:
    """Bounded in-memory server-side session store for staging only."""

    __slots__ = ("_sessions", "_absolute_lifetime", "_inactivity_timeout", "_clock", "_initialized")

    def __init__(
        self,
        *,
        clock=None,
        absolute_lifetime: timedelta = timedelta(minutes=30),
        inactivity_timeout: timedelta = timedelta(minutes=10),
    ) -> None:
        if absolute_lifetime <= timedelta(0) or inactivity_timeout <= timedelta(0):
            raise ValueError("session durations must be positive")
        object.__setattr__(self, "_sessions", {})
        object.__setattr__(self, "_absolute_lifetime", absolute_lifetime)
        object.__setattr__(self, "_inactivity_timeout", inactivity_timeout)
        object.__setattr__(self, "_clock", clock or (lambda: datetime.now(timezone.utc)))
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("OwnerSessionStore configuration is immutable")
        object.__setattr__(self, name, value)

    def create(self, *, now: datetime | None = None) -> OwnerSession:
        current = _utc(now or self._clock())
        session = OwnerSession(
            session_id=secrets.token_urlsafe(32),
            authenticated=True,
            created_at=current,
            last_seen_at=current,
            expires_at=current + self._absolute_lifetime,
            inactivity_expires_at=current + self._inactivity_timeout,
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str, *, now: datetime | None = None) -> OwnerSession | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        current = _utc(now or self._clock())
        if not session.is_active(current):
            self.revoke(session_id)
            return None
        refreshed = OwnerSession(
            session_id=session.session_id,
            authenticated=True,
            created_at=session.created_at,
            last_seen_at=current,
            expires_at=session.expires_at,
            inactivity_expires_at=min(session.expires_at, current + self._inactivity_timeout),
            cookie=session.cookie,
        )
        self._sessions[session_id] = refreshed
        return refreshed

    def revoke(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def revoke_all(self) -> None:
        self._sessions.clear()

    def active_count(self, *, now: datetime | None = None) -> int:
        current = _utc(now or self._clock())
        return sum(1 for session_id in tuple(self._sessions) if self.get(session_id, now=current) is not None)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be datetime")
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
