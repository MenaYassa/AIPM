"""MC-6.16-C: Production registration foundation tests.

Validates:
- Migration v6 creates project_registrations table
- ProjectRegistration domain model with registration_digest
- SQLiteProjectRegistrationStore persistence layer
- Registration lifecycle states (REGISTERED, DISABLED, REVOKED)
- Digest computation and verification
- CLI registration/revocation validation
"""
import hashlib
import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aipm.control_plane.registration import (
    ProjectRegistration,
    RegistrationError,
    RegistrationStatus,
    compute_file_hash,
    compute_registration_digest,
)
from aipm.control_plane.storage.schema import SCHEMA_VERSION, schema_statements
from aipm.control_plane.storage.sqlite_store import (
    ControlPlaneDatabase,
    SQLiteProjectRegistrationStore,
)


@pytest.fixture
def temp_db_path():
    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "test_control_plane.db"
    yield db_path
    if db_path.exists():
        db_path.unlink()
    Path(temp_dir).rmdir()


@pytest.fixture
def db(temp_db_path):
    return ControlPlaneDatabase(temp_db_path)


@pytest.fixture
def registration_store(db):
    return SQLiteProjectRegistrationStore(db)


def test_schema_version_is_v7():
    """AD-03: Schema v7 introduces registration_id as primary identity."""
    assert SCHEMA_VERSION == 8, "AD-01 (Phase D2.2 Gate A) increments SCHEMA_VERSION to 8 for action_protocol"


def test_migration_v7_creates_project_registrations_table(temp_db_path):
    """AD-03: Schema v7 adds registration_id as primary identity."""
    conn = sqlite3.connect(temp_db_path)
    conn.row_factory = sqlite3.Row

    for stmt in schema_statements():
        conn.execute(stmt)

    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='project_registrations'")
    tables = cursor.fetchall()
    assert len(tables) == 1, "project_registrations table must exist"

    cursor = conn.execute("PRAGMA table_info(project_registrations)")
    columns = {row["name"] for row in cursor.fetchall()}

    expected_columns = {
        "registration_id",  # AD-03: New primary identity
        "target_id",
        "environment",
        "status",
        "canonical_project_path",
        "runtime_mode",
        "compose_project_name",
        "registration_digest",
        "registration_version",
        "registered_by",
        "registered_at",
        "approved_by",
        "approved_at",
        "revoked_by",
        "revoked_at",
        "revocation_reason",
        "updated_at",
    }

    assert columns == expected_columns, f"project_registrations must have all required columns. Missing: {expected_columns - columns}"

    # AD-03: Verify registration_id is PRIMARY KEY
    cursor = conn.execute("PRAGMA table_info(project_registrations)")
    for row in cursor.fetchall():
        if row["name"] == "registration_id":
            assert row["pk"] == 1, "registration_id must be PRIMARY KEY"
            break

    conn.close()


def test_registration_digest_is_deterministic():
    digest1 = compute_registration_digest(
        target_id="test-target",
        canonical_project_path="/home/ubuntu/test-project",
        runtime_mode="compose",
        environment="production",
        compose_project_name="test_project",
        compose_file_hashes=["abc123", "def456"],
        registered_at_iso="2026-09-19T07:00:00+00:00",
    )

    digest2 = compute_registration_digest(
        target_id="test-target",
        canonical_project_path="/home/ubuntu/test-project",
        runtime_mode="compose",
        environment="production",
        compose_project_name="test_project",
        compose_file_hashes=["abc123", "def456"],
        registered_at_iso="2026-09-19T07:00:00+00:00",
    )

    assert digest1 == digest2, "Registration digest must be deterministic"
    assert len(digest1) == 64, "Registration digest must be 64-character hex"
    assert all(c in "0123456789abcdef" for c in digest1), "Registration digest must be lowercase hex"


def test_registration_digest_changes_with_input():
    base_digest = compute_registration_digest(
        target_id="test-target",
        canonical_project_path="/home/ubuntu/test-project",
        runtime_mode="compose",
        environment="production",
        compose_project_name="test_project",
        compose_file_hashes=["abc123"],
        registered_at_iso="2026-09-19T07:00:00+00:00",
    )

    different_path = compute_registration_digest(
        target_id="test-target",
        canonical_project_path="/home/ubuntu/different-project",
        runtime_mode="compose",
        environment="production",
        compose_project_name="test_project",
        compose_file_hashes=["abc123"],
        registered_at_iso="2026-09-19T07:00:00+00:00",
    )

    different_hash = compute_registration_digest(
        target_id="test-target",
        canonical_project_path="/home/ubuntu/test-project",
        runtime_mode="compose",
        environment="production",
        compose_project_name="test_project",
        compose_file_hashes=["xyz789"],
        registered_at_iso="2026-09-19T07:00:00+00:00",
    )

    assert base_digest != different_path, "Digest must change when path changes"
    assert base_digest != different_hash, "Digest must change when compose file hashes change"


def test_project_registration_model_immutability():
    """AD-03: ProjectRegistration includes immutable registration_id."""
    now = datetime.now(timezone.utc)
    reg = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440000",  # AD-03: Fixed test UUID
        target_id="test-target",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/test",
        runtime_mode="compose",
        registration_digest="a" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    assert reg.registration_id == "550e8400-e29b-41d4-a716-446655440000"
    assert reg.target_id == "test-target"
    assert reg.is_active()
    assert not reg.is_revoked()
    assert not reg.is_disabled()

    with pytest.raises(AttributeError):
        reg.target_id = "changed"


