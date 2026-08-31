"""Unit tests for the canonical audit event model (Shot 4 ledger)."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

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
from aipm.control_plane.audit.repository import InMemoryAuditLedger
from aipm.control_plane.audit.sanitize import bounded_reason

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def draft(**overrides):
    values = {
        "event_type": AuditEventType.ACTION_CREATED,
        "actor_subject": "local-owner",
        "occurred_at": NOW,
        "actor_role": AuditActorRole.REQUESTER,
        "action_id": "a" * 64,
        "result_code": "created",
    }
    values.update(overrides)
    return AuditEventDraft(**values)


def test_audit_event_draft_is_immutable_and_bounded():
    value = draft()
    assert value.event_type is AuditEventType.ACTION_CREATED
    assert value.event_id and len(value.event_id) == 32
    with pytest.raises((FrozenInstanceError, AttributeError)):
        value.actor_subject = "someone-else"
    with pytest.raises(AuditEventError):
        draft(reason="x" * 257)
    with pytest.raises(AuditEventError):
        draft(result_code="x" * 65)
    with pytest.raises(AuditEventError):
        draft(plan_revision=0)
    with pytest.raises(AuditEventError):
        draft(event_type="not-in-vocabulary")
    with pytest.raises(AuditEventError):
        draft(actor_role="not-a-role")


def test_actor_roles_are_explicit_and_system_actors_are_bounded_constants():
    assert SYSTEM_ACTOR_SUBJECT and UNAUTHENTICATED_ACTOR_SUBJECT
    assert SYSTEM_ACTOR_SUBJECT != UNAUTHENTICATED_ACTOR_SUBJECT
    system = draft(actor_subject=SYSTEM_ACTOR_SUBJECT, actor_role=AuditActorRole.SYSTEM)
    unauthenticated = draft(actor_subject=UNAUTHENTICATED_ACTOR_SUBJECT, actor_role=AuditActorRole.SYSTEM)
    assert system.actor_role is AuditActorRole.SYSTEM
    assert unauthenticated.actor_subject == "unauthenticated"
    with pytest.raises(AuditEventError):
        draft(actor_subject="")
    with pytest.raises(AuditEventError):
        draft(actor_subject=None)


def test_secret_like_material_is_rejected_at_construction():
    for payload in (
        {"reason": "the password is hunter2"},
        {"reason": "bearer token presented"},
        {"reason": "session_id=abc123"},
        {"actor_subject": "subject-with-$argon2id$verifier"},
        {"result_code": "carried_credential"},
    ):
        with pytest.raises(AuditEventError):
            draft(**payload)


def test_event_id_is_unique_per_occurrence_and_opaque():
    first = draft()
    second = draft()
    assert first.event_id != second.event_id
    assert len(first.event_id) == 32
    assert all(char in "0123456789abcdef" for char in first.event_id)


def test_chain_payload_and_hash_are_canonical_and_versioned():
    ledger = InMemoryAuditLedger()
    event = ledger.append(draft())
    payload = event.chain_payload()
    assert payload["chain_version"] == AUDIT_CHAIN_VERSION == "mc612-audit-v1"
    assert payload["previous_hash"] == GENESIS_PREVIOUS_HASH
    assert payload["sequence"] == 1
    assert set(payload) <= set(CANONICAL_FIELDS)
    assert event.event_hash == compute_event_hash(event.previous_hash, payload)
    canonical = canonical_audit_payload(payload)
    assert canonical == canonical_audit_payload(dict(sorted(payload.items())))
    assert "None" not in canonical and "null" not in canonical


def test_optional_references_are_absent_not_null():
    minimal = AuditEventDraft(event_type=AuditEventType.SYSTEM_ERROR, actor_subject=SYSTEM_ACTOR_SUBJECT, occurred_at=NOW)
    payload = minimal.logical_payload()
    assert "action_id" not in payload and "decision_id" not in payload
    assert minimal.logical_payload() == minimal.logical_payload()


def test_chain_verification_result_is_bounded():
    ledger = InMemoryAuditLedger()
    ledger.append(draft())
    result = ledger.verify_chain()
    assert isinstance(result, ChainVerificationResult)
    assert result.ok is True and result.events_checked == 1
    assert result.safe_dict()["error"] is None


def test_bounded_reason_rejects_control_characters():
    with pytest.raises(AuditEventError):
        bounded_reason("line\nbreak")
    assert bounded_reason("maintenance window") == "maintenance window"
