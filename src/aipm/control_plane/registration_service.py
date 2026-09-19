"""Registration validation and service layer for production registration.

This module provides the canonical validation and service layer for production
project registration. It encapsulates all security-sensitive validation logic
including path canonicalization, ownership checks, discovery verification,
and provenance validation.

The CLI and any future registration entry points delegate to this service
rather than duplicating validation logic.

Compose identity resolution is injected at composition time to maintain
architectural boundaries (control_plane does not import providers).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from aipm.control_plane.registration import (
    ProjectRegistration,
    RegistrationStatus,
    compute_file_hash,
    compute_registration_digest,
)
from aipm.models.project import Project, ProjectCapabilities


@dataclass
class RegistrationValidationResult:
    """Result of registration validation."""

    valid: bool
    canonical_path: Path | None = None
    compose_project_name: str | None = None
    compose_file_hashes: list[str] | None = None
    error_code: str | None = None
    error_message: str | None = None


class RegistrationValidator:
    """Canonical validator for production project registration.

    Enforces all security boundaries:
    - Path canonicalization and traversal safety
    - Namespace constraints
    - Ownership validation
    - Permission validation
    - Provenance verification (Compose identity via injected dependency)
    - Runtime mode constraints

    Compose identity resolution is injected at composition time to maintain
    architectural separation (control_plane does not import providers).
    """

    ALLOWED_NAMESPACE = "/home/ubuntu/"
    SUPPORTED_RUNTIME_MODES = ("compose",)

    def __init__(self, compose_identity_resolver: Callable[[Project], str | None] | None = None):
        """Initialize validator with optional injected Compose identity resolver.

        Args:
            compose_identity_resolver: Optional callable that takes a Project and returns
                                      the resolved Compose project name or None.
                                      If not provided, Compose validation will fail.
        """
        self.compose_identity_resolver = compose_identity_resolver

    def validate_registration_request(
        self,
        project_path: str,
        runtime_mode: str,
        environment: str,
    ) -> RegistrationValidationResult:
        """Validate a registration request.

        Returns RegistrationValidationResult with validation outcome.
        """

        if environment not in ("staging", "production"):
            return RegistrationValidationResult(
                valid=False,
                error_code="invalid_environment",
                error_message=f"Invalid environment: {environment}. Must be 'staging' or 'production'.",
            )

        if runtime_mode not in self.SUPPORTED_RUNTIME_MODES:
            return RegistrationValidationResult(
                valid=False,
                error_code="unsupported_runtime",
                error_message=f"Unsupported runtime mode: {runtime_mode}. Currently only 'compose' is supported.",
            )

        try:
            canonical_path = Path(project_path).resolve(strict=True)
        except (OSError, ValueError) as exc:
            return RegistrationValidationResult(
                valid=False,
                error_code="invalid_path",
                error_message=f"Invalid or non-existent project path: {exc}",
            )

        if not canonical_path.is_dir():
            return RegistrationValidationResult(
                valid=False,
                error_code="not_directory",
                error_message=f"Project path is not a directory: {canonical_path}",
            )

        if not str(canonical_path).startswith(self.ALLOWED_NAMESPACE):
            return RegistrationValidationResult(
                valid=False,
                error_code="namespace_violation",
                error_message=f"Project path must be under {self.ALLOWED_NAMESPACE}: {canonical_path}",
            )

        ownership_result = self._validate_ownership(canonical_path)
        if not ownership_result.valid:
            return ownership_result

        permission_result = self._validate_permissions(canonical_path)
        if not permission_result.valid:
            return permission_result

        if runtime_mode == "compose":
            return self._validate_compose_registration(canonical_path)

        return RegistrationValidationResult(
            valid=False,
            error_code="unsupported_runtime",
            error_message=f"Runtime mode validation not implemented: {runtime_mode}",
        )

    def _validate_ownership(self, path: Path) -> RegistrationValidationResult:
        """Validate that the path is owned by a safe user (not root, not world-writable owner)."""
        try:
            stat_info = path.stat()
            uid = stat_info.st_uid

            if uid == 0:
                return RegistrationValidationResult(
                    valid=False,
                    error_code="root_owned",
                    error_message=f"Project path is owned by root (uid=0): {path}",
                )

            current_uid = os.getuid()
            if uid != current_uid and current_uid != 0:
                return RegistrationValidationResult(
                    valid=False,
                    error_code="ownership_mismatch",
                    error_message=f"Project path is not owned by the current user (uid={current_uid}, path uid={uid}): {path}",
                )

            return RegistrationValidationResult(valid=True)

        except (OSError, ValueError) as exc:
            return RegistrationValidationResult(
                valid=False,
                error_code="ownership_check_failed",
                error_message=f"Unable to verify ownership: {exc}",
            )

    def _validate_permissions(self, path: Path) -> RegistrationValidationResult:
        """Validate that the path does not have dangerous permissions."""
        try:
            stat_info = path.stat()
            mode = stat_info.st_mode

            if mode & 0o002:
                return RegistrationValidationResult(
                    valid=False,
                    error_code="world_writable",
                    error_message=f"Project path is world-writable: {path}",
                )

            if mode & 0o020:
                gid = stat_info.st_gid
                current_gid = os.getgid()
                if gid != current_gid:
                    return RegistrationValidationResult(
                        valid=False,
                        error_code="group_writable_unsafe",
                        error_message=f"Project path is group-writable by a different group: {path}",
                    )

            return RegistrationValidationResult(valid=True)

        except (OSError, ValueError) as exc:
            return RegistrationValidationResult(
                valid=False,
                error_code="permission_check_failed",
                error_message=f"Unable to verify permissions: {exc}",
            )

    def _validate_compose_registration(self, canonical_path: Path) -> RegistrationValidationResult:
        """Validate Compose-specific registration requirements.

        Requires an injected Compose identity resolver to be available.
        """
        compose_files = list(canonical_path.glob("docker-compose*.y*ml"))
        if not compose_files:
            return RegistrationValidationResult(
                valid=False,
                error_code="no_compose_files",
                error_message=f"No docker-compose files found in {canonical_path}",
            )

        if not self.compose_identity_resolver:
            return RegistrationValidationResult(
                valid=False,
                error_code="compose_resolver_unavailable",
                error_message="Compose identity resolver not available (dependency injection not configured)",
            )

        project = Project(
            name=canonical_path.name,
            path=str(canonical_path),
            capabilities=ProjectCapabilities(has_compose=True),
            compose_files=[str(f) for f in compose_files],
        )

        compose_project_name = self.compose_identity_resolver(project)
        if not compose_project_name:
            return RegistrationValidationResult(
                valid=False,
                error_code="invalid_compose_identity",
                error_message=f"Unable to resolve Compose project name from {canonical_path}",
            )

        compose_file_hashes = []
        for compose_file in compose_files:
            try:
                compose_file_hashes.append(compute_file_hash(str(compose_file)))
            except Exception as exc:
                return RegistrationValidationResult(
                    valid=False,
                    error_code="compose_file_hash_failed",
                    error_message=f"Unable to hash {compose_file}: {exc}",
                )

        return RegistrationValidationResult(
            valid=True,
            canonical_path=canonical_path,
            compose_project_name=compose_project_name,
            compose_file_hashes=compose_file_hashes,
        )


class RegistrationService:
    """High-level service for production registration operations.

    Coordinates validation, digest computation, and persistence.
    """

    def __init__(self, validator: RegistrationValidator | None = None):
        self.validator = validator or RegistrationValidator()

    def create_registration(
        self,
        target_id: str,
        project_path: str,
        environment: str,
        runtime_mode: str,
        registered_by: str,
    ) -> tuple[ProjectRegistration | None, RegistrationValidationResult]:
        """Create and validate a registration.

        Returns (registration, validation_result).
        If validation fails, registration is None.
        """

        validation_result = self.validator.validate_registration_request(
            project_path=project_path,
            runtime_mode=runtime_mode,
            environment=environment,
        )

        if not validation_result.valid:
            return None, validation_result

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        registration_digest = compute_registration_digest(
            target_id=target_id,
            canonical_project_path=str(validation_result.canonical_path),
            runtime_mode=runtime_mode,
            environment=environment,
            compose_project_name=validation_result.compose_project_name,
            compose_file_hashes=validation_result.compose_file_hashes,
            registered_at_iso=now_iso,
        )

        registration = ProjectRegistration(
            target_id=target_id,
            environment=environment,
            status=RegistrationStatus.REGISTERED,
            canonical_project_path=str(validation_result.canonical_path),
            runtime_mode=runtime_mode,
            compose_project_name=validation_result.compose_project_name,
            registration_digest=registration_digest,
            registration_version="mc616-reg-v1",
            registered_by=registered_by,
            registered_at=now,
        )

        return registration, validation_result
