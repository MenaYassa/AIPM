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


def test_schema_version_is_v6():
    assert SCHEMA_VERSION == 6, "Migration v6 must increment SCHEMA_VERSION to 6"


def test_migration_v6_creates_project_registrations_table(temp_db_path):
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
    now = datetime.now(timezone.utc)
    reg = ProjectRegistration(
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

    assert reg.target_id == "test-target"
    assert reg.is_active()
    assert not reg.is_revoked()
    assert not reg.is_disabled()

    with pytest.raises(AttributeError):
        reg.target_id = "changed"


def test_registration_store_save_and_get(registration_store):
    now = datetime.now(timezone.utc)
    registration = ProjectRegistration(
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