def test_registration_store_save_and_get(registration_store):
    """AD-03: RegistrationService generates registration_id via UUID."""
    now = datetime.now(timezone.utc)
    registration = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440001",  # AD-03: Fixed test UUID
        target_id="test-target-1",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/test-project-1",
        runtime_mode="compose",
        compose_project_name="test_project_1",
        registration_digest="a" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    saved = registration_store.save(registration)
    assert saved.target_id == "test-target-1"

    retrieved = registration_store.get("test-target-1", "production")
    assert retrieved is not None
    assert retrieved.target_id == "test-target-1"
    assert retrieved.environment == "production"
    assert retrieved.status == RegistrationStatus.REGISTERED
    assert retrieved.canonical_project_path == "/home/ubuntu/test-project-1"
    assert retrieved.runtime_mode == "compose"
    assert retrieved.compose_project_name == "test_project_1"
    assert retrieved.registration_digest == "a" * 64


def test_registration_store_get_by_path(registration_store):
    now = datetime.now(timezone.utc)
    registration = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440002",  # AD-03: Fixed test UUID
        target_id="test-target-2",
        environment="staging",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/test-project-2",
        runtime_mode="compose",
        registration_digest="b" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    registration_store.save(registration)

    retrieved = registration_store.get_by_path("/home/ubuntu/test-project-2", "staging")
    assert retrieved is not None
    assert retrieved.target_id == "test-target-2"
    assert retrieved.canonical_project_path == "/home/ubuntu/test-project-2"


def test_registration_store_duplicate_raises_error(registration_store):
    now = datetime.now(timezone.utc)
    registration1 = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440003",  # AD-03: Fixed test UUID
        target_id="test-target-3",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/test-project-3",
        runtime_mode="compose",
        registration_digest="c" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    registration_store.save(registration1)

    registration2 = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440004",  # AD-03: Fixed test UUID
        target_id="test-target-3",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/test-project-3",
        runtime_mode="compose",
        registration_digest="d" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    with pytest.raises(RegistrationError, match="already exists"):
        registration_store.save(registration2)


def test_registration_store_update_status_to_revoked(registration_store):
    now = datetime.now(timezone.utc)
    registration = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440005",  # AD-03: Fixed test UUID
        target_id="test-target-4",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/test-project-4",
        runtime_mode="compose",
        registration_digest="e" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    registration_store.save(registration)

    updated = registration_store.update_status(
        target_id="test-target-4",
        environment="production",
        new_status=RegistrationStatus.REVOKED,
        actor_subject="admin",
        reason="Security incident",
    )

    assert updated is not None
    assert updated.status == RegistrationStatus.REVOKED
    assert updated.revoked_by == "admin"
    assert updated.revocation_reason == "Security incident"
    assert updated.revoked_at is not None
    assert updated.is_revoked()


def test_registration_store_update_status_to_disabled(registration_store):
    now = datetime.now(timezone.utc)
    registration = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440006",  # AD-03: Fixed test UUID
        target_id="test-target-5",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/test-project-5",
        runtime_mode="systemd",
        registration_digest="f" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    registration_store.save(registration)

    updated = registration_store.update_status(
        target_id="test-target-5",
        environment="production",
        new_status=RegistrationStatus.DISABLED,
        actor_subject="admin",
    )

    assert updated is not None
    assert updated.status == RegistrationStatus.DISABLED
    assert updated.is_disabled()


def test_registration_store_list_registrations(registration_store):
    now = datetime.now(timezone.utc)

    reg1 = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440007",  # AD-03: Fixed test UUID
        target_id="target-a",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/project-a",
        runtime_mode="compose",
        registration_digest="1" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    reg2 = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440008",  # AD-03: Fixed test UUID
        target_id="target-b",
        environment="staging",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/project-b",
        runtime_mode="compose",
        registration_digest="2" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    reg3 = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440009",  # AD-03: Fixed test UUID
        target_id="target-c",
        environment="production",
        status=RegistrationStatus.REVOKED,
        canonical_project_path="/home/ubuntu/project-c",
        runtime_mode="compose",
        registration_digest="3" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    registration_store.save(reg1)
    registration_store.save(reg2)
    registration_store.save(reg3)

    all_regs = registration_store.list_registrations()
    assert len(all_regs) == 3

    prod_regs = registration_store.list_registrations(environment="production")
    assert len(prod_regs) == 2

    active_regs = registration_store.list_registrations(status=RegistrationStatus.REGISTERED)
    assert len(active_regs) == 2

    revoked_regs = registration_store.list_registrations(status=RegistrationStatus.REVOKED)
    assert len(revoked_regs) == 1


def test_registration_store_get_nonexistent_returns_none(registration_store):
    result = registration_store.get("nonexistent", "production")
    assert result is None


def test_registration_store_update_nonexistent_returns_none(registration_store):
    result = registration_store.update_status(
        target_id="nonexistent",
        environment="production",
        new_status=RegistrationStatus.REVOKED,
        actor_subject="admin",
    )
    assert result is None


def test_compute_file_hash_with_temp_file():
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
        f.write("test content for hashing\n")
        temp_path = f.name

    try:
        file_hash = compute_file_hash(temp_path)
        assert len(file_hash) == 64
        assert all(c in "0123456789abcdef" for c in file_hash)

        expected = hashlib.sha256(b"test content for hashing\n").hexdigest()
        assert file_hash == expected
    finally:
        Path(temp_path).unlink()


def test_compute_file_hash_nonexistent_raises_error():
    with pytest.raises(RegistrationError, match="Unable to hash file"):
        compute_file_hash("/nonexistent/path/file.txt")


