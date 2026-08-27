from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping


MAX_IDEMPOTENCY_KEY = 128
MAX_TARGET_ID = 128
MAX_ACTOR_ID = 128
MAX_ACTION_METADATA = 8
MAX_METADATA_KEY = 64
MAX_METADATA_VALUE = 128
MAX_EVIDENCE_ITEMS = 16
MAX_EVIDENCE_VALUE = 256
PLAN_TTL = timedelta(minutes=15)
APPROVAL_TTL = timedelta(minutes=10)
_SAFE_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_SAFE_VALUE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
_SENSITIVE_MARKERS = ("/", "\\\\", "token=", "password=", "secret", "credential", "authorization", "traceback", "exception=", "provider", "destination")


class OperationKind(str, Enum):
    UPDATE_PROJECT_PLAN = "update_project_plan"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EvidenceState(str, Enum):
    OBSERVED = "observed"
    UNAVAILABLE = "unavailable"
    NOT_OBSERVED = "not_observed"
    STALE = "stale"
    INVALID = "invalid"


class PlanState(str, Enum):
    PLANNED = "planned"
    INVALID = "invalid"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class ApprovalState(str, Enum):
    APPROVAL_REQUESTED = "approval_requested"
    APPROVED = "approved"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class AuditState(str, Enum):
    PLANNED = "planned"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVED = "approved"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class PlanningErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    UNAVAILABLE_TARGET = "unavailable_target"
    UNAVAILABLE_EVIDENCE = "unavailable_evidence"
    STALE_EVIDENCE = "stale_evidence"
    INVALID_PLAN = "invalid_plan"
    EXPIRED_PLAN = "expired_plan"
    APPROVAL_MISMATCH = "approval_mismatch"


class ControlPlaneError(ValueError):
    """Safe typed plan-only control-plane error."""

    def __init__(self, code: PlanningErrorCode, message: str = "Control-plane request rejected") -> None:
        self.code = code
        super().__init__(message[:256])


