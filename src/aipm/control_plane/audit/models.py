"""Canonical audit event model for the control plane.

One audit abstraction represents every security-relevant control-plane fact:
authorization decisions, action creation, owner confirmation, lifecycle
transitions, kill-switch changes, and — as future components exist — leases,
execution, verification, and rollback. The vocabulary is closed and typed;
arbitrary caller-provided event types are impossible.

Event identity is distinct from action identity: ``event_id`` is derived from
the canonical logical content, ``action_id`` is the canonical action identity.
References (action, plan, decision, confirmation, lease, parent event) are
optional links; an event remains valid when they are absent.

Free-text fields are sanitized centrally (``sanitize.py``) at construction, so
secret material cannot reach the ledger through any producer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import secrets

from aipm.control_plane.audit.canonical import AUDIT_CHAIN_VERSION
from aipm.control_plane.audit.sanitize import (
    AuditEventError,
    assert_no_secret_material,
    bounded_code,
    bounded_reason,
    bounded_reference,
    bounded_subject,
)


class AuditEventType(str, Enum):
    """Closed vocabulary of control-plane audit events."""

    AUTHENTICATION_SUCCESS = "authentication_success"
    AUTHENTICATION_FAILURE = "authentication_failure"
    SESSION_CREATED = "session_created"
    SESSION_REVOKED = "session_revoked"
    CREDENTIAL_EPOCH_ROTATED = "credential_epoch_rotated"

    AUTHORIZATION_REQUESTED = "authorization_requested"
    AUTHORIZATION_ALLOWED = "authorization_allowed"
    AUTHORIZATION_DENIED = "authorization_denied"

    ACTION_CREATED = "action_created"
    ACTION_IDEMPOTENCY_REPLAY = "action_idempotency_replay"
    ACTION_IDEMPOTENCY_CONFLICT = "action_idempotency_conflict"

    OWNER_CONFIRMATION_REQUESTED = "owner_confirmation_requested"
    OWNER_CONFIRMED = "owner_confirmed"
    OWNER_CONFIRMATION_REJECTED = "owner_confirmation_rejected"
    OWNER_CONFIRMATION_CONSUMED = "owner_confirmation_consumed"

    LIFECYCLE_TRANSITION = "lifecycle_transition"

    KILL_SWITCH_ENGAGED = "kill_switch_engaged"
    KILL_SWITCH_DISENGAGED = "kill_switch_disengaged"
    KILL_SWITCH_PERMANENT = "kill_switch_permanent"

    LEASE_ACQUIRED = "lease_acquired"
    LEASE_RELEASED = "lease_released"
    LEASE_EXPIRED = "lease_expired"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETED = "recovery_completed"

    EXECUTION_STARTED = "execution_started"
    EXECUTION_SUCCEEDED = "execution_succeeded"
    EXECUTION_FAILED = "execution_failed"

    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_SUCCEEDED = "verification_succeeded"
    VERIFICATION_FAILED = "verification_failed"

    ROLLBACK_REQUESTED = "rollback_requested"
    ROLLBACK_SUCCEEDED = "rollback_succeeded"
    ROLLBACK_FAILED = "rollback_failed"

    SYSTEM_ERROR = "system_error"


class AuditActorRole(str, Enum):
    """Explicit actor role; the underlying subject remains the canonical one."""

    REQUESTER = "requester"
    CONFIRMER = "confirmer"
    VERIFIER = "verifier"
    EXECUTOR = "executor"
    KILL_SWITCH_OPERATOR = "kill_switch_operator"
    AUDITOR = "auditor"
    SYSTEM = "system"


#: Bounded explicit actor for events raised by the control plane itself.
SYSTEM_ACTOR_SUBJECT = "control-plane-system"

#: Bounded explicit actor for events raised before a principal is established.
UNAUTHENTICATED_ACTOR_SUBJECT = "unauthenticated"


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise AuditEventError("Invalid audit timestamp")
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class AuditEventDraft:
    """Validated, sanitized, bounded logical content of one audit event.

    Constructing a draft is the only way to introduce content into the
    ledger; construction enforces the closed vocabulary, bounds, canonical
    actor semantics, and secret hygiene. ``event_id`` is a cryptographically
    random opaque identifier, unique per event occurrence.
    """

    event_type: AuditEventType
    actor_subject: str
    occurred_at: datetime
    event_id: str = field(default_factory=lambda: secrets.token_hex(16))
    actor_role: AuditActorRole = AuditActorRole.SYSTEM
    action_id: str | None = None
    plan_id: str | None = None
    plan_revision: int | None = None
    plan_digest: str | None = None
    target_id: str | None = None
    environment: str | None = None
    operation: str | None = None
    decision_id: str | None = None
    confirmation_id: str | None = None
    lease_id: str | None = None
    parent_event_id: str | None = None
    policy_version: str | None = None
    lifecycle_from: str | None = None
    lifecycle_to: str | None = None
    result_code: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        try:
            event_type = self.event_type if isinstance(self.event_type, AuditEventType) else AuditEventType(self.event_type)
            actor_role = self.actor_role if isinstance(self.actor_role, AuditActorRole) else AuditActorRole(self.actor_role)
        except ValueError as exc:
            raise AuditEventError("Invalid audit event type or actor role") from exc
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "actor_role", actor_role)
        object.__setattr__(self, "actor_subject", bounded_subject(self.actor_subject))
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at))
        for name in ("action_id", "plan_id", "target_id", "decision_id", "confirmation_id", "lease_id", "parent_event_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, bounded_reference(value, field=name))
        for name in ("policy_version", "environment", "operation", "lifecycle_from", "lifecycle_to"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, bounded_reference(value, field=name, maximum=128))
        if self.plan_revision is not None and (not isinstance(self.plan_revision, int) or self.plan_revision < 1):
            raise AuditEventError("Invalid audit plan revision")
        if self.plan_digest is not None:
            object.__setattr__(self, "plan_digest", bounded_reference(self.plan_digest, field="plan digest", maximum=64))
        if self.result_code is not None:
            object.__setattr__(self, "result_code", bounded_code(self.result_code))
        if self.reason is not None:
            object.__setattr__(self, "reason", bounded_reason(self.reason))

    def logical_payload(self) -> dict[str, Any]:
        """Canonical logical content (no chain fields); None fields absent."""

        payload: dict[str, Any] = {
            "actor_role": self.actor_role.value,
            "actor_subject": self.actor_subject,
            "chain_version": AUDIT_CHAIN_VERSION,
            "event_type": self.event_type.value,
            "occurred_at": self.occurred_at.isoformat(),
        }
        optional = {
            "action_id": self.action_id,
            "confirmation_id": self.confirmation_id,
            "decision_id": self.decision_id,
            "environment": self.environment,
            "lifecycle_from": self.lifecycle_from,
            "lifecycle_to": self.lifecycle_to,
            "lease_id": self.lease_id,
            "operation": self.operation,
            "parent_event_id": self.parent_event_id,
            "plan_digest": self.plan_digest,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "policy_version": self.policy_version,
            "reason": self.reason,
            "result_code": self.result_code,
            "target_id": self.target_id,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        return payload


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One persisted ledger record: draft content plus chain position."""

    sequence: int
    previous_hash: str
    event_hash: str
    draft: AuditEventDraft

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or self.sequence < 1:
            raise AuditEventError("Invalid audit sequence")
        if not isinstance(self.previous_hash, str) or len(self.previous_hash) != 64:
            raise AuditEventError("Invalid audit previous hash")
        if not isinstance(self.event_hash, str) or len(self.event_hash) != 64:
            raise AuditEventError("Invalid audit event hash")
        if not isinstance(self.draft, AuditEventDraft):
            raise AuditEventError("Invalid audit draft")

    def chain_payload(self) -> dict[str, Any]:
        payload = self.draft.logical_payload()
        payload["event_id"] = self.draft.event_id
        payload["previous_hash"] = self.previous_hash
        payload["sequence"] = self.sequence
        return payload

    @property
    def event_type(self) -> AuditEventType:
        return self.draft.event_type

    @property
    def actor_subject(self) -> str:
        return self.draft.actor_subject

    @property
    def action_id(self) -> str | None:
        return self.draft.action_id

    @property
    def event_id(self) -> str:
        return self.draft.event_id

    def safe_dict(self) -> dict[str, Any]:
        payload = self.chain_payload()
        payload["event_hash"] = self.event_hash
        return payload


@dataclass(frozen=True, slots=True)
class ChainVerificationResult:
    """Bounded result of an independent ledger chain verification."""

    ok: bool
    events_checked: int
    error_sequence: int | None = None
    error: str | None = None

    def safe_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "events_checked": self.events_checked,
            "error_sequence": self.error_sequence,
            "error": self.error,
        }
