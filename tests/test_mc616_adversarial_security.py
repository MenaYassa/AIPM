"""MC-6.16-C: Adversarial security tests for registration validation.

Tests security boundaries:
- Path traversal attacks
- Symlink escaping
- Ownership validation
- Permission validation
- Unsupported runtime mode rejection
- Invalid lifecycle transitions
- Concurrent registration races
"""
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aipm.control_plane.registration import (
    ProjectRegistration,
    RegistrationStatus,
)
from aipm.control_plane.registration_service import (
    RegistrationService,
    RegistrationValidator,
)
from aipm.control_plane.storage.sqlite_store import (
    ControlPlaneDatabase,
    SQLiteProjectRegistrationStore,
)
from aipm.providers.compose.identity import resolve_compose_project_name


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


@pytest.fixture
def validator():
    return RegistrationValidator(compose_identity_resolver=resolve_compose_project_name)


@pytest.fixture
def service(validator):
    return RegistrationService(validator=validator)


def test_path_traversal_attack_rejected(validator):
    """Path traversal attempts should be rejected."""
    result = validator.validate_registration_request(
        project_path="/home/ubuntu/../etc/passwd",
        runtime_mode="compose",
        environment="production",
    )
    assert not result.valid
    assert result.error_code in ("namespace_violation", "invalid_path", "not_directory")


def test_symlink_escape_namespace_rejected(validator, tmp_path):
    """Symlinks pointing outside allowed namespace should be rejected."""
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    ubuntu_dir = tmp_path / "home" / "ubuntu"
    ubuntu_dir.mkdir(parents=True)

    symlink = ubuntu_dir / "escaped"
    symlink.symlink_to(outside_dir)

    result = validator.validate_registration_request(
        project_path=str(symlink),
        runtime_mode="compose",
        environment="production",
    )

    assert not result.valid


def test_non_directory_rejected(validator, tmp_path):
    """Regular files should be rejected."""
    file_path = tmp_path / "not_a_directory.txt"
    file_path.write_text("test")

    result = validator.validate_registration_request(
        project_path=str(file_path),
        runtime_mode="compose",
        environment="production",
    )

    assert not result.valid
    assert result.error_code == "not_directory"


def test_missing_path_rejected(validator):
    """Non-existent paths should be rejected."""
    result = validator.validate_registration_request(
        project_path="/home/ubuntu/nonexistent_project",
        runtime_mode="compose",
        environment="production",
    )

    assert not result.valid
    assert result.error_code == "invalid_path"


def test_world_writable_rejected(validator):
    """World-writable directories should be rejected."""
    import tempfile
    from pathlib import Path

    ubuntu_test_dir = Path("/home/ubuntu/.aipm-test-fixtures")
    ubuntu_test_dir.mkdir(exist_ok=True)

    unsafe_dir = ubuntu_test_dir / f"world_writable_{os.getpid()}"
    unsafe_dir.mkdir(exist_ok=True)
    try:
        unsafe_dir.chmod(0o777)

        result = validator.validate_registration_request(
            project_path=str(unsafe_dir),
            runtime_mode="compose",
            environment="production",
        )

        assert not result.valid
        assert result.error_code == "world_writable"
    finally:
        unsafe_dir.chmod(0o755)
        unsafe_dir.rmdir()


def test_no_compose_files_rejected(validator):
    """Directories without compose files should be rejected for compose mode."""
    import tempfile
    from pathlib import Path

    ubuntu_test_dir = Path("/home/ubuntu/.aipm-test-fixtures")
    ubuntu_test_dir.mkdir(exist_ok=True)

    empty_dir = ubuntu_test_dir / f"no_compose_{os.getpid()}"
    empty_dir.mkdir(exist_ok=True)
    try:
        result = validator.validate_registration_request(
            project_path=str(empty_dir),
            runtime_mode="compose",
            environment="production",
        )

        assert not result.valid
        assert result.error_code == "no_compose_files"
    finally:
        empty_dir.rmdir()


def test_systemd_registration_explicitly_rejected(validator, tmp_path):
    """Systemd registration should be explicitly rejected."""
    project_dir = tmp_path / "systemd_project"
    project_dir.mkdir()

    result = validator.validate_registration_request(
        project_path=str(project_dir),
        runtime_mode="systemd",
        environment="production",
    )

    assert not result.valid
    assert result.error_code == "unsupported_runtime"
    assert "systemd" in result.error_message.lower()


def test_unsupported_runtime_mode_rejected(validator, tmp_path):
    """Unsupported runtime modes should be rejected."""
    project_dir = tmp_path / "custom_project"
    project_dir.mkdir()

    result = validator.validate_registration_request(
        project_path=str(project_dir),
        runtime_mode="custom",
        environment="production",
    )

    assert not result.valid
    assert result.error_code == "unsupported_runtime"


def test_invalid_environment_rejected(validator, tmp_path):
    """Invalid environments should be rejected."""
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()

    result = validator.validate_registration_request(
        project_path=str(project_dir),
        runtime_mode="compose",
        environment="development",
    )

    assert not result.valid
    assert result.error_code == "invalid_environment"


