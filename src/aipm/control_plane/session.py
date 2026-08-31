"""Canonical server-side owner session contract.

Sessions are represented in memory for this slice; this is explicitly NOT
production-complete session infrastructure and will be replaced by a durable
store behind the same interface. The module has no HTTP, cookie transport, or
external-system integration.

A session references the canonical ``OwnerPrincipal`` produced by successful
authentication and carries the authentication epoch it was issued under. The
owner secret never enters a session. Sessions carry an opaque identifier and a
CSRF token; every state change must present a matching token. Cookie
attributes are restrictive by default.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from aipm.control_plane.identity import OwnerPrincipal

_SESSION_ID_BYTES = 32


@dataclass(frozen=True, slots=True)
class SessionCookiePolicy:
    secure: bool = True
    http_only: bool = True
    same_site: str = "Strict"
    path: str = "/"


@dataclass(frozen=True, slots=True)
class OwnerSession:
    """Opaque server-side session bound to the canonical owner principal."""

    session_id: str
    principal: OwnerPrincipal
    auth_epoch: int
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    inactivity_expires_at: datetime
    cookie: SessionCookiePolicy = SessionCookiePolicy()
    csrf_token: str = ""

    def is_active(self, now: datetime) -> bool:
        current = _utc(now)
        if self.auth_epoch < 1:
            return False
        if not isinstance(self.principal, OwnerPrincipal) or not self.principal.is_usable(current):
            return False
        return current < self.expires_at and current < self.inactivity_expires_at

    def verify_csrf(self, presented: str | None) -> bool:
        if not isinstance(presented, str) or not presented or not self.csrf_token:
            return False
        return secrets.compare_digest(presented.encode("utf-8"), self.csrf_token.encode("utf-8"))


class SessionStore(Protocol):
    """Interface for session stores; a durable replacement can implement this."""

    def create(self, *, principal: OwnerPrincipal, now: datetime | None = None) -> OwnerSession: ...

    def get(self, session_id: str, *, now: datetime | None = None) -> OwnerSession | None: ...

    def revoke(self, session_id: str) -> None: ...

    def revoke_all(self) -> None: ...

    def rotate(self, session_id: str, *, now: datetime | None = None) -> OwnerSession | None: ...

    def active_count(self, *, now: datetime | None = None) -> int: ...

    def rotate_auth_epoch(self) -> int: ...


class OwnerSessionStore:
    """Bounded in-memory session store for staging; not production-complete.

    The authentication epoch is checked on every lookup: when the epoch is
    rotated (for example after a credential change), every previously issued
    session is refused, implementing global revocation.
    """

    __slots__ = ("_sessions", "_absolute_lifetime", "_inactivity_timeout", "_clock", "_auth_epoch", "_initialized")

    def __init__(
        self,
        *,
        clock=None,
        absolute_lifetime: timedelta = timedelta(minutes=30),
        inactivity_timeout: timedelta = timedelta(minutes=10),
        auth_epoch: int = 1,
    ) -> None:
        if absolute_lifetime <= timedelta(0) or inactivity_timeout <= timedelta(0):
            raise ValueError("session durations must be positive")
        if not isinstance(auth_epoch, int) or auth_epoch < 1:
            raise ValueError("invalid authentication epoch")
        object.__setattr__(self, "_sessions", {})
        object.__setattr__(self, "_absolute_lifetime", absolute_lifetime)
        object.__setattr__(self, "_inactivity_timeout", inactivity_timeout)
        object.__setattr__(self, "_clock", clock or (lambda: datetime.now(timezone.utc)))
        object.__setattr__(self, "_auth_epoch", auth_epoch)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("OwnerSessionStore configuration is immutable")
        object.__setattr__(self, name, value)

    @property
    def auth_epoch(self) -> int:
        return self._auth_epoch

    def create(self, *, principal: OwnerPrincipal, now: datetime | None = None) -> OwnerSession:
        if not isinstance(principal, OwnerPrincipal):
            raise TypeError("sessions require a canonical OwnerPrincipal")
        current = _utc(now or self._clock())
        session = OwnerSession(
            session_id=secrets.token_urlsafe(_SESSION_ID_BYTES),
            principal=principal,
            auth_epoch=self._auth_epoch,
            created_at=current,
            last_seen_at=current,
            expires_at=current + self._absolute_lifetime,
            inactivity_expires_at=current + self._inactivity_timeout,
            csrf_token=secrets.token_urlsafe(_SESSION_ID_BYTES),
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str, *, now: datetime | None = None) -> OwnerSession | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        current = _utc(now or self._clock())
        if session.auth_epoch != self._auth_epoch or not session.is_active(current):
            self.revoke(session_id)
            return None
        refreshed = OwnerSession(
            session_id=session.session_id,
            principal=session.principal,
            auth_epoch=session.auth_epoch,
            created_at=session.created_at,
            last_seen_at=current,
            expires_at=session.expires_at,
            inactivity_expires_at=min(session.expires_at, current + self._inactivity_timeout),
            cookie=session.cookie,
            csrf_token=session.csrf_token,
        )
        self._sessions[session_id] = refreshed
        return refreshed

    def revoke(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def revoke_all(self) -> None:
        self._sessions.clear()

    def rotate(self, session_id: str, *, now: datetime | None = None) -> OwnerSession | None:
        current = _utc(now or self._clock())
        existing = self.get(session_id, now=current)
        if existing is None:
            return None
        self.revoke(session_id)
        return self.create(principal=existing.principal, now=current)

    def active_count(self, *, now: datetime | None = None) -> int:
        """Count sessions that are valid right now, without refreshing them."""

        current = _utc(now or self._clock())
        return sum(
            1
            for session in tuple(self._sessions.values())
            if session.auth_epoch == self._auth_epoch and session.is_active(current)
        )

    def rotate_auth_epoch(self) -> int:
        """Advance the authentication epoch and revoke every live session."""

        object.__setattr__(self, "_auth_epoch", self._auth_epoch + 1)
        self._sessions.clear()
        return self._auth_epoch


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be datetime")
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
