"""Versioned schema for the dedicated control-plane database.

This database is entirely separate from the telemetry/events/incidents/
notification databases: it has its own file, its own path, and its own schema
versioning. It never stores secrets, session identifiers, cookies, or owner
credentials.
"""
from __future__ import annotations

SCHEMA_NAME = "control_plane"
SCHEMA_VERSION = 5

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS control_plane_schema_meta (
        schema_name TEXT PRIMARY KEY,
        schema_version INTEGER NOT NULL,
        migrated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_plans (
        target_id TEXT PRIMARY KEY,
        environment TEXT NOT NULL,
        revision INTEGER NOT NULL,
        title TEXT NOT NULL,
        objective TEXT NOT NULL,
        enabled INTEGER NOT NULL,
        canonical_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS authorization_decisions (
        decision_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL,
        allowed INTEGER NOT NULL,
        code TEXT NOT NULL,
        operation TEXT NOT NULL,
        target_id TEXT NOT NULL,
        environment TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        principal_subject TEXT NOT NULL,
        confirmation_required INTEGER NOT NULL,
        confirmation_kind TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        plan_revision INTEGER NOT NULL,
        plan_digest TEXT NOT NULL,
        target_digest TEXT NOT NULL,
        request_canonical TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS actions (
        action_id TEXT PRIMARY KEY,
        decision_id TEXT NOT NULL REFERENCES authorization_decisions(decision_id),
        idempotency_key TEXT NOT NULL,
        operation TEXT NOT NULL,
        target_id TEXT NOT NULL,
        environment TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        plan_revision INTEGER NOT NULL,
        plan_digest TEXT NOT NULL,
        target_digest TEXT NOT NULL,
        requester_subject TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        lifecycle_state TEXT NOT NULL,
        confirmation_kind TEXT NOT NULL,
        approver_subject TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        version INTEGER NOT NULL,
        rollback_of_action_id TEXT,
        snapshot_id TEXT,
        outcome TEXT,
        contract_version TEXT,
        capability_version TEXT,
        contract_digest TEXT,
        UNIQUE (target_id, operation, idempotency_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS confirmations (
        confirmation_id TEXT PRIMARY KEY,
        decision_id TEXT NOT NULL,
        action_id TEXT NOT NULL REFERENCES actions(action_id),
        plan_id TEXT NOT NULL,
        plan_digest TEXT NOT NULL,
        target_revision INTEGER NOT NULL,
        target_digest TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        requester_subject TEXT NOT NULL,
        confirmed_by_subject TEXT,
        confirmation_kind TEXT NOT NULL,
        request_canonical TEXT NOT NULL,
        scope TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kill_switch_state (
        environment TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        epoch INTEGER NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        actor_subject TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS execution_leases (
        lease_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL,
        environment TEXT NOT NULL,
        fencing_token INTEGER NOT NULL,
        state TEXT NOT NULL,
        holder TEXT,
        granted_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        released_at TEXT,
        action_version INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        target_id TEXT NOT NULL,
        environment TEXT NOT NULL,
        revision INTEGER NOT NULL,
        canonical_digest TEXT NOT NULL,
        payload_canonical TEXT NOT NULL,
        action_id TEXT,
        captured_at TEXT NOT NULL,
        plan_id TEXT,
        snapshot_version TEXT NOT NULL DEFAULT 'mc612-snapshot-v1',
        integrity_digest TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS verification_records (
        verification_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL,
        success INTEGER NOT NULL,
        reason_code TEXT NOT NULL,
        expected_revision INTEGER NOT NULL,
        observed_revision INTEGER,
        expected_digest TEXT,
        observed_digest TEXT,
        verifier TEXT NOT NULL,
        verification_version TEXT NOT NULL,
        evidence_references TEXT NOT NULL DEFAULT '[]',
        observed_at TEXT NOT NULL,
        integrity_digest TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS operator_sessions (
        session_id_hash TEXT PRIMARY KEY,
        principal_subject TEXT NOT NULL,
        auth_epoch INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        inactivity_expires_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        revoked_at TEXT,
        csrf_token_hash TEXT NOT NULL,
        session_version TEXT NOT NULL DEFAULT 'mc612-session-v1'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_actions_target_state ON actions (target_id, lifecycle_state)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_leases_action ON execution_leases (action_id, fencing_token)
    """,
    """
    CREATE TABLE IF NOT EXISTS control_plane_audit_ledger (
        sequence INTEGER PRIMARY KEY,
        event_id TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        actor_subject TEXT NOT NULL,
        actor_role TEXT NOT NULL,
        action_id TEXT,
        plan_id TEXT,
        plan_revision INTEGER,
        plan_digest TEXT,
        target_id TEXT,
        environment TEXT,
        operation TEXT,
        decision_id TEXT,
        confirmation_id TEXT,
        lease_id TEXT,
        parent_event_id TEXT,
        policy_version TEXT,
        lifecycle_from TEXT,
        lifecycle_to TEXT,
        result_code TEXT,
        reason TEXT,
        previous_hash TEXT NOT NULL,
        event_hash TEXT NOT NULL,
        chain_version TEXT NOT NULL
    )
    """,
)


def schema_statements() -> tuple[str, ...]:
    """Return the deterministic, ordered schema statements for this version."""

    return _SCHEMA_STATEMENTS


def schema_statements_for_version(version: int) -> tuple[str, ...]:
    """Statements to apply when upgrading FROM ``version`` to the current one.

    Migration model: every statement is idempotent (IF NOT EXISTS), so an
    older database simply applies the full current set; column additions for
    pre-existing tables are applied deterministically by the store via
    :data:`COLUMN_MIGRATIONS`; the version stamp is advanced last.
    """

    if version < 0 or version > SCHEMA_VERSION:
        return ()
    return _SCHEMA_STATEMENTS


#: Deterministic column additions for databases created before v3, keyed by
#: table. Applied only when the column is missing (PRAGMA table_info check).
COLUMN_MIGRATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "plan_snapshots": (
        ("plan_id", "TEXT"),
        ("snapshot_version", "TEXT NOT NULL DEFAULT 'mc612-snapshot-v1'"),
        ("integrity_digest", "TEXT NOT NULL DEFAULT ''"),
    ),
    "actions": (
        ("rollback_of_action_id", "TEXT"),
        ("snapshot_id", "TEXT"),
        ("outcome", "TEXT"),
        ("contract_version", "TEXT"),
        ("capability_version", "TEXT"),
        ("contract_digest", "TEXT"),
    ),
    "operator_sessions": (
        ("session_version", "TEXT NOT NULL DEFAULT 'mc612-session-v1'"),
        ("csrf_token_hash", "TEXT NOT NULL DEFAULT ''"),
    ),
    "execution_leases": (
        ("action_version", "INTEGER"),
    ),
}