def _bounded_string(value: Any, *, name: str, maximum: int, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, f"Invalid {name}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, f"Invalid {name}")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, f"Invalid {name}")
    return value


def _safe_bounded_text(value: Any, *, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, f"Invalid {name}")
    text = _bounded_string(unicodedata.normalize("NFC", value), name=name, maximum=maximum)
    lowered = text.casefold()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, f"Unsafe {name}")
    return text


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid timestamp")
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class ActionRequest:
    operation: OperationKind
    target_id: str
    idempotency_key: str
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        operation = self.operation if isinstance(self.operation, OperationKind) else OperationKind(self.operation)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "target_id", _bounded_string(self.target_id, name="target identity", maximum=MAX_TARGET_ID, pattern=_SAFE_ID))
        object.__setattr__(self, "idempotency_key", _bounded_string(self.idempotency_key, name="idempotency key", maximum=MAX_IDEMPOTENCY_KEY, pattern=_SAFE_ID))
        if len(self.metadata) > MAX_ACTION_METADATA:
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Too many request metadata fields")
        normalized: list[tuple[str, str]] = []
        for key, value in self.metadata:
            key = _bounded_string(key, name="metadata key", maximum=MAX_METADATA_KEY, pattern=_SAFE_KEY)
            value = _safe_bounded_text(value, name="metadata value", maximum=MAX_METADATA_VALUE)
            if key in {"path", "command", "url", "token", "secret", "password", "provider"}:
                raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Unsafe request metadata")
            normalized.append((key, value))
        if len({key for key, _value in normalized}) != len(normalized):
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Duplicate request metadata key")
        object.__setattr__(self, "metadata", tuple(sorted(normalized)))

    def canonical(self) -> str:
        payload = {
            "operation": self.operation.value,
            "target_id": self.target_id,
            "idempotency_key": self.idempotency_key,
            "metadata": [[key, value] for key, value in self.metadata],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class EvidenceSummary:
    state: EvidenceState
    items: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        state = self.state if isinstance(self.state, EvidenceState) else EvidenceState(self.state)
        object.__setattr__(self, "state", state)
        if len(self.items) > MAX_EVIDENCE_ITEMS:
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Too much planning evidence")
        normalized: list[tuple[str, str]] = []
        for key, value in self.items:
            key = _bounded_string(key, name="evidence key", maximum=MAX_METADATA_KEY, pattern=_SAFE_KEY)
            value = _safe_bounded_text(value, name="evidence value", maximum=MAX_EVIDENCE_VALUE)
            normalized.append((key, value))
        object.__setattr__(self, "items", tuple(sorted(normalized)))

    def canonical(self) -> list[list[str]]:
        return [[key, value] for key, value in self.items]


class EvidenceSource(str, Enum):
    NONE = "none"
    MISSION_CONTROL_OBSERVATION = "mission_control_observation"


@dataclass(frozen=True, slots=True)
class ActionPlan:
    plan_id: str
    request: ActionRequest
    risk: RiskLevel
    evidence: EvidenceSummary
    expected_effect: str
    expires_at: datetime
    created_at: datetime
    evidence_source: EvidenceSource = EvidenceSource.NONE
    state: PlanState = PlanState.PLANNED
    digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _bounded_string(self.plan_id, name="plan ID", maximum=128, pattern=_SAFE_ID))
        source = self.evidence_source if isinstance(self.evidence_source, EvidenceSource) else EvidenceSource(self.evidence_source)
        object.__setattr__(self, "evidence_source", source)
        object.__setattr__(self, "expected_effect", _safe_bounded_text(self.expected_effect, name="expected effect", maximum=MAX_EVIDENCE_VALUE))
        created = _utc(self.created_at)
        expires = _utc(self.expires_at)
        if expires <= created or expires - created > PLAN_TTL:
            raise ControlPlaneError(PlanningErrorCode.INVALID_PLAN, "Invalid plan expiry")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        risk = self.risk if isinstance(self.risk, RiskLevel) else RiskLevel(self.risk)
        state = self.state if isinstance(self.state, PlanState) else PlanState(self.state)
        object.__setattr__(self, "risk", risk)
        object.__setattr__(self, "state", state)
        if self.digest:
            if len(self.digest) != 64 or self.digest != self.computed_digest():
                raise ControlPlaneError(PlanningErrorCode.INVALID_PLAN, "Invalid plan digest")

    @property
    def request_identity(self) -> str:
        return hashlib.sha256(self.request.canonical().encode("utf-8")).hexdigest()

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "request": json.loads(self.request.canonical()),
            "risk": self.risk.value,
            "evidence_source": self.evidence_source.value,
            "evidence_state": self.evidence.state.value,
            "evidence": self.evidence.canonical(),
            "expected_effect": self.expected_effect,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "state": self.state.value,
        }

    def canonical(self) -> str:
        return json.dumps(self.canonical_payload(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def computed_digest(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    def is_expired(self, now: datetime) -> bool:
        return _utc(now) >= self.expires_at


@dataclass(frozen=True, slots=True)
class ApprovalBinding:
    approval_id: str
    request: ActionRequest
    plan_id: str
    plan_digest: str
    actor_id: str
    created_at: datetime
    expires_at: datetime
    scope: str
    state: ApprovalState = ApprovalState.APPROVAL_REQUESTED

    def __post_init__(self) -> None:
        if not isinstance(self.request, ActionRequest):
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid approval request")
        for name, value, maximum in (
            ("approval ID", self.approval_id, 128),
            ("plan ID", self.plan_id, 128),
            ("actor identity", self.actor_id, MAX_ACTOR_ID),
            ("approval scope", self.scope, MAX_METADATA_VALUE),
        ):
            _bounded_string(value, name=name, maximum=maximum, pattern=_SAFE_ID if name != "approval scope" else _SAFE_VALUE)
        _bounded_string(self.plan_digest, name="plan digest", maximum=64, pattern=re.compile(r"^[0-9a-f]{64}$"))
        created = _utc(self.created_at)
        expires = _utc(self.expires_at)
        if expires <= created or expires - created > APPROVAL_TTL:
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid approval expiry")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        state = self.state if isinstance(self.state, ApprovalState) else ApprovalState(self.state)
        object.__setattr__(self, "state", state)

    def is_expired(self, now: datetime) -> bool:
        return _utc(now) >= self.expires_at


@dataclass(frozen=True, slots=True)
class ActionAuditRecord:
    action_id: str
    plan_id: str
    plan_digest: str
    operation: OperationKind
    target_id: str
    actor_id: str
    timestamp: datetime
    state: AuditState
    risk: RiskLevel
    evidence_state: EvidenceState
    outcome_code: str

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("action ID", self.action_id, 128),
            ("plan ID", self.plan_id, 128),
            ("target identity", self.target_id, MAX_TARGET_ID),
            ("actor identity", self.actor_id, MAX_ACTOR_ID),
            ("outcome code", self.outcome_code, MAX_EVIDENCE_VALUE),
        ):
            _bounded_string(value, name=name, maximum=maximum, pattern=_SAFE_ID)
        _bounded_string(self.plan_digest, name="plan digest", maximum=64, pattern=re.compile(r"^[0-9a-f]{64}$"))
        object.__setattr__(self, "timestamp", _utc(self.timestamp))
        object.__setattr__(self, "operation", self.operation if isinstance(self.operation, OperationKind) else OperationKind(self.operation))
        object.__setattr__(self, "state", self.state if isinstance(self.state, AuditState) else AuditState(self.state))
        object.__setattr__(self, "risk", self.risk if isinstance(self.risk, RiskLevel) else RiskLevel(self.risk))
        object.__setattr__(self, "evidence_state", self.evidence_state if isinstance(self.evidence_state, EvidenceState) else EvidenceState(self.evidence_state))

    def safe_dict(self) -> dict[str, str]:
        return {
            "action_id": self.action_id,
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "operation": self.operation.value,
            "target_id": self.target_id,
            "actor_id": self.actor_id,
            "timestamp": self.timestamp.isoformat(),
            "state": self.state.value,
            "risk": self.risk.value,
            "evidence_state": self.evidence_state.value,
            "outcome_code": self.outcome_code,
        }


# MC-6.12 Stage 2 non-executing lifecycle foundation.
MAX_ENVIRONMENT_ID = 128
MAX_POLICY_VERSION = 64
MAX_ACTION_REASON = 256
MAX_AUDIT_ATTRIBUTES = 12


class ActorRole(str, Enum):
    REQUESTER = "requester"
    APPROVER = "approver"
    AUDITOR = "auditor"
    KILL_SWITCH_OPERATOR = "kill_switch_operator"
    EXECUTOR = "executor"
    VERIFIER = "verifier"


class LifecycleState(str, Enum):
    REQUESTED = "requested"
    PLANNED = "planned"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVED = "approved"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"
    REJECTED = "rejected"
    LEASED = "leased"
    RUNNING = "running"
    EXECUTED_PENDING_VERIFICATION = "executed_pending_verification"
    VERIFIED_SUCCESS = "verified_success"
    VERIFICATION_FAILED = "verification_failed"
    CANCEL_REQUESTED = "cancel_requested"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    ROLLBACK_REQUESTED = "rollback_requested"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_UNAVAILABLE = "rollback_unavailable"
    ROLLBACK_FAILED = "rollback_failed"


class LifecycleError(ValueError):
    """Raised when an immutable Stage 2 lifecycle value is invalid."""


def _stage2_id(value: Any, *, name: str, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or _SAFE_ID.fullmatch(value) is None:
        raise LifecycleError(f"Invalid {name}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise LifecycleError(f"Invalid {name}")
    return value


@dataclass(frozen=True, slots=True)
class ActionScope:
    """Immutable target/environment scope; never a filesystem or command path."""

    target_id: str
    environment: str
    policy_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", _stage2_id(self.target_id, name="target ID"))
        object.__setattr__(self, "environment", _stage2_id(self.environment, name="environment", maximum=MAX_ENVIRONMENT_ID))
        object.__setattr__(self, "policy_version", _stage2_id(self.policy_version, name="policy version", maximum=MAX_POLICY_VERSION))

    def canonical(self) -> dict[str, str]:
        return {
            "environment": self.environment,
            "policy_version": self.policy_version,
            "target_id": self.target_id,
        }


@dataclass(frozen=True, slots=True)
class ActionLifecycle:
    """Immutable logical action state; it does not execute or persist anything."""

    action_id: str
    plan_id: str
    plan_digest: str
    operation: OperationKind
    scope: ActionScope
    state: LifecycleState
    requester_subject: str
    approver_subject: str | None = None
    idempotency_key: str = ""
    created_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
    expires_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
    version: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _stage2_id(self.action_id, name="action ID"))
        object.__setattr__(self, "plan_id", _stage2_id(self.plan_id, name="plan ID"))
        if not isinstance(self.plan_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", self.plan_digest):
            raise LifecycleError("Invalid plan digest")
        operation = self.operation if isinstance(self.operation, OperationKind) else OperationKind(self.operation)
        if operation is not OperationKind.UPDATE_PROJECT_PLAN:
            raise LifecycleError("Unsupported lifecycle operation")
        object.__setattr__(self, "operation", operation)
        state = self.state if isinstance(self.state, LifecycleState) else LifecycleState(self.state)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "requester_subject", _stage2_id(self.requester_subject, name="requester subject"))
        if self.approver_subject is not None:
            approver = _stage2_id(self.approver_subject, name="approver subject")
            if approver == self.requester_subject:
                raise LifecycleError("Requester and approver must be distinct")
            object.__setattr__(self, "approver_subject", approver)
        if state in {
            LifecycleState.APPROVED,
            LifecycleState.LEASED,
            LifecycleState.RUNNING,
            LifecycleState.EXECUTED_PENDING_VERIFICATION,
            LifecycleState.VERIFIED_SUCCESS,
            LifecycleState.VERIFICATION_FAILED,
            LifecycleState.CANCEL_REQUESTED,
            LifecycleState.TIMED_OUT,
            LifecycleState.INTERRUPTED,
            LifecycleState.RECONCILIATION_REQUIRED,
            LifecycleState.ROLLBACK_REQUESTED,
            LifecycleState.ROLLED_BACK,
            LifecycleState.ROLLBACK_UNAVAILABLE,
            LifecycleState.ROLLBACK_FAILED,
        } and self.approver_subject is None:
            raise LifecycleError("Action state requires a distinct approver")
        object.__setattr__(self, "idempotency_key", _stage2_id(self.idempotency_key, name="idempotency key"))
        created = _utc(self.created_at)
        expires = _utc(self.expires_at)
        if expires <= created:
            raise LifecycleError("Lifecycle expiry must follow creation")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        if not isinstance(self.version, int) or self.version < 0:
            raise LifecycleError("Invalid lifecycle version")

    def canonical(self) -> str:
        return json.dumps({
            "action_id": self.action_id,
            "approver_subject": self.approver_subject,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "idempotency_key": self.idempotency_key,
            "operation": self.operation.value,
            "plan_digest": self.plan_digest,
            "plan_id": self.plan_id,
            "requester_subject": self.requester_subject,
            "scope": self.scope.canonical(),
            "state": self.state.value,
            "version": self.version,
        }, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def is_expired(self, now: datetime) -> bool:
        return _utc(now) >= self.expires_at


@dataclass(frozen=True, slots=True)
class Stage2AuditEvent:
    """Bounded audit fact about logical control-plane state, not external mutation."""

    event_id: str
    action_id: str
    plan_id: str
    plan_digest: str
    state: LifecycleState
    actor_subject: str
    actor_role: ActorRole
    timestamp: datetime
    outcome_code: str
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _stage2_id(self.event_id, name="audit event ID"))
        object.__setattr__(self, "action_id", _stage2_id(self.action_id, name="audit action ID"))
        object.__setattr__(self, "plan_id", _stage2_id(self.plan_id, name="audit plan ID"))
        if not isinstance(self.plan_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", self.plan_digest):
            raise LifecycleError("Invalid audit plan digest")
        object.__setattr__(self, "state", self.state if isinstance(self.state, LifecycleState) else LifecycleState(self.state))
        object.__setattr__(self, "actor_subject", _stage2_id(self.actor_subject, name="audit actor subject"))
        object.__setattr__(self, "actor_role", self.actor_role if isinstance(self.actor_role, ActorRole) else ActorRole(self.actor_role))
        object.__setattr__(self, "timestamp", _utc(self.timestamp))
        object.__setattr__(self, "outcome_code", _stage2_id(self.outcome_code, name="audit outcome code", maximum=MAX_ACTION_REASON))
        if len(self.attributes) > MAX_AUDIT_ATTRIBUTES:
            raise LifecycleError("Too many audit attributes")
        normalized: list[tuple[str, str]] = []
        for key, value in self.attributes:
            normalized.append((_stage2_id(key, name="audit attribute key", maximum=64), _stage2_id(value, name="audit attribute value", maximum=256)))
        if len({key for key, _value in normalized}) != len(normalized):
            raise LifecycleError("Duplicate audit attribute key")
        object.__setattr__(self, "attributes", tuple(sorted(normalized)))

    def safe_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "action_id": self.action_id,
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "state": self.state.value,
            "actor_subject": self.actor_subject,
            "actor_role": self.actor_role.value,
            "timestamp": self.timestamp.isoformat(),
            "outcome_code": self.outcome_code,
            "attributes": {key: value for key, value in self.attributes},
        }
