from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from aipm.control_plane.identity import PrincipalVerification, VerifiedPrincipal
from aipm.control_plane.models import ActionScope, ActorRole, OperationKind, RiskLevel

_ALLOWED_OPERATIONS = frozenset({OperationKind.UPDATE_PROJECT_PLAN})


class PolicyCode(str, Enum):
    ALLOWED = "allowed"
    UNVERIFIED_IDENTITY = "unverified_identity"
    EXPIRED_IDENTITY = "expired_identity"
    MISSING_ROLE = "missing_role"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    TARGET_NOT_ALLOWED = "target_not_allowed"
    ENVIRONMENT_NOT_ALLOWED = "environment_not_allowed"
    SELF_APPROVAL = "self_approval"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Safe deterministic policy result; it has no execution authority."""

    allowed: bool
    code: PolicyCode
    operation: OperationKind | None
    scope: ActionScope | None
    actor_subject: str | None
    actor_role: ActorRole | None
    policy_version: str

    def safe_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code.value,
            "operation": self.operation.value if self.operation else None,
            "scope": self.scope.canonical() if self.scope else None,
            "actor_subject": self.actor_subject,
            "actor_role": self.actor_role.value if self.actor_role else None,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True, slots=True)
class AuthorizationPolicy:
    """Deny-by-default Stage 2 policy model for one bounded operation."""

    policy_version: str
    allowed_scopes: frozenset[tuple[str, str]]
    allowed_operations: frozenset[OperationKind] = _ALLOWED_OPERATIONS
    require_distinct_requester_approver: bool = True

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
            raise ValueError("Stage 2 policy must retain the single operation allow-list")
        if self.require_distinct_requester_approver is not True:
            raise ValueError("Requester and approver separation is required")

    def evaluate(
        self,
        principal: VerifiedPrincipal | None,
        *,
        actor_role: ActorRole,
        operation: OperationKind,
        target_id: str,
        environment: str,
        requester_subject: str | None = None,
        approver_subject: str | None = None,
        now: datetime | None = None,
    ) -> PolicyDecision:
        scope = None
        try:
            scope = ActionScope(target_id=target_id, environment=environment, policy_version=self.policy_version)
        except ValueError:
            operation_value = operation if isinstance(operation, OperationKind) else None
            role_value = actor_role if isinstance(actor_role, ActorRole) else None
            return self._deny(PolicyCode.TARGET_NOT_ALLOWED, operation_value, principal, role_value, None)
        operation_value = operation if isinstance(operation, OperationKind) else None
        role_value = actor_role if isinstance(actor_role, ActorRole) else None
        if operation_value not in self.allowed_operations:
            return self._deny(PolicyCode.UNSUPPORTED_OPERATION, operation_value, principal, role_value, scope)
        if principal is None or principal.verification is not PrincipalVerification.VERIFIED:
            return self._deny(PolicyCode.UNVERIFIED_IDENTITY, operation_value, principal, role_value, scope)
        current = now or principal.authenticated_at
        if not principal.is_usable(current):
            code = PolicyCode.EXPIRED_IDENTITY if current >= principal.expires_at else PolicyCode.UNVERIFIED_IDENTITY
            return self._deny(code, operation_value, principal, role_value, scope)
        if role_value is None or role_value.value not in principal.roles:
            return self._deny(PolicyCode.MISSING_ROLE, operation_value, principal, role_value, scope)
        if (target_id, environment) not in self.allowed_scopes:
            code = PolicyCode.ENVIRONMENT_NOT_ALLOWED if any(target == target_id for target, _env in self.allowed_scopes) else PolicyCode.TARGET_NOT_ALLOWED
            return self._deny(code, operation_value, principal, actor_role, scope)
        if self.require_distinct_requester_approver and requester_subject and approver_subject and requester_subject == approver_subject:
            return self._deny(PolicyCode.SELF_APPROVAL, operation_value, principal, actor_role, scope)
        return PolicyDecision(True, PolicyCode.ALLOWED, operation_value, scope, principal.subject, actor_role, self.policy_version)

    def _deny(self, code, operation, principal, actor_role, scope) -> PolicyDecision:
        return PolicyDecision(
            allowed=False,
            code=code,
            operation=operation,
            scope=scope,
            actor_subject=principal.subject if principal else None,
            actor_role=actor_role if isinstance(actor_role, ActorRole) else None,
            policy_version=self.policy_version,
        )


def validate_operation(operation: OperationKind) -> None:
    if not isinstance(operation, OperationKind) or operation not in _ALLOWED_OPERATIONS:
        raise ValueError("unsupported operation")


def risk_for(operation: OperationKind) -> RiskLevel:
    validate_operation(operation)
    return RiskLevel.LOW


def allowed_operations() -> frozenset[OperationKind]:
    return _ALLOWED_OPERATIONS