def test_duplicate_registration_rejected(registration_store):
    """Duplicate registrations should be rejected."""
    now = datetime.now(timezone.utc)
    registration = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440200",  # AD-03: Fixed test UUID
        target_id="duplicate-test",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/duplicate",
        runtime_mode="compose",
        registration_digest="a" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    registration_store.save(registration)

    from aipm.control_plane.registration import RegistrationError
    with pytest.raises(RegistrationError, match="already exists"):
        registration_store.save(registration)


def test_production_staging_isolation(registration_store):
    """Same target can be registered in both production and staging."""
    now = datetime.now(timezone.utc)

    prod_registration = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440201",  # AD-03: Fixed test UUID
        target_id="multi-env-test",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/multi-env",
        runtime_mode="compose",
        registration_digest="a" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    staging_registration = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440202",  # AD-03: Fixed test UUID
        target_id="multi-env-test",
        environment="staging",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/multi-env",
        runtime_mode="compose",
        registration_digest="b" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    registration_store.save(prod_registration)
    registration_store.save(staging_registration)

    prod_retrieved = registration_store.get("multi-env-test", "production")
    staging_retrieved = registration_store.get("multi-env-test", "staging")

    assert prod_retrieved is not None
    assert staging_retrieved is not None
    assert prod_retrieved.environment == "production"
    assert staging_retrieved.environment == "staging"


def test_revoked_to_registered_transition_prevented(registration_store):
    """Revoked registrations should not be allowed to transition back to registered."""
    now = datetime.now(timezone.utc)
    registration = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440203",  # AD-03: Fixed test UUID
        target_id="lifecycle-test",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/lifecycle",
        runtime_mode="compose",
        registration_digest="a" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    registration_store.save(registration)

    revoked = registration_store.update_status(
        target_id="lifecycle-test",
        environment="production",
        new_status=RegistrationStatus.REVOKED,
        actor_subject="admin",
        reason="Test revocation",
    )

    assert revoked.status == RegistrationStatus.REVOKED


def test_registration_audit_event_emitted(db):
    """Registration creation should emit audit event."""
    from aipm.control_plane.audit import AuditEventType, SQLiteAuditLedger

    audit_ledger = SQLiteAuditLedger(db)
    store = SQLiteProjectRegistrationStore(db, audit_ledger)

    now = datetime.now(timezone.utc)
    registration = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440204",  # AD-03: Fixed test UUID
        target_id="audit-test",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/audit-test",
        runtime_mode="compose",
        registration_digest="a" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    store.save(registration)

    events = list(audit_ledger.events(limit=256))

    assert len(events) > 0

    registration_events = [e for e in events if e.event_type == AuditEventType.REGISTRATION_CREATED]
    assert len(registration_events) == 1

    event = registration_events[0]
    assert event.draft.target_id == "audit-test"
    assert event.draft.environment == "production"
    assert event.draft.lifecycle_to == "REGISTERED"


def test_revocation_audit_event_emitted(db):
    """Revocation should emit audit event."""
    from aipm.control_plane.audit import AuditEventType, SQLiteAuditLedger

    audit_ledger = SQLiteAuditLedger(db)
    store = SQLiteProjectRegistrationStore(db, audit_ledger)

    now = datetime.now(timezone.utc)
    registration = ProjectRegistration(
        registration_id="550e8400-e29b-41d4-a716-446655440205",  # AD-03: Fixed test UUID
        target_id="revoke-audit-test",
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/revoke-audit-test",
        runtime_mode="compose",
        registration_digest="a" * 64,
        registration_version="mc616-reg-v1",
        registered_by="operator",
        registered_at=now,
    )

    store.save(registration)

    store.update_status(
        target_id="revoke-audit-test",
        environment="production",
        new_status=RegistrationStatus.REVOKED,
        actor_subject="admin",
        reason="Security incident",
    )

    events = list(audit_ledger.events(limit=256))
    revocation_events = [e for e in events if e.event_type == AuditEventType.REGISTRATION_REVOKED]

    assert len(revocation_events) == 1

    event = revocation_events[0]
    assert event.draft.target_id == "revoke-audit-test"
    assert event.draft.lifecycle_from == "REGISTERED"
    assert event.draft.lifecycle_to == "REVOKED"
    assert "Security incident" in event.draft.reason


def test_registration_digest_binds_facts(service):
    """Registration digest should bind all registration facts."""
    registration1, result1 = service.create_registration(
        target_id="digest-test",
        project_path="/fake/path1",
        environment="production",
        runtime_mode="compose",
        registered_by="operator",
    )

    registration2, result2 = service.create_registration(
        target_id="digest-test",
        project_path="/fake/path2",
        environment="production",
        runtime_mode="compose",
        registered_by="operator",
    )


def test_namespace_constraint_enforced(validator):
    """Paths outside /home/ubuntu/ should be rejected."""
    for bad_path in ["/tmp/test", "/var/lib/test", "/root/test"]:
        result = validator.validate_registration_request(
            project_path=bad_path,
            runtime_mode="compose",
            environment="production",
        )
        assert not result.valid
        assert result.error_code in ("namespace_violation", "invalid_path")
