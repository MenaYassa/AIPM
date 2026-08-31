"""Audit ledger repositories.

``SQLiteAuditLedger`` is the durable, tamper-evident authority, stored in the
dedicated control-plane database (atomicity model OPTION A: state + evidence
share one transaction; a state transition whose audit append fails is rolled
back entirely). ``InMemoryAuditLedger`` is the explicit test double obeying
the same contract.

The public surface is append-only: there is no update, delete, or reorder
path. ``verify_chain`` independently re-reads the persisted rows and
recomputes the chain. Sequence allocation is database-serialized (PRIMARY KEY
on ``sequence``); concurrent appends retry on the rare allocation race.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from aipm.control_plane.audit.canonical import AUDIT_CHAIN_VERSION
from aipm.control_plane.audit.chain import GENESIS_PREVIOUS_HASH, compute_event_hash
from aipm.control_plane.audit.models import (
    AuditEvent,
    AuditEventDraft,
    AuditEventError,
    ChainVerificationResult,
)
from aipm.control_plane.models import ControlPlaneError, PlanningErrorCode

_MAX_APPEND_ATTEMPTS = 4


class SQLiteAuditLedger:
    """Durable append-only hash-chained audit ledger."""

    __slots__ = ("_db", "_initialized")

    def __init__(self, db) -> None:
        from aipm.control_plane.storage.sqlite_store import ControlPlaneDatabase

        if not isinstance(db, ControlPlaneDatabase):
            raise TypeError("SQLiteAuditLedger requires a ControlPlaneDatabase")
        object.__setattr__(self, "_db", db)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("SQLiteAuditLedger configuration is immutable")
        object.__setattr__(self, name, value)

    # -- public append-only API --------------------------------------------

    def append(self, draft: AuditEventDraft) -> AuditEvent:
        """Append one event with a durable sequence and chain linkage.

        The append runs inside an exclusive ``BEGIN IMMEDIATE`` transaction so
        the sequence read and the insert share the database write lock;
        concurrent appends serialize on the database itself, and the
        ``PRIMARY KEY(sequence)`` / ``UNIQUE(event_id)`` constraints remain
        the ultimate authorities.
        """

        if not isinstance(draft, AuditEventDraft):
            raise AuditEventError("Invalid audit draft")
        last_error: Exception | None = None
        for _attempt in range(_MAX_APPEND_ATTEMPTS):
            try:
                with self._db.transaction():
                    return self._append_in_transaction(draft)
            except sqlite3.IntegrityError as exc:
                last_error = exc
                continue
            except sqlite3.Error as exc:
                last_error = exc
                continue
        raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Audit append could not allocate a sequence") from last_error

    def append_in_transaction(self, draft: AuditEventDraft) -> AuditEvent:
        """Append inside a caller-owned transaction on the same database.

        Used by the composite state+evidence writes; raises on failure so the
        caller's transaction (state change) rolls back too.
        """

        if not isinstance(draft, AuditEventDraft):
            raise AuditEventError("Invalid audit draft")
        return self._append_in_transaction(draft)

    def events(self, limit: int = 256) -> tuple[AuditEvent, ...]:
        """Bounded read of the most recent events, newest last."""

        if not isinstance(limit, int) or limit < 1 or limit > 4096:
            raise ValueError("Invalid audit read bound")
        rows = self._db.connection.execute(
            "SELECT * FROM control_plane_audit_ledger ORDER BY sequence DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(_event_from_row(row) for row in reversed(rows))

    def count(self) -> int:
        row = self._db.connection.execute("SELECT COUNT(*) AS total FROM control_plane_audit_ledger").fetchone()
        return int(row["total"]) if row else 0

    def verify_chain(self) -> ChainVerificationResult:
        """Independently verify sequence continuity, linkage, and hashes."""

        expected_sequence = 1
        previous_hash = GENESIS_PREVIOUS_HASH
        checked = 0
        cursor = self._db.connection.execute(
            "SELECT * FROM control_plane_audit_ledger ORDER BY sequence ASC"
        )
        while True:
            rows = cursor.fetchmany(256)
            if not rows:
                break
            for row in rows:
                try:
                    event = _event_from_row(row)
                except (AuditEventError, ControlPlaneError, ValueError, TypeError, KeyError) as exc:
                    return ChainVerificationResult(False, checked, expected_sequence, f"unreadable record: {exc}")
                if event.sequence != expected_sequence:
                    return ChainVerificationResult(False, checked, expected_sequence, "sequence discontinuity")
                if event.previous_hash != previous_hash:
                    return ChainVerificationResult(False, checked, event.sequence, "previous hash linkage broken")
                expected_hash = compute_event_hash(event.previous_hash, event.chain_payload())
                if event.event_hash != expected_hash:
                    return ChainVerificationResult(False, checked, event.sequence, "event hash mismatch")
                previous_hash = event.event_hash
                expected_sequence += 1
                checked += 1
        return ChainVerificationResult(True, checked, None, None)

    # -- internals ----------------------------------------------------------

    def _append_in_transaction(self, draft: AuditEventDraft) -> AuditEvent:
        event_id = draft.event_id
        if self._db.connection.execute(
            "SELECT 1 FROM control_plane_audit_ledger WHERE event_id = ?",
            (event_id,),
        ).fetchone() is not None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Duplicate audit event identity")
        row = self._db.connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS last FROM control_plane_audit_ledger"
        ).fetchone()
        sequence = int(row["last"]) + 1 if row else 1
        previous_hash = self._last_hash()
        chain_payload = {**draft.logical_payload(), "event_id": event_id, "previous_hash": previous_hash, "sequence": sequence}
        event_hash = compute_event_hash(previous_hash, chain_payload)
        self._db.connection.execute(
            "INSERT INTO control_plane_audit_ledger (sequence, event_id, event_type, occurred_at, actor_subject, actor_role,"
            " action_id, plan_id, plan_revision, plan_digest, target_id, environment, operation, decision_id,"
            " confirmation_id, lease_id, parent_event_id, policy_version, lifecycle_from, lifecycle_to,"
            " result_code, reason, previous_hash, event_hash, chain_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sequence,
                event_id,
                draft.event_type.value,
                draft.occurred_at.isoformat(),
                draft.actor_subject,
                draft.actor_role.value,
                draft.action_id,
                draft.plan_id,
                draft.plan_revision,
                draft.plan_digest,
                draft.target_id,
                draft.environment,
                draft.operation,
                draft.decision_id,
                draft.confirmation_id,
                draft.lease_id,
                draft.parent_event_id,
                draft.policy_version,
                draft.lifecycle_from,
                draft.lifecycle_to,
                draft.result_code,
                draft.reason,
                previous_hash,
                event_hash,
                AUDIT_CHAIN_VERSION,
            ),
        )
        return AuditEvent(sequence=sequence, previous_hash=previous_hash, event_hash=event_hash, draft=draft)

    def _last_hash(self) -> str:
        row = self._db.connection.execute(
            "SELECT event_hash FROM control_plane_audit_ledger ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return GENESIS_PREVIOUS_HASH
        return str(row["event_hash"])


class InMemoryAuditLedger:
    """Bounded in-memory test double obeying the same contract."""

    def __init__(self, *, max_events: int = 4096) -> None:
        if max_events < 1:
            raise ValueError("Invalid audit bound")
        self._max_events = max_events
        self._events: list[AuditEvent] = []

    def append(self, draft: AuditEventDraft) -> AuditEvent:
        if not isinstance(draft, AuditEventDraft):
            raise AuditEventError("Invalid audit draft")
        return self.append_in_transaction(draft)

    def append_in_transaction(self, draft: AuditEventDraft) -> AuditEvent:
        if not isinstance(draft, AuditEventDraft):
            raise AuditEventError("Invalid audit draft")
        if any(event.draft.event_id == draft.event_id for event in self._events):
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Duplicate audit event identity")
        if len(self._events) >= self._max_events:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Audit bound reached")
        sequence = len(self._events) + 1
        previous_hash = self._events[-1].event_hash if self._events else GENESIS_PREVIOUS_HASH
        event_id = draft.event_id
        chain_payload = {**draft.logical_payload(), "event_id": event_id, "previous_hash": previous_hash, "sequence": sequence}
        event_hash = compute_event_hash(previous_hash, chain_payload)
        event = AuditEvent(sequence=sequence, previous_hash=previous_hash, event_hash=event_hash, draft=draft)
        self._events.append(event)
        return event

    def events(self, limit: int = 256) -> tuple[AuditEvent, ...]:
        if not isinstance(limit, int) or limit < 1 or limit > 4096:
            raise ValueError("Invalid audit read bound")
        return tuple(self._events[-limit:])

    def count(self) -> int:
        return len(self._events)

    def verify_chain(self) -> ChainVerificationResult:
        previous_hash = GENESIS_PREVIOUS_HASH
        for expected_sequence, event in enumerate(self._events, start=1):
            if event.sequence != expected_sequence:
                return ChainVerificationResult(False, expected_sequence - 1, expected_sequence, "sequence discontinuity")
            if event.previous_hash != previous_hash:
                return ChainVerificationResult(False, expected_sequence - 1, event.sequence, "previous hash linkage broken")
            if event.event_hash != compute_event_hash(event.previous_hash, event.chain_payload()):
                return ChainVerificationResult(False, expected_sequence - 1, event.sequence, "event hash mismatch")
            previous_hash = event.event_hash
        return ChainVerificationResult(True, len(self._events), None, None)


def _event_from_row(row) -> AuditEvent:
    from aipm.control_plane.audit.models import AuditActorRole, AuditEventType

    try:
        draft = AuditEventDraft(
            event_type=AuditEventType(row["event_type"]),
            event_id=row["event_id"],
            actor_subject=row["actor_subject"],
            occurred_at=_parse_timestamp(row["occurred_at"]),
            actor_role=AuditActorRole(row["actor_role"]),
            action_id=row["action_id"],
            plan_id=row["plan_id"],
            plan_revision=row["plan_revision"],
            plan_digest=row["plan_digest"],
            target_id=row["target_id"],
            environment=row["environment"],
            operation=row["operation"],
            decision_id=row["decision_id"],
            confirmation_id=row["confirmation_id"],
            lease_id=row["lease_id"],
            parent_event_id=row["parent_event_id"],
            policy_version=row["policy_version"],
            lifecycle_from=row["lifecycle_from"],
            lifecycle_to=row["lifecycle_to"],
            result_code=row["result_code"],
            reason=row["reason"],
        )
    except (AuditEventError, ValueError, TypeError, KeyError) as exc:
        raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Stored audit record cannot be reconstructed") from exc
    if row["chain_version"] != AUDIT_CHAIN_VERSION:
        raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Stored audit record has an unknown chain version")
    return AuditEvent(
        sequence=int(row["sequence"]),
        previous_hash=row["previous_hash"],
        event_hash=row["event_hash"],
        draft=draft,
    )


def _parse_timestamp(value):
    from datetime import datetime

    if not isinstance(value, str):
        raise AuditEventError("Invalid audit timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AuditEventError("Invalid audit timestamp") from exc
    if parsed.tzinfo is None:
        raise AuditEventError("Invalid audit timestamp")
    return parsed
