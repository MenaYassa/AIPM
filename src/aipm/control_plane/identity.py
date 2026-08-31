"""Canonical identity and action-identity boundaries for MC-6.12.

This module is the single source of canonical identity for the control plane:

* ``OwnerPrincipal`` is the one authenticated principal; it is produced only by
  successful owner authentication and consumed only by authorization.
* ``ActionIdentity`` is derived exactly once per (request, plan, target state,
  policy) combination; no other component may reconstruct it from partial
  inputs.

Construction is not authentication: no tokens, headers, network, persistence,
or provider integration are accepted here, and no secret material can be
represented in any type below.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TYPE_CHECKING

from aipm.control_plane.models import (
    MAX_ACTOR_ID,
    MAX_POLICY_VERSION,
    SAFE_ID_PATTERN,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers
    from aipm.control_plane.models import ActionPlan, ActionRequest
    from aipm.control_plane.project_plan import ProjectPlan

PLAN_IDENTITY_VERSION = "mc612a-plan-v1"
ACTION_IDENTITY_VERSION = "mc612-action-identity-v1"
PRINCIPAL_IDENTITY_VERSION = "mc612-canonical-principal-v1"
EXPECTED_EFFECT = "No runtime effect; produce a bounded future-operation plan only"
OWNER_SUBJECT = "local-owner"
OWNER_ISSUER = "aipm-owner-auth"
OWNER_ROLE = "owner"
_MAX_PRINCIPAL_ID = 128
_MAX_ROLE_COUNT = 8
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class AuthenticationMethod(str, Enum):
    ARGON2ID_OWNER_PASSPHRASE = "owner_passphrase_argon2id"


class PrincipalVerification(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    EXPIRED = "expired"
    REVOKED = "revoked"


class IdentityError(ValueError):
    """Raised when a principal or action identity cannot satisfy the contract."""


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise IdentityError("Invalid identity timestamp")
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _bounded_id(value: str, name: str, *, maximum: int = _MAX_PRINCIPAL_ID) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or SAFE_ID_PATTERN.fullmatch(value) is None:
        raise IdentityError(f"Invalid {name}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise IdentityError(f"Invalid {name}")
    return value


@dataclass(frozen=True, slots=True)
class OwnerPrincipal:
    """The canonical authenticated principal of the control plane.

    Carries only identity and authorization information: stable subject,
    authentication method, verification state, bounded roles, authentication
    epoch, and validity window. Secrets, session identifiers, cookies, tokens,
    and verifier material cannot be represented here.
    """

    subject: str
    issuer: str
    authentication_method: AuthenticationMethod
    verification: PrincipalVerification
    auth_epoch: int
    authenticated_at: datetime
    expires_at: datetime
    roles: tuple[str, ...] = (OWNER_ROLE,)
    identity_version: str = PRINCIPAL_IDENTITY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "subject", _bounded_id(self.subject, "principal subject"))
        object.__setattr__(self, "issuer", _bounded_id(self.issuer, "identity issuer"))
        method = self.authentication_method if isinstance(self.authentication_method, AuthenticationMethod) else AuthenticationMethod(self.authentication_method)
        object.__setattr__(self, "authentication_method", method)
        verification = self.verification if isinstance(self.verification, PrincipalVerification) else PrincipalVerification(self.verification)
        object.__setattr__(self, "verification", verification)
        if not isinstance(self.auth_epoch, int) or self.auth_epoch < 1:
            raise IdentityError("Invalid authentication epoch")
        authenticated_at = _utc(self.authenticated_at)
        expires_at = _utc(self.expires_at)
        if expires_at <= authenticated_at:
            raise IdentityError("Identity expiry must follow authentication time")
        object.__setattr__(self, "authenticated_at", authenticated_at)
        object.__setattr__(self, "expires_at", expires_at)
        if self.identity_version != PRINCIPAL_IDENTITY_VERSION:
            raise IdentityError("Unsupported identity version")
        if len(self.roles) > _MAX_ROLE_COUNT:
            raise IdentityError("Too many principal roles")
        normalized = tuple(sorted({_bounded_id(role, "principal role") for role in self.roles}))
        object.__setattr__(self, "roles", normalized)

    def is_usable(self, now: datetime) -> bool:
        current = _utc(now)
        return self.verification is PrincipalVerification.VERIFIED and current < self.expires_at

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def canonical(self) -> str:
        return json.dumps(
            {
                "authenticated_at": self.authenticated_at.isoformat(),
                "authentication_method": self.authentication_method.value,
                "auth_epoch": self.auth_epoch,
                "expires_at": self.expires_at.isoformat(),
                "issuer": self.issuer,
                "roles": list(self.roles),
                "subject": self.subject,
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
            "authentication_method": self.authentication_method.value,
            "auth_epoch": self.auth_epoch,
            "verification": self.verification.value,
            "authenticated_at": self.authenticated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "roles": list(self.roles),
            "version": self.identity_version,
        }


@dataclass(frozen=True, slots=True)
class ActionIdentity:
    """Deterministic canonical identity of one bounded action.

    ``action_id`` binds together the plan identity (plan_id, plan_digest), the
    exact target plan revision and digest, the requester principal subject, and
    the policy version. It contains no wall-clock or random input of its own;
    the embedded plan identity carries the bounded plan window fixed at plan
    creation, so re-deriving from the same plan and target state is stable.
    """

    action_id: str
    plan_id: str
    plan_digest: str
    target_revision: int
    target_digest: str
    policy_version: str
    requester_subject: str
    operation: str
    target_id: str
    environment: str

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "environment": self.environment,
            "operation": self.operation,
            "plan_digest": self.plan_digest,
            "plan_id": self.plan_id,
            "policy_version": self.policy_version,
            "requester_subject": self.requester_subject,
            "target_digest": self.target_digest,
            "target_id": self.target_id,
            "target_revision": self.target_revision,
            "version": ACTION_IDENTITY_VERSION,
        }

    def canonical(self) -> str:
        return json.dumps(self.canonical_payload(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def safe_dict(self) -> dict[str, Any]:
        return self.canonical_payload()


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _action_identity_payload(
    *,
    operation: str,
    target_id: str,
    environment: str,
    plan_id: str,
    plan_digest: str,
    target_revision: int,
    target_digest: str,
    policy_version: str,
    requester_subject: str,
) -> dict[str, Any]:
    return {
        "operation": operation,
        "target_id": target_id,
        "environment": environment,
        "plan_id": plan_id,
        "plan_digest": plan_digest,
        "target_revision": target_revision,
        "target_digest": target_digest,
        "policy_version": policy_version,
        "requester_subject": requester_subject,
        "version": ACTION_IDENTITY_VERSION,
    }


def verify_action_identity(identity: "ActionIdentity") -> bool:
    """Verify a loaded identity against the canonical derivation algorithm.

    The single shared payload layout is reused; persistence layers call this
    instead of reimplementing the digest, so a corrupted or forged stored
    identity cannot pass as canonical.
    """

    payload = _action_identity_payload(
        operation=identity.operation,
        target_id=identity.target_id,
        environment=identity.environment,
        plan_id=identity.plan_id,
        plan_digest=identity.plan_digest,
        target_revision=identity.target_revision,
        target_digest=identity.target_digest,
        policy_version=identity.policy_version,
        requester_subject=identity.requester_subject,
    )
    expected = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return identity.action_id == expected


def derive_action_identity(
    *,
    request: "ActionRequest",
    plan: "ActionPlan",
    current_plan: "ProjectPlan",
    policy_version: str,
    requester_subject: str,
) -> ActionIdentity:
    """Derive the canonical action identity from typed values only.

    This is the ONLY authorized construction path. Every security-relevant
    input is re-verified against the typed objects themselves so a forged or
    reconstructed plan, digest, or revision cannot flow into authorization.
    """

    from aipm.control_plane.models import ActionPlan, ActionRequest, PlanState
    from aipm.control_plane.project_plan import ProjectPlan

    if not isinstance(request, ActionRequest):
        raise IdentityError("Invalid action request")
    if not isinstance(plan, ActionPlan):
        raise IdentityError("Invalid action plan")
    if not isinstance(current_plan, ProjectPlan):
        raise IdentityError("Invalid target plan state")
    if plan.request != request:
        raise IdentityError("Plan was issued for a different request")
    if plan.state is not PlanState.PLANNED:
        raise IdentityError("Plan is not in the planned state")
    if plan.digest != plan.computed_digest():
        raise IdentityError("Plan digest mismatch")
    expected_plan_id = plan_id(
        request=plan.request,
        evidence=plan.evidence,
        evidence_source=plan.evidence_source,
        risk=plan.risk,
        expected_effect=plan.expected_effect,
        created_at=plan.created_at,
        expires_at=plan.expires_at,
        state=plan.state,
    )
    if plan.plan_id != expected_plan_id:
        raise IdentityError("Plan identity mismatch")
    if current_plan.target_id != request.target_id:
        raise IdentityError("Target plan state does not match the request target")
    if current_plan.environment.value != request.environment:
        raise IdentityError("Target plan environment does not match the request")
    target_digest = current_plan.digest()
    if current_plan.canonical_digest != target_digest:
        raise IdentityError("Target plan digest mismatch")
    subject = _bounded_id(requester_subject, "requester subject", maximum=MAX_ACTOR_ID)
    version = _bounded_id(policy_version, "policy version", maximum=MAX_POLICY_VERSION)
    if not isinstance(current_plan.revision, int) or current_plan.revision < 1:
        raise IdentityError("Invalid target revision")
    payload = _action_identity_payload(
        operation=request.operation.value,
        target_id=request.target_id,
        environment=request.environment,
        plan_id=plan.plan_id,
        plan_digest=plan.digest,
        target_revision=current_plan.revision,
        target_digest=target_digest,
        policy_version=version,
        requester_subject=subject,
    )
    action_id = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    if _HEX64.fullmatch(action_id) is None:  # pragma: no cover - defensive
        raise IdentityError("Action identity derivation failed")
    return ActionIdentity(
        action_id=action_id,
        plan_id=plan.plan_id,
        plan_digest=plan.digest,
        target_revision=current_plan.revision,
        target_digest=target_digest,
        policy_version=version,
        requester_subject=subject,
        operation=request.operation.value,
        target_id=request.target_id,
        environment=request.environment,
    )


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
