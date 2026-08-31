"""Canonical durable audit ledger for the control plane.

One audit abstraction records every security-relevant control-plane fact in
the dedicated control-plane database (atomicity model OPTION A: state
transitions and their evidence share one transaction; a state change whose
audit append fails is rolled back). The ledger is append-only, hash-chained,
sequence-ordered, centrally sanitized, and independently verifiable.

There is deliberately no other audit repository: the former in-memory audit
surfaces were retired in favor of this ledger.
"""

from aipm.control_plane.audit.canonical import AUDIT_CHAIN_VERSION, CANONICAL_FIELDS, canonical_audit_payload
from aipm.control_plane.audit.chain import GENESIS_PREVIOUS_HASH, compute_event_hash
from aipm.control_plane.audit.models import (
    SYSTEM_ACTOR_SUBJECT,
    UNAUTHENTICATED_ACTOR_SUBJECT,
    AuditActorRole,
    AuditEvent,
    AuditEventDraft,
    AuditEventError,
    AuditEventType,
    ChainVerificationResult,
)
from aipm.control_plane.audit.repository import InMemoryAuditLedger, SQLiteAuditLedger
from aipm.control_plane.audit.sanitize import AuditEventError as AuditSanitizationError

__all__ = [
    "AUDIT_CHAIN_VERSION",
    "CANONICAL_FIELDS",
    "ChainVerificationResult",
    "GENESIS_PREVIOUS_HASH",
    "InMemoryAuditLedger",
    "SQLiteAuditLedger",
    "SYSTEM_ACTOR_SUBJECT",
    "UNAUTHENTICATED_ACTOR_SUBJECT",
    "AuditActorRole",
    "AuditEvent",
    "AuditEventDraft",
    "AuditEventError",
    "AuditEventType",
    "AuditSanitizationError",
    "canonical_audit_payload",
    "compute_event_hash",
]
