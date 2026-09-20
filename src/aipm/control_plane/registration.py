"""Project registration domain model and digest computation.

Production registration is an operator-initiated, auditable declaration that
a specific filesystem path contains a legitimate production application with
known Compose/systemd provenance.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class RegistrationStatus(Enum):
    REGISTERED = "REGISTERED"
    DISABLED = "DISABLED"
    REVOKED = "REVOKED"


class RegistrationError(Exception):
    pass


@dataclass(frozen=True)
class ProjectRegistration:
    registration_id: str
    target_id: str
    environment: str
    status: RegistrationStatus
    canonical_project_path: str
    runtime_mode: str
    registration_digest: str
    registration_version: str
    registered_by: str
    registered_at: datetime
    compose_project_name: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    revoked_by: str | None = None
    revoked_at: datetime | None = None
    revocation_reason: str | None = None
    updated_at: datetime | None = None

    def is_active(self) -> bool:
        return self.status == RegistrationStatus.REGISTERED

    def is_revoked(self) -> bool:
        return self.status == RegistrationStatus.REVOKED

    def is_disabled(self) -> bool:
        return self.status == RegistrationStatus.DISABLED


def compute_registration_digest(
    target_id: str,
    canonical_project_path: str,
    runtime_mode: str,
    environment: str,
    compose_project_name: str | None,
    compose_file_hashes: list[str] | None,
    registered_at_iso: str,
    registration_version: str = "mc616-reg-v1",
) -> str:
    """Compute the authoritative registration identity digest.

    The registration_digest is a VERSIONED REGISTRATION-STATE IDENTITY that
    cryptographically binds the validated registration facts at creation time.

    Semantics: VERSION-STAMPED IMMUTABLE IDENTITY

    The digest is NOT globally unique across re-registrations. If the same
    project is registered at different timestamps, each registration will have
    a different digest due to the timestamp binding.

    The digest provides:
    - Cryptographic binding of validated registration facts
    - Detection of registration fact tampering
    - Version-stamped registration identity
    - Audit trail correlation

    The authoritative current-registration lookup remains (target_id, environment).
    The digest is NOT used as a primary key.

    Args:
        target_id: Project target identifier (lookup key)
        canonical_project_path: Validated absolute canonical path
        runtime_mode: Runtime mode (compose, systemd)
        environment: Environment (production, staging)
        compose_project_name: Resolved Compose project name
        compose_file_hashes: SHA-256 hashes of compose files
        registered_at_iso: ISO 8601 registration timestamp
        registration_version: Registration schema version

    Returns:
        64-character lowercase hex SHA-256 digest
    """
    payload: dict[str, Any] = {
        "target_id": target_id,
        "canonical_project_path": canonical_project_path,
        "runtime_mode": runtime_mode,
        "environment": environment,
        "compose_project_name": compose_project_name,
        "compose_file_hashes": sorted(compose_file_hashes or []),
        "registered_at": registered_at_iso,
        "registration_version": registration_version,
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def compute_file_hash(file_path: str) -> str:
    hasher = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            while chunk := f.read(8192):
                hasher.update(chunk)
        return hasher.hexdigest()
    except (OSError, IOError) as exc:
        raise RegistrationError(f"Unable to hash file {file_path}: {exc}") from exc


def verify_registration_digest(
    registration: ProjectRegistration,
    compose_file_hashes: list[str] | None = None,
) -> bool:
    """Verify that a registration's digest is valid and matches its facts.

    Validates:
    1. Digest is structurally valid (64 lowercase hex characters).
    2. If compose_file_hashes are available (either on the object or passed in),
       recomputes the canonical digest using compute_registration_digest and
       verifies exact match.
    """
    digest = getattr(registration, "registration_digest", None)
    if not isinstance(digest, str) or len(digest) != 64:
        return False
    if not all(c in "0123456789abcdef" for c in digest):
        return False

    hashes = getattr(registration, "compose_file_hashes", None)
    if compose_file_hashes is not None:
        hashes = compose_file_hashes

    if hashes is not None:
        reg_at = getattr(registration, "registered_at", None)
        reg_at_iso = reg_at.isoformat() if hasattr(reg_at, "isoformat") else str(reg_at)
        expected = compute_registration_digest(
            target_id=registration.target_id,
            canonical_project_path=registration.canonical_project_path,
            runtime_mode=registration.runtime_mode,
            environment=registration.environment,
            compose_project_name=registration.compose_project_name,
            compose_file_hashes=hashes,
            registered_at_iso=reg_at_iso,
            registration_version=getattr(registration, "registration_version", "mc616-reg-v1"),
        )
        if digest != expected:
            return False

    return True
