"""Staging-only single-owner authentication and authorization contracts.

This module has no HTTP, provider, persistence, filesystem, or runtime-control
integration. The verifier accepts an Argon2id PHC string and fails closed when
the local Argon2 implementation is unavailable or malformed.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable

from aipm.control_plane.models import OperationKind
from aipm.control_plane.project_plan import Environment
from aipm.control_plane.session import OwnerSession

_ARGON2ID_RE = re.compile(
    r"^\$argon2id\$v=19\$m=(?:[1-9][0-9]{0,8}),t=(?:[1-9][0-9]{0,3}),p=(?:[1-9][0-9]{0,2})\$[^$]+\$[^$]+$"
)


class AuthenticationError(ValueError):
    """Raised only for invalid verifier configuration, never for bad secrets."""


class FailureReason(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COOLDOWN = "cooldown"
    LOCKED = "locked"


@dataclass(frozen=True, slots=True)
class AuthenticationResult:
    accepted: bool
    reason: FailureReason
    retry_after_seconds: int = 0


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    code: str
    operation: OperationKind
    target_id: str
    environment: Environment
    fields: tuple[str, ...]


class Argon2idVerifier:
    """Provider-neutral Argon2id PHC verifier with fail-closed behavior."""

    __slots__ = ("_encoded",)

    def __init__(self, encoded_verifier: str) -> None:
        if not isinstance(encoded_verifier, str) or _ARGON2ID_RE.fullmatch(encoded_verifier) is None:
            raise AuthenticationError("Invalid Argon2id verifier")
        object.__setattr__(self, "_encoded", encoded_verifier)

    @property
    def encoded_verifier(self) -> str:
        return self._encoded

    def verify(self, secret: str | bytes) -> bool:
        if isinstance(secret, str):
            password = secret.encode("utf-8")
        elif isinstance(secret, bytes):
            password = secret
        else:
            return False
        if not password or b"\x00" in password:
            return False
        try:
            library_name = ctypes.util.find_library("argon2")
            if not library_name:
                return False
            library = ctypes.CDLL(library_name)
            verify = library.argon2id_verify
            verify.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t]
            verify.restype = ctypes.c_int
            return verify(self._encoded.encode("ascii"), password, len(password)) == 0
        except (AttributeError, OSError, UnicodeError, TypeError):
            return False


class OwnerAuthenticator:
    """Single-owner in-memory authentication limiter for staging."""

    __slots__ = ("_verifier", "_clock", "_failure_count", "_cooldown_until", "_locked_until", "_max_failures_before_lock", "_lockout_seconds", "_initialized")

    def __init__(
        self,
        verifier: Argon2idVerifier,
        *,
        clock: Callable[[], datetime] | None = None,
        max_failures_before_lock: int = 5,
        lockout_seconds: int = 60,
    ) -> None:
        if not isinstance(verifier, Argon2idVerifier):
            raise TypeError("verifier must be Argon2idVerifier")
        if not isinstance(max_failures_before_lock, int) or max_failures_before_lock < 1:
            raise ValueError("invalid failure limit")
        if not isinstance(lockout_seconds, int) or lockout_seconds < 1:
            raise ValueError("invalid lockout duration")
        object.__setattr__(self, "_verifier", verifier)
        object.__setattr__(self, "_clock", clock or (lambda: datetime.now(timezone.utc)))
        object.__setattr__(self, "_failure_count", 0)
        object.__setattr__(self, "_cooldown_until", None)
        object.__setattr__(self, "_locked_until", None)
        object.__setattr__(self, "_max_failures_before_lock", max_failures_before_lock)
        object.__setattr__(self, "_lockout_seconds", lockout_seconds)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("OwnerAuthenticator configuration is immutable")
        object.__setattr__(self, name, value)

    def authenticate(self, secret: str | bytes, *, now: datetime | None = None) -> AuthenticationResult:
        current = self._utc(now or self._clock())
        locked_until = self._locked_until
        if locked_until is not None and current < locked_until:
            return AuthenticationResult(False, FailureReason.LOCKED, self._remaining(locked_until, current))
        cooldown_until = self._cooldown_until
        if cooldown_until is not None and current < cooldown_until:
            return AuthenticationResult(False, FailureReason.COOLDOWN, self._remaining(cooldown_until, current))
        if self._verifier.verify(secret):
            object.__setattr__(self, "_failure_count", 0)
            object.__setattr__(self, "_cooldown_until", None)
            object.__setattr__(self, "_locked_until", None)
            return AuthenticationResult(True, FailureReason.ACCEPTED)
        failures = self._failure_count + 1
        object.__setattr__(self, "_failure_count", failures)
        if failures >= self._max_failures_before_lock:
            locked_until = current + timedelta(seconds=self._lockout_seconds)
            object.__setattr__(self, "_locked_until", locked_until)
            object.__setattr__(self, "_cooldown_until", None)
            return AuthenticationResult(False, FailureReason.LOCKED, self._lockout_seconds)
        cooldown_seconds = min(30, 2 ** (failures - 1))
        cooldown_until = current + timedelta(seconds=cooldown_seconds)
        object.__setattr__(self, "_cooldown_until", cooldown_until)
        return AuthenticationResult(False, FailureReason.REJECTED, cooldown_seconds)

    def _utc(self, value: datetime) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError("now must be datetime")
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _remaining(end: datetime, start: datetime) -> int:
        return max(1, int((end - start).total_seconds()))


class SingleOwnerActionGuard:
    """Pure staging-only authorization for the one permitted operation."""

    __slots__ = ("_target_ids", "_initialized")

    def __init__(self, *, target_ids: set[str] | frozenset[str]) -> None:
        targets = frozenset(target_ids)
        if not targets or any(not isinstance(target, str) or not target for target in targets):
            raise ValueError("explicit target allow-list is required")
        object.__setattr__(self, "_target_ids", targets)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("action guard configuration is immutable")
        object.__setattr__(self, name, value)

    def authorize(
        self,
        session: OwnerSession | None,
        *,
        operation: OperationKind,
        target_id: str,
        environment: Environment,
        fields: tuple[str, ...] | list[str] | set[str],
    ) -> AuthorizationDecision:
        normalized_fields = tuple(sorted(fields)) if isinstance(fields, (tuple, list, set, frozenset)) else ()
        if not isinstance(session, OwnerSession) or not session.authenticated:
            return AuthorizationDecision(False, "DENY_NO_OWNER_SESSION", operation, target_id, environment, normalized_fields)
        if operation is not OperationKind.UPDATE_PROJECT_PLAN:
            return AuthorizationDecision(False, "DENY_OPERATION", operation, target_id, environment, normalized_fields)
        if environment is not Environment.STAGING:
            return AuthorizationDecision(False, "DENY_PRODUCTION", operation, target_id, environment, normalized_fields)
        if target_id not in self._target_ids:
            return AuthorizationDecision(False, "DENY_TARGET", operation, target_id, environment, normalized_fields)
        if not normalized_fields or any(field not in {"title", "objective"} for field in normalized_fields):
            return AuthorizationDecision(False, "DENY_FIELD_SET", operation, target_id, environment, normalized_fields)
        return AuthorizationDecision(True, "ALLOWED", operation, target_id, environment, normalized_fields)
