"""Canonical single-owner authentication boundary.

This module has no HTTP, provider, persistence, filesystem, or runtime-control
integration. The verifier accepts an Argon2id PHC string and fails closed when
the local Argon2 implementation is unavailable or malformed.

Successful authentication produces the canonical ``OwnerPrincipal``; the owner
secret is consumed here and never leaves this boundary in any result,
exception, session, or record. An authentication epoch is maintained so
credential rotation can globally revoke previously issued principals.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable

from aipm.control_plane.identity import (
    OWNER_ISSUER,
    OWNER_ROLE,
    OWNER_SUBJECT,
    AuthenticationMethod,
    OwnerPrincipal,
    PrincipalVerification,
)

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
    principal: OwnerPrincipal | None = None


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
    """Single-owner authentication producing the canonical principal.

    Authentication failures are bounded by progressive cooldown and a finite
    lockout. Successful authentication never retains the secret; the returned
    principal carries only identity material.
    """

    __slots__ = (
        "_verifier",
        "_clock",
        "_failure_count",
        "_cooldown_until",
        "_locked_until",
        "_max_failures_before_lock",
        "_lockout_seconds",
        "_principal_lifetime",
        "_auth_epoch",
        "_subject",
        "_issuer",
        "_initialized",
    )

    def __init__(
        self,
        verifier: Argon2idVerifier,
        *,
        clock: Callable[[], datetime] | None = None,
        max_failures_before_lock: int = 5,
        lockout_seconds: int = 60,
        principal_lifetime: timedelta = timedelta(minutes=30),
        subject: str = OWNER_SUBJECT,
        issuer: str = OWNER_ISSUER,
        auth_epoch: int = 1,
    ) -> None:
        if not isinstance(verifier, Argon2idVerifier):
            raise TypeError("verifier must be Argon2idVerifier")
        if not isinstance(max_failures_before_lock, int) or max_failures_before_lock < 1:
            raise ValueError("invalid failure limit")
        if not isinstance(lockout_seconds, int) or lockout_seconds < 1:
            raise ValueError("invalid lockout duration")
        if principal_lifetime <= timedelta(0):
            raise ValueError("invalid principal lifetime")
        if not isinstance(auth_epoch, int) or auth_epoch < 1:
            raise ValueError("invalid authentication epoch")
        object.__setattr__(self, "_verifier", verifier)
        object.__setattr__(self, "_clock", clock or (lambda: datetime.now(timezone.utc)))
        object.__setattr__(self, "_failure_count", 0)
        object.__setattr__(self, "_cooldown_until", None)
        object.__setattr__(self, "_locked_until", None)
        object.__setattr__(self, "_max_failures_before_lock", max_failures_before_lock)
        object.__setattr__(self, "_lockout_seconds", lockout_seconds)
        object.__setattr__(self, "_principal_lifetime", principal_lifetime)
        object.__setattr__(self, "_auth_epoch", auth_epoch)
        object.__setattr__(self, "_subject", subject)
        object.__setattr__(self, "_issuer", issuer)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("OwnerAuthenticator configuration is immutable")
        object.__setattr__(self, name, value)

    @property
    def auth_epoch(self) -> int:
        return self._auth_epoch

    def rotate_auth_epoch(self) -> int:
        """Advance the authentication epoch (credential rotation boundary)."""

        object.__setattr__(self, "_auth_epoch", self._auth_epoch + 1)
        return self._auth_epoch

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
            return AuthenticationResult(
                True,
                FailureReason.ACCEPTED,
                principal=self._principal(current),
            )
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

    def _principal(self, current: datetime) -> OwnerPrincipal:
        return OwnerPrincipal(
            subject=self._subject,
            issuer=self._issuer,
            authentication_method=AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE,
            verification=PrincipalVerification.VERIFIED,
            auth_epoch=self._auth_epoch,
            authenticated_at=current,
            expires_at=current + self._principal_lifetime,
            roles=(OWNER_ROLE,),
        )

    def _utc(self, value: datetime) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError("now must be datetime")
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _remaining(end: datetime, start: datetime) -> int:
        return max(1, int((end - start).total_seconds()))
