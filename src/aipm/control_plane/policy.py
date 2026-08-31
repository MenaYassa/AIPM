"""The single authoritative authorization policy of the control plane.

This module produces typed, deterministic, deny-by-default decisions only. It
never executes anything: no subprocess, provider, filesystem, network, or
update-engine boundary is reachable from here. Every mutating consumer must
present an ``AuthorizationDecision`` issued by this policy.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from aipm.control_plane.identity import (
    ACTION_IDENTITY_VERSION,
    OWNER_ROLE,
    ActionIdentity,
    OwnerPrincipal,
    PrincipalVerification,
    derive_action_identity,
)
from aipm.control_plane.models import (
    DECISION_TTL,
    ActionPlan,
    ActionRequest,
    ConfirmationKind,
    OperationKind,
    RiskLevel,
)
from aipm.control_plane.project_plan import allowed_fields as plan_allowed_fields
from aipm.control_plane.project_plan import ProjectPlan

#: The bounded, closed operation allow-list. New operations require an
#: explicit, reviewed change to this constant — never caller input.
_ALLOWED_OPERATIONS = frozenset({OperationKind.UPDATE_PROJECT_PLAN, OperationKind.ROLLBACK_PROJECT_PLAN})


class PolicyCode(str, Enum):
    ALLOWED = "allowed"
    UNVERIFIED_IDENTITY = "unverified_identity"
    EXPIRED_IDENTITY = "expired_identity"
    MISSING_ROLE = "missing_role"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    TARGET_NOT_ALLOWED = "target_not_allowed"
    ENVIRONMENT_NOT_ALLOWED = "environment_not_allowed"
    SELF_APPROVAL = "self_approval"
    PLAN_MISSING = "plan_missing"
    PLAN_DISABLED = "plan_disabled"
    PLAN_EXPIRED = "plan_expired"
    PLAN_ENVIRONMENT_MISMATCH = "plan_environment_mismatch"
    FIELD_NOT_ALLOWED = "field_not_allowed"
    INVALID_REQUEST = "invalid_request"


@dataclass(frozen=True, slots=True)
class AuthorizationContext:
    """Immutable evaluation input for one authorization request."""

    principal: OwnerPrincipal | None
    request: ActionRequest
    plan: ActionPlan
    current_plan: ProjectPlan | None

    def __post_init__(self) -> None:
        if self.principal is not None and not isinstance(self.principal, OwnerPrincipal):
            raise ValueError("Invalid principal")
        if not isinstance(self.request, ActionRequest):
            raise ValueError("Invalid action request")
        if not isinstance(self.plan, ActionPlan):
            raise ValueError("Invalid action plan")
        if self.current_plan is not None and not isinstance(self.current_plan, ProjectPlan):
            raise ValueError("Invalid target plan state")


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """Safe deterministic policy result; it has no execution authority.

    An allowed decision binds the canonical action identity, the exact target
    plan revision and digest, the policy version, and an explicit confirmation
    requirement with a bounded validity window. A denied decision carries no
    identity and no plan binding.
    """

    decision_id: str
    allowed: bool
    code: PolicyCode
    operation: OperationKind | None
    target_id: str | None
    environment: str | None
    policy_version: str
    principal_subject: str | None
    confirmation_required: bool
    decided_at: datetime
    expires_at: datetime
    action_identity: ActionIdentity | None = None
    plan_revision: int | None = None
    plan_digest: str | None = None
    confirmation_kind: ConfirmationKind = ConfirmationKind.OWNER_CONFIRMATION
    request: ActionRequest | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, PolicyCode):
            raise ValueError("Invalid policy code")
        if not isinstance(self.decided_at, datetime) or not isinstance(self.expires_at, datetime):
            raise ValueError("Invalid decision timestamps")
        if self.expires_at < self.decided_at:
            raise ValueError("Decision expiry must follow the decision time")
        if self.allowed != (self.code is PolicyCode.ALLOWED):
            raise ValueError("Decision code and outcome disagree")
        if self.allowed:
            if self.action_identity is None or self.plan_revision is None or self.plan_digest is None:
                raise ValueError("Allowed decisions must bind the action identity")
            if not self.confirmation_required:
                raise ValueError("Allowed decisions require explicit confirmation")
            if not isinstance(self.request, ActionRequest):
                raise ValueError("Allowed decisions must carry the exact request")

    def is_expired(self, now: datetime) -> bool:
        current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        return current >= self.expires_at

    def safe_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "allowed": self.allowed,
            "code": self.code.value,
            "operation": self.operation.value if self.operation else None,
            "target_id": self.target_id,
            "environment": self.environment,
            "policy_version": self.policy_version,
            "principal_subject": self.principal_subject,
            "confirmation_required": self.confirmation_required,
            "decided_at": self.decided_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "action_identity": self.action_identity.safe_dict() if self.action_identity else None,
            "plan_revision": self.plan_revision,
            "plan_digest": self.plan_digest,
            "confirmation_kind": self.confirmation_kind.value,
            "request": self.request.canonical() if self.request else None,
        }


@dataclass(frozen=True, slots=True)
class AuthorizationPolicy:
    """Deny-by-default canonical policy for the bounded operation allow-list.

    ``confirmation_kind`` selects the explicit confirmation semantics:
    OWNER_CONFIRMATION (implemented single-owner path) or DISTINCT_APPROVAL
    (reserved future mode requiring a different authenticated subject).
    """

    policy_version: str
    allowed_scopes: frozenset[tuple[str, str]]
    allowed_operations: frozenset[OperationKind] = _ALLOWED_OPERATIONS
    require_distinct_requester_approver: bool = True
    required_role: str = OWNER_ROLE
    allowed_fields: frozenset[str] = plan_allowed_fields()
    confirmation_kind: ConfirmationKind = ConfirmationKind.OWNER_CONFIRMATION

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not self.policy_version or len(self.policy_version) > 64:
            raise ValueError("Invalid policy version")
        scopes: set[tuple[str, str]] = set()
        for target_id, environment in self.allowed_scopes:
            if not isinstance(target_id, str) or not isinstance(environment, str) or not target_id or not environment:
                raise ValueError("Invalid authorization scope")
            scopes.add((target_id, environment))
        object.__setattr__(self, "allowed_scopes", frozenset(scopes))
        object.__setattr__(self, "allowed_operations", frozenset(self.allowed_operations))
        if self.allowed_operations != _ALLOWED_OPERATIONS:
            raise ValueError("Policy must retain the bounded operation allow-list")
        if self.require_distinct_requester_approver is not True:
            raise ValueError("Distinct-subject separation cannot be disabled")
        if not isinstance(self.required_role, str) or not self.required_role or len(self.required_role) > 128:
            raise ValueError("Invalid required role")
        if not isinstance(self.allowed_fields, frozenset) or not self.allowed_fields:
            raise ValueError("An explicit field allow-list is required")
        kind = self.confirmation_kind if isinstance(self.confirmation_kind, ConfirmationKind) else ConfirmationKind(self.confirmation_kind)
        object.__setattr__(self, "confirmation_kind", kind)

    def authorize(
        self,
        principal: OwnerPrincipal | None,
        request: ActionRequest,
        plan: ActionPlan,
        current_plan: ProjectPlan | None,
        *,
        now: datetime | None = None,
    ) -> AuthorizationDecision:
        """Evaluate one bounded action request and return the typed decision."""

        decided_at = self._utc(now) if now is not None else None
        try:
            context = AuthorizationContext(
                principal=principal,
                request=request,
                plan=plan,
                current_plan=current_plan,
            )
        except (TypeError, ValueError):
            return self._deny(PolicyCode.INVALID_REQUEST, request, decided_at)
        return self._evaluate(context, decided_at)

    def evaluate(self, context: AuthorizationContext, *, now: datetime | None = None) -> AuthorizationDecision:
        return self._evaluate(context, self._utc(now) if now is not None else None)

    def _evaluate(self, context: AuthorizationContext, decided_at: datetime | None) -> AuthorizationDecision:
        request = context.request
        if request.operation not in self.allowed_operations:
            return self._deny(PolicyCode.UNSUPPORTED_OPERATION, request, decided_at)
        principal = context.principal
        if principal is None or principal.verification is not PrincipalVerification.VERIFIED:
            return self._deny(PolicyCode.UNVERIFIED_IDENTITY, request, decided_at, principal)
        current = decided_at or principal.authenticated_at
        if not principal.is_usable(current):
            code = PolicyCode.EXPIRED_IDENTITY if current >= principal.expires_at else PolicyCode.UNVERIFIED_IDENTITY
            return self._deny(code, request, decided_at, principal)
        if not principal.has_role(self.required_role):
            return self._deny(PolicyCode.MISSING_ROLE, request, decided_at, principal)
        target_id, environment = request.target_id, request.environment
        if (target_id, environment) not in self.allowed_scopes:
            code = PolicyCode.ENVIRONMENT_NOT_ALLOWED if any(target == target_id for target, _env in self.allowed_scopes) else PolicyCode.TARGET_NOT_ALLOWED
            return self._deny(code, request, decided_at, principal)
        current_plan = context.current_plan
        if current_plan is None:
            return self._deny(PolicyCode.PLAN_MISSING, request, decided_at, principal)
        if not current_plan.enabled:
            return self._deny(PolicyCode.PLAN_DISABLED, request, decided_at, principal)
        if current_plan.environment.value != environment:
            return self._deny(PolicyCode.PLAN_ENVIRONMENT_MISMATCH, request, decided_at, principal)
        plan = context.plan
        if plan.is_expired(decided_at or plan.created_at):
            return self._deny(PolicyCode.PLAN_EXPIRED, request, decided_at, principal)
        if request.operation is OperationKind.UPDATE_PROJECT_PLAN:
            if not request.fields or not request.fields.issubset(self.allowed_fields):
                return self._deny(PolicyCode.FIELD_NOT_ALLOWED, request, decided_at, principal)
        elif request.operation is OperationKind.ROLLBACK_PROJECT_PLAN:
            # Rollback restores the immutable snapshot; it carries no new
            # mutation fields of its own.
            if request.fields:
                return self._deny(PolicyCode.FIELD_NOT_ALLOWED, request, decided_at, principal)
        else:  # pragma: no cover - closed allow-list makes this unreachable
            return self._deny(PolicyCode.UNSUPPORTED_OPERATION, request, decided_at, principal)
        try:
            identity = derive_action_identity(
                request=request,
                plan=plan,
                current_plan=current_plan,
                policy_version=self.policy_version,
                requester_subject=principal.subject,
            )
        except (TypeError, ValueError):
            return self._deny(PolicyCode.INVALID_REQUEST, request, decided_at, principal)
        moment = decided_at or plan.created_at
        return self._allow(identity, principal, moment, plan.expires_at, request)

    def _allow(self, identity: ActionIdentity, principal: OwnerPrincipal, moment: datetime, plan_expires_at: datetime, request: ActionRequest) -> AuthorizationDecision:
        decision = AuthorizationDecision(
            decision_id="",
            allowed=True,
            code=PolicyCode.ALLOWED,
            operation=OperationKind(identity.operation),
            target_id=identity.target_id,
            environment=identity.environment,
            policy_version=self.policy_version,
            principal_subject=principal.subject,
            confirmation_required=True,
            decided_at=moment,
            expires_at=min(moment + DECISION_TTL, plan_expires_at),
            action_identity=identity,
            plan_revision=identity.target_revision,
            plan_digest=identity.plan_digest,
            confirmation_kind=self.confirmation_kind,
            request=request,
        )
        object.__setattr__(decision, "decision_id", self._decision_id(decision))
        return decision

    def _deny(
        self,
        code: PolicyCode,
        request: ActionRequest,
        decided_at: datetime | None,
        principal: OwnerPrincipal | None = None,
    ) -> AuthorizationDecision:
        moment = decided_at or (principal.authenticated_at if principal else datetime.min.replace(tzinfo=timezone.utc))
        denial = AuthorizationDecision(
            decision_id="",
            allowed=False,
            code=code,
            operation=request.operation if isinstance(request, ActionRequest) else None,
            target_id=request.target_id if isinstance(request, ActionRequest) else None,
            environment=request.environment if isinstance(request, ActionRequest) else None,
            policy_version=self.policy_version,
            principal_subject=principal.subject if principal else None,
            confirmation_required=False,
            decided_at=moment,
            expires_at=moment,
            confirmation_kind=self.confirmation_kind,
        )
        object.__setattr__(denial, "decision_id", self._decision_id(denial, request=request))
        return denial

    @staticmethod
    def _decision_id(decision: AuthorizationDecision, *, request: ActionRequest | None = None) -> str:
        payload: dict[str, Any] = {
            "allowed": decision.allowed,
            "code": decision.code.value,
            "operation": decision.operation.value if decision.operation else None,
            "target_id": decision.target_id,
            "environment": decision.environment,
            "policy_version": decision.policy_version,
            "principal_subject": decision.principal_subject,
            "confirmation_required": decision.confirmation_required,
            "confirmation_kind": decision.confirmation_kind.value,
            "identity_version": ACTION_IDENTITY_VERSION,
            "action_identity": decision.action_identity.canonical_payload() if decision.action_identity else None,
            "request_identity": hashlib.sha256(request.canonical().encode("utf-8")).hexdigest() if request is not None else None,
        }
        canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def validate_operation(operation: OperationKind) -> None:
    if not isinstance(operation, OperationKind) or operation not in _ALLOWED_OPERATIONS:
        raise ValueError("unsupported operation")


def risk_for(operation: OperationKind) -> RiskLevel:
    validate_operation(operation)
    return RiskLevel.LOW


def allowed_operations() -> frozenset[OperationKind]:
    return _ALLOWED_OPERATIONS
