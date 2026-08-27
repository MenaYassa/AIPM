"""Pure identity and canonical plan-identity boundaries for MC-6.12.

This module models a verified principal without authenticating a provider. It is
serialization/conformance only: no tokens, headers, network, persistence, or
provider integration are accepted here.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

PLAN_IDENTITY_VERSION = "mc612a-plan-v1"
PRINCIPAL_IDENTITY_VERSION = "mc612-stage2-principal-v1"
EXPECTED_EFFECT = "No runtime effect; produce a bounded future-operation plan only"
_MAX_PRINCIPAL_ID = 128
_MAX_ISSUER = 128
_MAX_TENANT = 128
_MAX_ATTRIBUTE_COUNT = 8
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")


class PrincipalVerification(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    EXPIRED = "expired"
    REVOKED = "revoked"


class IdentityError(ValueError):
    """Raised when a principal cannot satisfy the pure identity contract."""


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise IdentityError("Invalid identity timestamp")
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _bounded_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_PRINCIPAL_ID or _SAFE_ID.fullmatch(value) is None:
        raise IdentityError(f"Invalid {name}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise IdentityError(f"Invalid {name}")
    return value


@dataclass(frozen=True, slots=True)
class VerifiedPrincipal:
    """Provider-neutral identity assertion; construction is not authentication."""

    subject: str
    issuer: str
    tenant: str
    verification: PrincipalVerification
    authenticated_at: datetime
    expires_at: datetime
    roles: tuple[str, ...] = ()
    identity_version: str = PRINCIPAL_IDENTITY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "subject", _bounded_id(self.subject, "principal subject"))
        object.__setattr__(self, "issuer", _bounded_id(self.issuer, "identity issuer"))
        object.__setattr__(self, "tenant", _bounded_id(self.tenant, "identity tenant"))
        verification = self.verification if isinstance(self.verification, PrincipalVerification) else PrincipalVerification(self.verification)
        object.__setattr__(self, "verification", verification)
        authenticated_at = _utc(self.authenticated_at)
        expires_at = _utc(self.expires_at)
        if expires_at <= authenticated_at:
            raise IdentityError("Identity expiry must follow authentication time")
        object.__setattr__(self, "authenticated_at", authenticated_at)
        object.__setattr__(self, "expires_at", expires_at)
        if self.identity_version != PRINCIPAL_IDENTITY_VERSION:
            raise IdentityError("Unsupported identity version")
        if len(self.roles) > _MAX_ATTRIBUTE_COUNT:
            raise IdentityError("Too many principal roles")
        normalized = tuple(sorted({_bounded_id(role, "principal role") for role in self.roles}))
        object.__setattr__(self, "roles", normalized)

    def is_usable(self, now: datetime) -> bool:
        current = _utc(now)
        return self.verification is PrincipalVerification.VERIFIED and current < self.expires_at

    def canonical(self) -> str:
        return json.dumps(
            {
                "authenticated_at": self.authenticated_at.isoformat(),
                "expires_at": self.expires_at.isoformat(),
                "issuer": self.issuer,
                "roles": list(self.roles),
                "subject": self.subject,
                "tenant": self.tenant,
                "verification": self.verification.value,
                "version": self.identity_version,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "issuer": self.issuer,
            "tenant": self.tenant,
            "verification": self.verification.value,
            "authenticated_at": self.authenticated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "roles": list(self.roles),
            "version": self.identity_version,
        }


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def request_identity(request: Any) -> str:
    return hashlib.sha256(request.canonical().encode("utf-8")).hexdigest()


def plan_id_payload(*, request: Any, evidence: Any, evidence_source: Any, risk: Any,
                    expected_effect: str, created_at: datetime, expires_at: datetime,
                    state: Any) -> dict[str, Any]:
    return {
        "request_identity": request_identity(request),
        "operation": request.operation.value,
        "target_id": request.target_id,
        "evidence_source": evidence_source.value,
        "evidence_state": evidence.state.value,
        "evidence": evidence.canonical(),
        "risk": risk.value,
        "expected_effect": expected_effect,
        "created_at": _utc(created_at).isoformat(),
        "expires_at": _utc(expires_at).isoformat(),
        "state": state.value,
    }


def plan_id(*, request: Any, evidence: Any, evidence_source: Any, risk: Any,
            expected_effect: str, created_at: datetime, expires_at: datetime,
            state: Any) -> str:
    return hashlib.sha256(_canonical_json(plan_id_payload(
        request=request, evidence=evidence, evidence_source=evidence_source,
        risk=risk, expected_effect=expected_effect, created_at=created_at,
        expires_at=expires_at, state=state,
    )).encode("utf-8")).hexdigest()[:32]


def canonical_plan_payload(plan: Any) -> dict[str, Any]:
    return plan.canonical_payload()


def canonical_plan_bytes(plan: Any) -> bytes:
    return _canonical_json(canonical_plan_payload(plan)).encode("utf-8")


def plan_digest(plan: Any) -> str:
    return hashlib.sha256(canonical_plan_bytes(plan)).hexdigest()


def identity_vector(plan: Any) -> dict[str, str]:
    return {
        "version": PLAN_IDENTITY_VERSION,
        "request_identity": request_identity(plan.request),
        "plan_id": plan.plan_id,
        "digest": plan_digest(plan),
    }