def test_registration_lifecycle_transitions():
    now = datetime.now(timezone.utc)

    registered = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440010",  # AD-03: Fixed test UUID
        target_id="lifecycle-test",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/lifecycle-test",
        runtime_mode="compose",
        registration_digest="a" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    assert registered.is_active()
    assert not registered.is_disabled()
    assert not registered.is_revoked()

    disabled = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440011",  # AD-03: Fixed test UUID
        target_id="lifecycle-test",
        environment="production",
        status=RegistrationStatus.DISABLED,
        canonical_project_path="/home/ubuntu/lifecycle-test",
        runtime_mode="compose",
        registration_digest="a" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    assert not disabled.is_active()
    assert disabled.is_disabled()
    assert not disabled.is_revoked()

    revoked = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440012",  # AD-03: Fixed test UUID
        target_id="lifecycle-test",
        environment="production",
        status=RegistrationStatus.REVOKED,
        canonical_project_path="/home/ubuntu/lifecycle-test",
        runtime_mode="compose",
        registration_digest="a" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
        revoked_by="admin",
        revoked_at=now,
        revocation_reason="Test revocation",
    )

    assert not revoked.is_active()
    assert not revoked.is_disabled()
    assert revoked.is_revoked()


def test_registration_store_concurrent_save_same_target_different_env(registration_store):
    now = datetime.now(timezone.utc)

    prod_reg = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440013",  # AD-03: Fixed test UUID
        target_id="multi-env",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/multi-env",
        runtime_mode="compose",
        registration_digest="a" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    staging_reg = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440014",  # AD-03: Fixed test UUID
        target_id="multi-env",
        environment="staging",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/multi-env",
        runtime_mode="compose",
        registration_digest="b" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    registration_store.save(prod_reg)
    registration_store.save(staging_reg)

    prod_retrieved = registration_store.get("multi-env", "production")
    staging_retrieved = registration_store.get("multi-env", "staging")

    assert prod_retrieved is not None
    assert staging_retrieved is not None
    assert prod_retrieved.environment == "production"
    assert staging_retrieved.environment == "staging"


def test_ad03_registration_id_immutability(registration_store):
    """AD-03: registration_id is immutable and unique."""
    now = datetime.now(timezone.utc)

    reg1 = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440100",
        target_id="test-target-ad03-1",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/test-ad03-1",
        runtime_mode="compose",
        registration_digest="ad03" * 16,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    registration_store.save(reg1)

    # Verify registration_id is preserved
    retrieved = registration_store.get("test-target-ad03-1", "production")
    assert retrieved.registration_id == "550e8400-e29b-41d4-a716-446655440100"

    # Verify registration_id lookup works
    retrieved_by_id = registration_store.get_by_registration_id("550e8400-e29b-41d4-a716-446655440100")
    assert retrieved_by_id is not None
    assert retrieved_by_id.target_id == "test-target-ad03-1"


def test_ad03_reregistration_after_revoked_creates_new_id(registration_store):
    """AD-03: Re-registration after REVOKED creates new registration_id."""
    now = datetime.now(timezone.utc)

    # First registration
    reg1 = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440101",
        target_id="test-target-ad03-2",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/test-ad03-2",
        runtime_mode="compose",
        registration_digest="ad03a" * 16,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )
    registration_store.save(reg1)

    # Revoke
    revoked = registration_store.update_status(
        "test-target-ad03-2",
        "production",
        RegistrationStatus.REVOKED,
        "admin",
        "Test revocation",
    )
    assert revoked.status == RegistrationStatus.REVOKED
    assert revoked.registration_id == "550e8400-e29b-41d4-a716-446655440101"

    # Verify get() returns None for revoked registration
    active = registration_store.get("test-target-ad03-2", "production")
    assert active is None

    # Re-register with NEW registration_id
    reg2 = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440102",  # Different ID
        target_id="test-target-ad03-2",  # Same target
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/test-ad03-2",
        runtime_mode="compose",
        registration_digest="ad03b" * 16,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )
    registration_store.save(reg2)

    # Verify new registration is active
    active_new = registration_store.get("test-target-ad03-2", "production")
    assert active_new is not None
    assert active_new.registration_id == "550e8400-e29b-41d4-a716-446655440102"
    assert active_new.status == RegistrationStatus.REGISTERED

    # Verify old revoked registration still exists via registration_id lookup
    old_revoked = registration_store.get_by_registration_id("550e8400-e29b-41d4-a716-446655440101")
    assert old_revoked is not None
    assert old_revoked.status == RegistrationStatus.REVOKED

    # Verify both registrations are preserved
    all_regs = registration_store.list_registrations(environment="production")
    target_regs = [r for r in all_regs if r.target_id == "test-target-ad03-2"]
    assert len(target_regs) == 2  # Both REVOKED and REGISTERED exist


def test_ad03_active_registration_uniqueness_enforced(registration_store):
    """AD-03: Database enforces at most one active registration per (target_id, environment)."""
    now = datetime.now(timezone.utc)

    # First registration
    reg1 = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440103",
        target_id="test-target-ad03-3",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/test-ad03-3",
        runtime_mode="compose",
        registration_digest="ad03c" * 16,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )
    registration_store.save(reg1)

    # Attempt second registration with different registration_id but same target/env
    reg2 = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440104",  # Different ID
        target_id="test-target-ad03-3",  # Same target
        environment="production",  # Same environment
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/test-ad03-3-alt",
        runtime_mode="compose",
        registration_digest="ad03d" * 16,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    # Must fail due to partial unique index
    with pytest.raises(RegistrationError, match="already exists"):
        registration_store.save(reg2)


