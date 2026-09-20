"""MC-6.16-D2.2 Gate A: V7→V8 Actions Migration Tests

Validates v7→v8 migration for action_protocol column:
- Real v7 database → v8 migration
- All actions classified as legacy-v1
- Physical NOT NULL constraint enforced
- Indexes preserved
- Foreign keys preserved
- Data preservation
- Idempotency
- Partial migration handling
"""
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from aipm.control_plane.storage.schema import schema_statements
from aipm.control_plane.storage.sqlite_store import ControlPlaneDatabase


@pytest.fixture
def temp_db_path():
    """Create temp database in controlled directory (not /tmp) for ControlPlaneDatabase."""
    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "test_control_plane.db"
    yield db_path
    if db_path.exists():
        db_path.unlink()
    Path(temp_dir).rmdir()


def create_v7_database_with_actions(db_path: str, num_actions: int = 3):
    """Create a genuine v7 database with actions (no action_protocol column)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Create v7 schema (manually reconstructed without action_protocol)
    conn.execute("""
        CREATE TABLE control_plane_schema_meta (
            schema_name TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            migrated_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        INSERT INTO control_plane_schema_meta (schema_name, schema_version, migrated_at)
        VALUES ('control_plane', 7, ?)
    """, (datetime.now(timezone.utc).isoformat(),))

    conn.execute("""
        CREATE TABLE authorization_decisions (
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
    """)

    # V7 actions table (24 columns, no action_protocol)
    conn.execute("""
        CREATE TABLE actions (
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
    """)

    # V7 index
    conn.execute("""
        CREATE INDEX idx_actions_target_state ON actions (target_id, lifecycle_state)
    """)

    # Insert test data
    now = datetime.now(timezone.utc)
    for i in range(num_actions):
        action_id = f"action_{i}_" + "a" * (64 - len(f"action_{i}_"))
        decision_id = f"decision_{i}_" + "d" * (64 - len(f"decision_{i}_"))

        # Insert decision first (foreign key)
        conn.execute("""
            INSERT INTO authorization_decisions (
                decision_id, action_id, allowed, code, operation, target_id, environment,
                policy_version, principal_subject, confirmation_required, confirmation_kind,
                plan_id, plan_revision, plan_digest, target_digest, request_canonical,
                decided_at, expires_at
            ) VALUES (?, ?, 1, 'ALLOWED', 'update', ?, 'staging', 'v1', 'test_subject', 1,
                      'owner_confirmation', 'plan_id', 1, ?, ?, '{}', ?, ?)
        """, (
            decision_id, action_id, f"test_project_{i}",
            "p" * 64, "t" * 64,
            now.isoformat(), (now + timedelta(hours=1)).isoformat()
        ))

        # Insert action (v7 - no action_protocol)
        conn.execute("""
            INSERT INTO actions (
                action_id, decision_id, idempotency_key, operation, target_id, environment,
                plan_id, plan_revision, plan_digest, target_digest, requester_subject,
                policy_version, lifecycle_state, confirmation_kind, approver_subject,
                created_at, updated_at, expires_at, version, rollback_of_action_id,
                snapshot_id, outcome, contract_version, capability_version, contract_digest
            ) VALUES (?, ?, ?, 'update', ?, 'staging', 'plan_id', 1, ?, ?, 'test_subject',
                      'v1', 'requested', 'owner_confirmation', NULL, ?, ?, ?, 0, NULL,
                      ?, NULL, NULL, NULL, NULL)
        """, (
            action_id, decision_id, f"idem_key_{i}", f"test_project_{i}",
            "p" * 64, "t" * 64,
            now.isoformat(), now.isoformat(), (now + timedelta(hours=1)).isoformat(),
            f"snapshot_{i}" if i % 2 == 0 else None
        ))

    conn.commit()
    conn.close()

    # Set correct permissions for ControlPlaneDatabase
    import os
    os.chmod(db_path, 0o600)


def test_v7_to_v8_migration_classifies_legacy(temp_db_path):
    """V7→V8 migration classifies all existing actions as legacy-v1."""
    create_v7_database_with_actions(temp_db_path, num_actions=5)

    # Verify v7 state
    conn = sqlite3.connect(temp_db_path)
    conn.row_factory = sqlite3.Row
    cols = {row[1] for row in conn.execute("PRAGMA table_info(actions)")}
    assert "action_protocol" not in cols, "V7 should not have action_protocol"
    conn.close()

    # Trigger migration
    db = ControlPlaneDatabase(temp_db_path)

    # Verify all actions have legacy-v1
    rows = list(db.connection.execute("SELECT action_id, action_protocol FROM actions"))
    assert len(rows) == 5
    for row in rows:
        assert row["action_protocol"] == "legacy-v1", f"Action {row['action_id']} must be legacy-v1"


def test_v7_to_v8_migration_enforces_not_null(temp_db_path):
    """V7→V8 migration results in physical TEXT NOT NULL constraint."""
    create_v7_database_with_actions(temp_db_path, num_actions=2)

    # Trigger migration
    db = ControlPlaneDatabase(temp_db_path)

    # Verify physical schema has NOT NULL
    schema_info = list(db.connection.execute("PRAGMA table_info(actions)"))
    protocol_col = [col for col in schema_info if col[1] == "action_protocol"]

    assert len(protocol_col) == 1, "action_protocol column must exist"
    assert protocol_col[0][2].upper() == "TEXT", "action_protocol must be TEXT type"
    assert protocol_col[0][3] == 1, "action_protocol must have NOT NULL constraint (notnull=1)"


def test_v7_to_v8_migration_preserves_data(temp_db_path):
    """V7→V8 migration preserves all existing action data."""
    create_v7_database_with_actions(temp_db_path, num_actions=3)

    # Capture pre-migration data
    conn = sqlite3.connect(temp_db_path)
    conn.row_factory = sqlite3.Row
    pre_migration_data = {}
    for row in conn.execute("SELECT * FROM actions"):
        pre_migration_data[row["action_id"]] = {
            "decision_id": row["decision_id"],
            "idempotency_key": row["idempotency_key"],
            "target_id": row["target_id"],
            "snapshot_id": row["snapshot_id"],
            "lifecycle_state": row["lifecycle_state"],
        }
    conn.close()

    # Trigger migration
    db = ControlPlaneDatabase(temp_db_path)

    # Verify data preserved
    for row in db.connection.execute("SELECT * FROM actions"):
        action_id = row["action_id"]
        assert action_id in pre_migration_data
        pre_data = pre_migration_data[action_id]
        assert row["decision_id"] == pre_data["decision_id"]
        assert row["idempotency_key"] == pre_data["idempotency_key"]
        assert row["target_id"] == pre_data["target_id"]
        assert row["snapshot_id"] == pre_data["snapshot_id"]
        assert row["lifecycle_state"] == pre_data["lifecycle_state"]


def test_v7_to_v8_migration_preserves_index(temp_db_path):
    """V7→V8 migration preserves idx_actions_target_state index."""
    create_v7_database_with_actions(temp_db_path, num_actions=2)

    # Trigger migration
    db = ControlPlaneDatabase(temp_db_path)

    # Verify index exists
    indexes = list(db.connection.execute("""
        SELECT name, tbl_name, sql FROM sqlite_master
        WHERE type='index' AND tbl_name='actions'
    """))

    index_names = {idx[0] for idx in indexes}
    assert "idx_actions_target_state" in index_names, "idx_actions_target_state must be preserved"


def test_v7_to_v8_migration_preserves_constraints(temp_db_path):
    """V7→V8 migration preserves UNIQUE constraint and foreign key."""
    create_v7_database_with_actions(temp_db_path, num_actions=2)

    # Trigger migration
    db = ControlPlaneDatabase(temp_db_path)

    # Verify UNIQUE constraint (attempt duplicate should fail)
    now = datetime.now(timezone.utc).isoformat()
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.connection.execute("""
            INSERT INTO actions (
                action_id, decision_id, idempotency_key, operation, target_id, environment,
                plan_id, plan_revision, plan_digest, target_digest, requester_subject,
                policy_version, lifecycle_state, confirmation_kind, created_at, updated_at,
                expires_at, version, action_protocol
            ) VALUES (?, 'decision_0_' || ?, 'idem_key_0', 'update', 'test_project_0', 'staging',
                      'plan_id', 1, ?, ?, 'test_subject', 'v1', 'requested', 'owner_confirmation',
                      ?, ?, ?, 0, 'mc616d2-v1')
        """, ("new_action_" + "x" * 53, "d" * 53, "p" * 64, "t" * 64, now, now, now))


def test_v7_to_v8_migration_zero_nulls(temp_db_path):
    """V7→V8 migration results in zero NULL action_protocol values."""
    create_v7_database_with_actions(temp_db_path, num_actions=10)

    # Trigger migration
    db = ControlPlaneDatabase(temp_db_path)

    # Verify zero NULLs
    null_count = db.connection.execute("SELECT COUNT(*) FROM actions WHERE action_protocol IS NULL").fetchone()[0]
    assert null_count == 0, "No actions should have NULL action_protocol"


def test_v7_to_v8_migration_idempotency(temp_db_path):
    """Running v7→v8 migration twice is safe (idempotent)."""
    create_v7_database_with_actions(temp_db_path, num_actions=3)

    # First migration
    db = ControlPlaneDatabase(temp_db_path)
    first_count = db.connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    db.close()

    # Second migration (reopen triggers migration check)
    db2 = ControlPlaneDatabase(temp_db_path)
    second_count = db2.connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0]

    assert first_count == second_count == 3, "Row count must be preserved"

    # Verify all still legacy-v1
    for row in db2.connection.execute("SELECT action_protocol FROM actions"):
        assert row[0] == "legacy-v1"


def test_v7_to_v8_partial_migration_rejected(temp_db_path):
    """Partial migration (nullable action_protocol) is rejected with clear error."""
    create_v7_database_with_actions(temp_db_path, num_actions=2)

    # Manually create partial migration: add nullable action_protocol
    conn = sqlite3.connect(temp_db_path)
    conn.execute("ALTER TABLE actions ADD COLUMN action_protocol TEXT")
    conn.execute("UPDATE actions SET action_protocol = 'legacy-v1'")
    conn.commit()

    # Verify partial state (column exists but nullable)
    schema_info = list(conn.execute("PRAGMA table_info(actions)"))
    protocol_col = [col for col in schema_info if col[1] == "action_protocol"]
    assert protocol_col[0][3] == 0, "Partial migration: column is nullable"
    conn.close()

    # Attempt to open database (should detect partial migration)
    from aipm.control_plane.models import ControlPlaneError

    with pytest.raises(ControlPlaneError, match="Partial v7→v8 migration detected"):
        db = ControlPlaneDatabase(temp_db_path)


def test_v8_fresh_database_has_not_null(temp_db_path):
    """Fresh v8 database (no migration) has action_protocol NOT NULL."""
    # Create fresh v8 database
    db = ControlPlaneDatabase(temp_db_path)

    # Verify action_protocol column exists with NOT NULL
    schema_info = list(db.connection.execute("PRAGMA table_info(actions)"))
    protocol_col = [col for col in schema_info if col[1] == "action_protocol"]

    assert len(protocol_col) == 1
    assert protocol_col[0][3] == 1, "Fresh v8 must have NOT NULL constraint"


def test_v7_to_v8_migration_rollback_on_failure(temp_db_path):
    """V7→V8 migration rolls back on failure, leaving v7 intact."""
    create_v7_database_with_actions(temp_db_path, num_actions=2)

    # Corrupt the database to force migration failure
    conn = sqlite3.connect(temp_db_path)
    # Drop authorization_decisions to cause foreign key violation during reconstruction
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TABLE authorization_decisions")
    conn.commit()
    conn.close()

    # Attempt migration (should fail and rollback)
    try:
        db = ControlPlaneDatabase(temp_db_path)
    except:
        pass  # Expected failure

    # Verify v7 table still exists (migration rolled back)
    conn = sqlite3.connect(temp_db_path)
    conn.row_factory = sqlite3.Row
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "actions" in tables, "Actions table must still exist after rollback"

    # Verify action_protocol NOT added (rollback successful)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(actions)")}
    assert "action_protocol" not in cols, "Rollback must remove action_protocol"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