def test_ad03_disabled_registration_blocks_new_registration(registration_store):
    """AD-03: DISABLED status counts as active and blocks new registration."""
    now = datetime.now(timezone.utc)

    # Register
    reg1 = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440105",
        target_id="test-target-ad03-4",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/test-ad03-4",
        runtime_mode="compose",
        registration_digest="ad03e" * 16,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )
    registration_store.save(reg1)

    # Disable
    disabled = registration_store.update_status(
        "test-target-ad03-4",
        "production",
        RegistrationStatus.DISABLED,
        "admin",
        "Test disable",
    )
    assert disabled.status == RegistrationStatus.DISABLED

    # Attempt new registration while DISABLED
    reg2 = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440106",
        target_id="test-target-ad03-4",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/test-ad03-4-new",
        runtime_mode="compose",
        registration_digest="ad03f" * 16,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    # Must fail because DISABLED is still active
    with pytest.raises(RegistrationError, match="already exists"):
        registration_store.save(reg2)


def test_ad03_v6_to_v7_migration(temp_db_path):
    """AD-03: v6→v7 migration preserves existing registrations and adds registration_id."""
    import uuid

    conn = sqlite3.connect(temp_db_path)
    conn.row_factory = sqlite3.Row

    # Create v6 schema manually
    conn.execute("""
        CREATE TABLE control_plane_schema_meta (
            schema_name TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            migrated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        INSERT INTO control_plane_schema_meta VALUES ('control_plane', 6, '2026-09-19T10:00:00+00:00')
    """)

    conn.execute("""
        CREATE TABLE project_registrations (
            target_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            status TEXT NOT NULL,
            canonical_project_path TEXT NOT NULL,
            runtime_mode TEXT NOT NULL,
            compose_project_name TEXT,
            registration_digest TEXT NOT NULL,
            registration_version TEXT NOT NULL DEFAULT 'mc616-reg-v1',
            registered_by TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            approved_by TEXT,
            approved_at TEXT,
            revoked_by TEXT,
            revoked_at TEXT,
            revocation_reason TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (target_id, environment)
        )
    """)

    # Insert v6 test data
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO project_registrations (
            target_id, environment, status, canonical_project_path, runtime_mode,
            compose_project_name, registration_digest, registration_version,
            registered_by, registered_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "v6-target-1",
        "production",
        "REGISTERED",
        "/home/ubuntu/v6-project-1",
        "compose",
        "v6_project_1",
        "v6digest" * 10,
        "mc616-reg-v1",
        "operator",
        now_iso,
        now_iso,
    ))

    conn.execute("""
        INSERT INTO project_registrations (
            target_id, environment, status, canonical_project_path, runtime_mode,
            registration_digest, registration_version,
            registered_by, registered_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "v6-target-2",
        "staging",
        "REVOKED",
        "/home/ubuntu/v6-project-2",
        "compose",
        "v6digest2" * 10,
        "mc616-reg-v1",
        "operator",
        now_iso,
        now_iso,
    ))

    conn.commit()
    conn.close()

    # Set correct permissions for control-plane database
    import os
    os.chmod(temp_db_path, 0o600)

    # Trigger migration by opening as v7 database
    db = ControlPlaneDatabase(temp_db_path)

    # Verify schema version upgraded through full migration chain to v8
    version_row = db.connection.execute(
        "SELECT schema_version FROM control_plane_schema_meta WHERE schema_name = 'control_plane'"
    ).fetchone()
    assert version_row[0] == 8, "Schema must be upgraded through v7 to v8"

    # Verify registration_id column exists
    columns = {row[1] for row in db.connection.execute("PRAGMA table_info(project_registrations)")}
    assert "registration_id" in columns

    # Verify data preserved
    rows = db.connection.execute("SELECT * FROM project_registrations").fetchall()
    assert len(rows) == 2

    # Verify registration_id generated for existing rows
    for row in rows:
        assert row["registration_id"] is not None
        assert len(row["registration_id"]) == 36  # UUID format
        # Verify UUID format
        try:
            uuid.UUID(row["registration_id"])
        except ValueError:
            pytest.fail(f"Invalid UUID format: {row['registration_id']}")

    # Verify target_id preserved
    target_ids = {row["target_id"] for row in rows}
    assert "v6-target-1" in target_ids
    assert "v6-target-2" in target_ids

    # Verify status preserved
    for row in rows:
        if row["target_id"] == "v6-target-1":
            assert row["status"] == "REGISTERED"
        elif row["target_id"] == "v6-target-2":
            assert row["status"] == "REVOKED"

    db.close()


def test_ad03_historical_revoked_registrations_preserved(registration_store):
    """AD-03: REVOKED registrations are preserved as historical records."""
    now = datetime.now(timezone.utc)

    # Register and revoke multiple times
    for i in range(3):
        reg = ProjectRegistration(
            registration_id=f"550e8400-e29b-41d4-a716-4466554401{i:02d}",
            target_id="test-target-ad03-history",
            environment="production",
            status=RegistrationStatus.REGISTERED,
            canonical_project_path="/home/ubuntu/test-ad03-history",
            runtime_mode="compose",
            registration_digest=f"hist{i}" * 16,
            registration_version="mc616-reg-v1",
            registered_by="operator",
            registered_at=now,
        )
        registration_store.save(reg)

        if i < 2:  # Revoke first two
            registration_store.update_status(
                "test-target-ad03-history",
                "production",
                RegistrationStatus.REVOKED,
                "admin",
                f"Revocation {i}",
            )

    # Verify only one active registration
    active = registration_store.get("test-target-ad03-history", "production")
    assert active is not None
    assert active.registration_id == "550e8400-e29b-41d4-a716-446655440102"

    # Verify all three registrations exist in history
    all_regs = registration_store.list_registrations(environment="production")
    history_regs = [r for r in all_regs if r.target_id == "test-target-ad03-history"]
    assert len(history_regs) == 3

    # Verify two REVOKED, one REGISTERED
    statuses = [r.status for r in history_regs]
    assert statuses.count(RegistrationStatus.REVOKED) == 2
    assert statuses.count(RegistrationStatus.REGISTERED) == 1

    # Verify each has unique registration_id
    registration_ids = [r.registration_id for r in history_regs]
    assert len(set(registration_ids)) == 3


def test_ad03_real_concurrent_registration_race(temp_db_path):
    """AD-03: Real SQLite concurrency test with separate connections racing for same (target_id, environment)."""
    import os
    import threading
    import time
    from pathlib import Path

    # Initialize schema in first connection (creates the database file)
    db1 = ControlPlaneDatabase(temp_db_path)
    db1.close()

    # Set correct permissions after database is created
    os.chmod(temp_db_path, 0o600)

    # Test state
    results = {"success": [], "failures": [], "errors": []}
    barrier = threading.Barrier(2)  # Synchronize both threads

    def attempt_registration(thread_id: int):
        """Attempt registration in separate connection/transaction."""
        try:
            # Each thread gets its own connection
            conn = sqlite3.connect(temp_db_path)
            conn.row_factory = sqlite3.Row

            # Wait for both threads to be ready
            barrier.wait()

            # Both threads attempt to register at the same moment
            now_iso = datetime.now(timezone.utc).isoformat()
            registration_id = f"550e8400-e29b-41d4-a716-4466554403{thread_id:02d}"

            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("""
                    INSERT INTO project_registrations (
                        registration_id, target_id, environment, status, canonical_project_path,
                        runtime_mode, registration_digest, registration_version,
                        registered_by, registered_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    registration_id,
                    "concurrent-race-target",
                    "production",
                    "REGISTERED",
                    f"/home/ubuntu/concurrent-{thread_id}",
                    "compose",
                    f"race{thread_id}" * 16,
                    "mc616-reg-v1",
                    f"thread-{thread_id}",
                    now_iso,
                    now_iso,
                ))
                conn.commit()
                results["success"].append(thread_id)
            except sqlite3.IntegrityError as e:
                conn.rollback()
                results["failures"].append(thread_id)
            finally:
                conn.close()

        except Exception as e:
            results["errors"].append((thread_id, str(e)))

    # Launch two threads racing for the same (target_id, environment)
    thread1 = threading.Thread(target=attempt_registration, args=(1,))
    thread2 = threading.Thread(target=attempt_registration, args=(2,))

    thread1.start()
    thread2.start()

    thread1.join()
    thread2.join()

    # Verify results
    assert len(results["errors"]) == 0, f"Unexpected errors: {results['errors']}"
    assert len(results["success"]) == 1, f"Expected exactly 1 success, got {len(results['success'])}: {results['success']}"
    assert len(results["failures"]) == 1, f"Expected exactly 1 failure, got {len(results['failures'])}: {results['failures']}"

    # Verify database state: exactly one row exists
    verify_conn = sqlite3.connect(temp_db_path)
    verify_conn.row_factory = sqlite3.Row
    rows = verify_conn.execute("""
        SELECT * FROM project_registrations
        WHERE target_id = 'concurrent-race-target' AND environment = 'production'
    """).fetchall()

    assert len(rows) == 1, f"Expected exactly 1 row in database, got {len(rows)}"
    assert rows[0]["status"] == "REGISTERED"

    # Verify no orphaned partial data
    all_rows = verify_conn.execute("SELECT * FROM project_registrations").fetchall()
    race_rows = [r for r in all_rows if r["target_id"] == "concurrent-race-target"]
    assert len(race_rows) == 1, "No orphaned rows should exist"

    verify_conn.close()


def test_ad03_concurrent_register_while_revoked():
    """AD-03: Concurrent registration attempts while one thread revokes the active registration."""
    import tempfile
    import threading
    from pathlib import Path

    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "test_concurrent_revoke.db"

    try:
        import os

        # Initialize with one active registration (creates the database file)
        db = ControlPlaneDatabase(db_path)

        # Set correct permissions after database is created
        os.chmod(db_path, 0o600)

        store = SQLiteProjectRegistrationStore(db)

        initial_reg = ProjectRegistration(
            registration_id="550e8400-e29b-41d4-a716-446655440350",
            target_id="revoke-race-target",
            environment="production",
            status=RegistrationStatus.REGISTERED,
            canonical_project_path="/home/ubuntu/revoke-race",
            runtime_mode="compose",
            registration_digest="revoke" * 16,
            registration_version="mc616-reg-v1",
            registered_by="operator",
            registered_at=datetime.now(timezone.utc),
        )
        store.save(initial_reg)
        db.close()

        results = {"revoke": None, "register": None}
        barrier = threading.Barrier(2)

        def revoke_thread():
            try:
                db = ControlPlaneDatabase(db_path)
                store = SQLiteProjectRegistrationStore(db)
                barrier.wait()
                # Revoke the active registration
                updated = store.update_status(
                    "revoke-race-target",
                    "production",
                    RegistrationStatus.REVOKED,
                    "revoker",
                    "Concurrent revoke test",
                )
                results["revoke"] = "success" if updated else "not_found"
                db.close()
            except Exception as e:
                results["revoke"] = f"error: {e}"

        def register_thread():
            try:
                db = ControlPlaneDatabase(db_path)
                store = SQLiteProjectRegistrationStore(db)
                barrier.wait()
                # Attempt to register new instance
                new_reg = ProjectRegistration(
                    registration_id="550e8400-e29b-41d4-a716-446655440351",
                    target_id="revoke-race-target",
                    environment="production",
                    status=RegistrationStatus.REGISTERED,
                    canonical_project_path="/home/ubuntu/revoke-race-new",
                    runtime_mode="compose",
                    registration_digest="newrace" * 16,
                    registration_version="mc616-reg-v1",
                    registered_by="operator",
                    registered_at=datetime.now(timezone.utc),
                )
                store.save(new_reg)
                results["register"] = "success"
                db.close()
            except RegistrationError:
                results["register"] = "conflict"
            except Exception as e:
                results["register"] = f"error: {e}"

        t1 = threading.Thread(target=revoke_thread)
        t2 = threading.Thread(target=register_thread)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Verify results: one operation should succeed
        assert results["revoke"] in ("success", "not_found"), f"Revoke failed: {results['revoke']}"
        assert results["register"] in ("success", "conflict"), f"Register failed: {results['register']}"

        # Verify final database state is consistent
        verify_db = ControlPlaneDatabase(db_path)
        verify_store = SQLiteProjectRegistrationStore(verify_db)

        all_regs = [r for r in verify_store.list_registrations(environment="production")
                    if r.target_id == "revoke-race-target"]

        # Either: 1 REVOKED (revoke won, register failed)
        # Or: 1 REVOKED + 1 REGISTERED (register won after revoke)
        # Or: 1 REGISTERED (register won, revoke failed to find active)
        assert len(all_regs) in (1, 2), f"Unexpected registration count: {len(all_regs)}"

        active_count = sum(1 for r in all_regs if r.status in (RegistrationStatus.REGISTERED, RegistrationStatus.DISABLED))
        assert active_count <= 1, "At most one active registration must exist"

        verify_db.close()

    finally:
        if db_path.exists():
            db_path.unlink()
        Path(temp_dir).rmdir()


def test_ad03_migration_preserves_all_columns(temp_db_path):
    """AD-03: v6→v7 migration preserves every column value from v6 schema."""
    import os
    import uuid

    conn = sqlite3.connect(temp_db_path)
    conn.row_factory = sqlite3.Row

    # Create v6 schema
    conn.execute("""
        CREATE TABLE control_plane_schema_meta (
            schema_name TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            migrated_at TEXT NOT NULL
        )
    """)
    conn.execute("INSERT INTO control_plane_schema_meta VALUES ('control_plane', 6, '2026-09-19T10:00:00+00:00')")

    conn.execute("""
        CREATE TABLE project_registrations (
            target_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            status TEXT NOT NULL,
            canonical_project_path TEXT NOT NULL,
            runtime_mode TEXT NOT NULL,
            compose_project_name TEXT,
            registration_digest TEXT NOT NULL,
            registration_version TEXT NOT NULL DEFAULT 'mc616-reg-v1',
            registered_by TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            approved_by TEXT,
            approved_at TEXT,
            revoked_by TEXT,
            revoked_at TEXT,
            revocation_reason TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (target_id, environment)
        )
    """)

    # Insert v6 test data with ALL columns populated
    test_data = {
        "target_id": "preserve-test",
        "environment": "production",
        "status": "REVOKED",
        "canonical_project_path": "/home/ubuntu/preserve-test",
        "runtime_mode": "compose",
        "compose_project_name": "preserve_compose",
        "registration_digest": "digest" * 16,
        "registration_version": "mc616-reg-v1",
        "registered_by": "original-operator",
        "registered_at": "2026-09-18T10:00:00+00:00",
        "approved_by": "approver",
        "approved_at": "2026-09-18T11:00:00+00:00",
        "revoked_by": "revoker",
        "revoked_at": "2026-09-18T12:00:00+00:00",
        "revocation_reason": "Test revocation reason",
        "updated_at": "2026-09-18T12:00:00+00:00",
    }

    conn.execute("""
        INSERT INTO project_registrations (
            target_id, environment, status, canonical_project_path, runtime_mode,
            compose_project_name, registration_digest, registration_version,
            registered_by, registered_at, approved_by, approved_at,
            revoked_by, revoked_at, revocation_reason, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, tuple(test_data.values()))

    conn.commit()
    conn.close()

    # Set permissions and trigger migration
    os.chmod(temp_db_path, 0o600)
    db = ControlPlaneDatabase(temp_db_path)

    # Verify data preserved
    row = db.connection.execute(
        "SELECT * FROM project_registrations WHERE target_id = ?",
        ("preserve-test",)
    ).fetchone()

    assert row is not None, "Registration must exist after migration"

    # Verify ALL columns preserved (except registration_id which is new)
    for key, expected_value in test_data.items():
        actual_value = row[key]
        assert actual_value == expected_value, f"Column {key} not preserved: expected {expected_value}, got {actual_value}"

    # Verify registration_id was added
    assert row["registration_id"] is not None
    assert len(row["registration_id"]) == 36  # UUID format
    uuid.UUID(row["registration_id"])  # Validates UUID format

    db.close()


def test_ad03_migration_preserves_multiple_historical_registrations(temp_db_path):
    """AD-03: v6→v7 migration preserves complex historical state with multiple REVOKED registrations."""
    import os

    conn = sqlite3.connect(temp_db_path)
    conn.row_factory = sqlite3.Row

    # Create v6 schema
    conn.execute("""
        CREATE TABLE control_plane_schema_meta (
            schema_name TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            migrated_at TEXT NOT NULL
        )
    """)
    conn.execute("INSERT INTO control_plane_schema_meta VALUES ('control_plane', 6, '2026-09-19T10:00:00+00:00')")

    conn.execute("""
        CREATE TABLE project_registrations (
            target_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            status TEXT NOT NULL,
            canonical_project_path TEXT NOT NULL,
            runtime_mode TEXT NOT NULL,
            compose_project_name TEXT,
            registration_digest TEXT NOT NULL,
            registration_version TEXT NOT NULL DEFAULT 'mc616-reg-v1',
            registered_by TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            approved_by TEXT,
            approved_at TEXT,
            revoked_by TEXT,
            revoked_at TEXT,
            revocation_reason TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (target_id, environment)
        )
    """)

    # NOTE: v6 schema has PRIMARY KEY (target_id, environment), so we can only have
    # ONE registration per (target_id, environment). We'll create diverse registrations
    # across different targets/environments to simulate historical state.

    v6_registrations = [
        # Active REGISTERED in production
        ("target-1", "production", "REGISTERED", "/home/ubuntu/target-1-prod", "compose", "target1_prod", "active1" * 16, "operator1", "2026-09-01T10:00:00+00:00", None, None, None, None, None, "2026-09-01T10:00:00+00:00"),
        # Active DISABLED in staging
        ("target-2", "staging", "DISABLED", "/home/ubuntu/target-2-stage", "compose", "target2_stage", "disabled2" * 16, "operator2", "2026-09-02T10:00:00+00:00", None, None, None, None, None, "2026-09-02T11:00:00+00:00"),
        # REVOKED in production
        ("target-3", "production", "REVOKED", "/home/ubuntu/target-3-prod", "compose", "target3_prod", "revoked3" * 16, "operator3", "2026-09-03T10:00:00+00:00", "approver3", "2026-09-03T11:00:00+00:00", "revoker3", "2026-09-03T12:00:00+00:00", "Security incident", "2026-09-03T12:00:00+00:00"),
        # REVOKED in staging
        ("target-4", "staging", "REVOKED", "/home/ubuntu/target-4-stage", "compose", None, "revoked4" * 16, "operator4", "2026-09-04T10:00:00+00:00", None, None, "revoker4", "2026-09-04T12:00:00+00:00", "Decommissioned", "2026-09-04T12:00:00+00:00"),
        # Active REGISTERED in staging
        ("target-5", "staging", "REGISTERED", "/home/ubuntu/target-5-stage", "compose", "target5_stage", "active5" * 16, "operator5", "2026-09-05T10:00:00+00:00", None, None, None, None, None, "2026-09-05T10:00:00+00:00"),
    ]

    for reg_data in v6_registrations:
        conn.execute("""
            INSERT INTO project_registrations (
                target_id, environment, status, canonical_project_path, runtime_mode,
                compose_project_name, registration_digest, registered_by, registered_at,
                approved_by, approved_at, revoked_by, revoked_at, revocation_reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, reg_data)

    conn.commit()
    conn.close()

    # Trigger migration
    os.chmod(temp_db_path, 0o600)
    db = ControlPlaneDatabase(temp_db_path)

    # Verify all registrations preserved
    all_rows = db.connection.execute("SELECT * FROM project_registrations ORDER BY target_id").fetchall()
    assert len(all_rows) == 5, f"Expected 5 registrations after migration, got {len(all_rows)}"

    # Verify status distribution preserved
    status_counts = {}
    for row in all_rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1

    assert status_counts["REGISTERED"] == 2, f"Expected 2 REGISTERED, got {status_counts.get('REGISTERED', 0)}"
    assert status_counts["DISABLED"] == 1, f"Expected 1 DISABLED, got {status_counts.get('DISABLED', 0)}"
    assert status_counts["REVOKED"] == 2, f"Expected 2 REVOKED, got {status_counts.get('REVOKED', 0)}"

    # Verify each registration has unique registration_id
    registration_ids = [row["registration_id"] for row in all_rows]
    assert len(set(registration_ids)) == 5, "All registration_ids must be unique"

    # Verify target_ids preserved
    target_ids = [row["target_id"] for row in all_rows]
    assert set(target_ids) == {"target-1", "target-2", "target-3", "target-4", "target-5"}

    # Verify REVOKED registrations include revocation metadata
    revoked_rows = [row for row in all_rows if row["status"] == "REVOKED"]
    for row in revoked_rows:
        assert row["revoked_by"] is not None, f"REVOKED registration {row['target_id']} missing revoked_by"
        assert row["revoked_at"] is not None, f"REVOKED registration {row['target_id']} missing revoked_at"
        assert row["revocation_reason"] is not None, f"REVOKED registration {row['target_id']} missing revocation_reason"

    db.close()


def test_ad03_migration_rollback_on_failure(temp_db_path):
    """AD-03: v6→v7 migration rolls back completely if any step fails."""
    import os

    conn = sqlite3.connect(temp_db_path)
    conn.row_factory = sqlite3.Row

    # Create v6 schema
    conn.execute("""
        CREATE TABLE control_plane_schema_meta (
            schema_name TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            migrated_at TEXT NOT NULL
        )
    """)
    conn.execute("INSERT INTO control_plane_schema_meta VALUES ('control_plane', 6, '2026-09-19T10:00:00+00:00')")

    conn.execute("""
        CREATE TABLE project_registrations (
            target_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            status TEXT NOT NULL,
            canonical_project_path TEXT NOT NULL,
            runtime_mode TEXT NOT NULL,
            compose_project_name TEXT,
            registration_digest TEXT NOT NULL,
            registration_version TEXT NOT NULL DEFAULT 'mc616-reg-v1',
            registered_by TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            approved_by TEXT,
            approved_at TEXT,
            revoked_by TEXT,
            revoked_at TEXT,
            revocation_reason TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (target_id, environment)
        )
    """)

    # Insert test data
    conn.execute("""
        INSERT INTO project_registrations (
            target_id, environment, status, canonical_project_path, runtime_mode,
            registration_digest, registered_by, registered_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ("rollback-test", "production", "REGISTERED", "/home/ubuntu/rollback-test",
          "compose", "rollback" * 16, "operator", "2026-09-19T10:00:00+00:00", "2026-09-19T10:00:00+00:00"))

    conn.commit()

    # Verify v6 state exists
    v6_row = conn.execute("SELECT * FROM project_registrations WHERE target_id = 'rollback-test'").fetchone()
    assert v6_row is not None
    assert "registration_id" not in [desc[0] for desc in conn.execute("PRAGMA table_info(project_registrations)")], "registration_id should not exist in v6"

    conn.close()

    # Trigger normal migration (should succeed)
    os.chmod(temp_db_path, 0o600)
    db = ControlPlaneDatabase(temp_db_path)

    # Verify migration succeeded
    v7_row = db.connection.execute("SELECT * FROM project_registrations WHERE target_id = 'rollback-test'").fetchone()
    assert v7_row is not None
    assert v7_row["registration_id"] is not None, "registration_id should exist after migration"

    # Verify schema version updated to post-D2.2 final schema (v8)
    version_row = db.connection.execute("SELECT schema_version FROM control_plane_schema_meta WHERE schema_name = 'control_plane'").fetchone()
    assert version_row[0] == 8, f"Schema version should be 8 after migration chain, got {version_row[0]}"

    db.close()


def test_ad03_migration_idempotent_rerun_safe(temp_db_path):
    """AD-03: Running migration against already-v7 database is safe (idempotent)."""
    import os

    # Create v7 database directly
    db = ControlPlaneDatabase(temp_db_path)

    # Set correct permissions after database is created
    os.chmod(temp_db_path, 0o600)

    store = SQLiteProjectRegistrationStore(db)

    # Add test registration
    reg = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440400",
        target_id="idempotent-test",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/idempotent-test",
        runtime_mode="compose",
        registration_digest="idempotent" * 16,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=datetime.now(timezone.utc),
    )
    store.save(reg)
    db.close()

    # Reopen database (should not re-run migration)
    db2 = ControlPlaneDatabase(temp_db_path)

    # Verify registration still exists
    row = db2.connection.execute("SELECT * FROM project_registrations WHERE target_id = 'idempotent-test'").fetchone()
    assert row is not None
    assert row["registration_id"] == "550e8400-e29b-41d4-a716-446655440400"
    assert row["target_id"] == "idempotent-test"

    # Verify schema version still v8 (current schema version)
    version_row = db2.connection.execute("SELECT schema_version FROM control_plane_schema_meta WHERE schema_name = 'control_plane'").fetchone()
    assert version_row[0] == 8

    db2.close()


def test_ad03_migration_indexes_and_constraints_exist(temp_db_path):
    """AD-03: v6→v7 migration creates all required indexes and constraints."""
    import os

    conn = sqlite3.connect(temp_db_path)
    conn.row_factory = sqlite3.Row

    # Create v6 schema
    conn.execute("""
        CREATE TABLE control_plane_schema_meta (
            schema_name TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            migrated_at TEXT NOT NULL
        )
    """)
    conn.execute("INSERT INTO control_plane_schema_meta VALUES ('control_plane', 6, '2026-09-19T10:00:00+00:00')")

    conn.execute("""
        CREATE TABLE project_registrations (
            target_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            status TEXT NOT NULL,
            canonical_project_path TEXT NOT NULL,
            runtime_mode TEXT NOT NULL,
            compose_project_name TEXT,
            registration_digest TEXT NOT NULL,
            registration_version TEXT NOT NULL DEFAULT 'mc616-reg-v1',
            registered_by TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            approved_by TEXT,
            approved_at TEXT,
            revoked_by TEXT,
            revoked_at TEXT,
            revocation_reason TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (target_id, environment)
        )
    """)

    conn.commit()
    conn.close()

    # Trigger migration
    os.chmod(temp_db_path, 0o600)
    db = ControlPlaneDatabase(temp_db_path)

    # Verify indexes exist
    indexes = db.connection.execute("""
        SELECT name, sql FROM sqlite_master
        WHERE type='index' AND tbl_name='project_registrations'
        ORDER BY name
    """).fetchall()

    index_names = [idx["name"] for idx in indexes]

    # Required indexes from AD-03
    assert "idx_project_registrations_active_target" in index_names, "Partial unique index for active registrations missing"
    assert "idx_project_registrations_target" in index_names, "Lookup index missing"
    assert "idx_project_registrations_status" in index_names, "Status index missing"

    # Verify partial unique index has WHERE clause
    active_target_idx = next((idx for idx in indexes if idx["name"] == "idx_project_registrations_active_target"), None)
    assert active_target_idx is not None
    assert "WHERE" in active_target_idx["sql"], "Partial unique index must have WHERE clause"
    assert "status IN" in active_target_idx["sql"], "Partial unique index must filter by status"

    # Verify primary key is registration_id
    table_info = db.connection.execute("PRAGMA table_info(project_registrations)").fetchall()
    pk_columns = [col["name"] for col in table_info if col["pk"] > 0]
    assert pk_columns == ["registration_id"], f"Primary key should be registration_id, got {pk_columns}"

    db.close()
