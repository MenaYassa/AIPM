"""Compose execution adapter for bounded service updates (MC-6.15-C.3).

Translates already-authorized UpdateExecutionBinding and canonical service_scope
into bounded, non-shell Docker Compose mutation operations and typed post-mutation
verification hooks.

Security Invariants:
- Never uses shell=True, bash -c, sh -c, or string commands.
- Never accepts arbitrary Compose file paths, image tags, or registries from callers.
- Project and Compose file resolution is strictly server-side and path-traversal safe.
- service_scope is opaque authorized execution authority; no scope expansion or narrowing.
- Command exit code 0 is NOT verified success; independent verification is mandatory.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from aipm.core.exceptions import ProviderError

_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_FORBIDDEN_CHARS = frozenset(";&|$`><\n\r\t\0'\"/\\:@ ")


class ServiceUpdateVerificationCode(str, Enum):
    """Typed verification outcome codes for service mutations (MC-6.15-C.3)."""

    VERIFIED_SUCCESS = "verified_success"
    APPLICATION_PROBE_FAILURE = "application_probe_failure"
    RUNTIME_DIGEST_MISMATCH = "runtime_digest_mismatch"
    DEPENDENCY_HEALTH_FAILURE = "dependency_health_failure"
    SUPERVISOR_FAILURE = "supervisor_failure"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class ServiceUpdateVerificationResult:
    """Independent post-mutation inspection verdict."""

    code: ServiceUpdateVerificationCode
    verified_services: tuple[str, ...]
    details: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        return self.code is ServiceUpdateVerificationCode.VERIFIED_SUCCESS


@dataclass(frozen=True, slots=True)
class ComposeExecutionResult:
    """Complete outcome of the Compose execution adapter."""

    verification: ServiceUpdateVerificationResult
    executed_commands: tuple[tuple[str, ...], ...] = ()
    mutation_occurred: bool = False
    details: str = ""

    @property
    def is_success(self) -> bool:
        return self.verification.is_success


class ComposeExecutionError(ProviderError):
    """Raised when an execution precondition fails or authorization is violated."""

    def __init__(self, message: str, *, code: str = "execution_error"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BoundedServiceUpdateIntent:
    """Typed authorization input passed to the Compose execution adapter."""

    project_name: str
    service_scope: tuple[str, ...]
    plan_digest: str
    confirmation_id: str
    action_id: str
    fencing_token: int
    contract_digest: str
    lease_id: str
    expected_target_digest: str | None = None
    expected_child_digest: str | None = None
    atomicity: str = "leaf_independent"
    now: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.project_name, str) or not self.project_name or not _SAFE_ID_RE.match(self.project_name):
            raise ComposeExecutionError("Invalid project_name", code="invalid_project_name")
        if not isinstance(self.service_scope, tuple) or not self.service_scope:
            raise ComposeExecutionError("service_scope must be a non-empty tuple", code="invalid_service_scope")
        seen: set[str] = set()
        for svc in self.service_scope:
            if not isinstance(svc, str) or not svc or len(svc) > 64:
                raise ComposeExecutionError(f"Invalid service name: {svc!r}", code="invalid_service_scope")
            if svc != svc.strip() or any(c in _FORBIDDEN_CHARS for c in svc) or any(c.isspace() for c in svc):
                raise ComposeExecutionError(f"Forbidden characters in service name: {svc!r}", code="invalid_service_scope")
            if svc.startswith("-"):
                raise ComposeExecutionError(f"Service name cannot start with dash: {svc!r}", code="invalid_service_scope")
            if not _SAFE_ID_RE.match(svc):
                raise ComposeExecutionError(f"Service name does not match safe pattern: {svc!r}", code="invalid_service_scope")
            if svc in seen:
                raise ComposeExecutionError(f"Duplicate service name in service_scope: {svc!r}", code="invalid_service_scope")
            seen.add(svc)

        _hex64 = set("0123456789abcdef")
        _hex32 = set("0123456789abcdef")
        if not isinstance(self.plan_digest, str) or len(self.plan_digest) != 64 or not set(self.plan_digest) <= _hex64:
            raise ComposeExecutionError("Invalid plan_digest", code="invalid_plan_digest")
        if not isinstance(self.confirmation_id, str) or len(self.confirmation_id) != 32 or not set(self.confirmation_id) <= _hex32:
            raise ComposeExecutionError("Invalid confirmation_id", code="invalid_confirmation_id")
        if not isinstance(self.contract_digest, str) or len(self.contract_digest) != 64 or not set(self.contract_digest) <= _hex64:
            raise ComposeExecutionError("Invalid contract_digest", code="invalid_contract_digest")
        if not isinstance(self.lease_id, str) or not self.lease_id:
            raise ComposeExecutionError("Invalid lease_id", code="invalid_lease_id")
        if not isinstance(self.fencing_token, int) or isinstance(self.fencing_token, bool) or self.fencing_token < 1:
            raise ComposeExecutionError("Invalid fencing_token", code="invalid_fencing_token")


class ComposeExecutionAdapter:
    """Bounded, non-shell execution adapter for Compose service mutations."""

    def __init__(
        self,
        *,
        project_resolver: Callable[[str], Any] | Mapping[str, Any] | None = None,
        runner: Callable = subprocess.run,
        inspector: Callable[[str, str], Any] | None = None,
        lease_validator: Callable[[str, int, Any], bool] | None = None,
    ) -> None:
        self._project_resolver = project_resolver
        self._runner = runner
        self._inspector = inspector
        self._lease_validator = lease_validator

    def execute(self, intent: BoundedServiceUpdateIntent) -> ComposeExecutionResult:
        """Execute the authorized update scope under strict bounds and verification."""
        # 1. Lease & Fencing Validation
        if self._lease_validator is not None:
            if not self._lease_validator(intent.action_id, intent.fencing_token, intent.now):
                raise ComposeExecutionError("Lease expired or fencing token mismatch", code="lease_expired")

        # 2. Server-side Compose Project & File Resolution
        project = self._resolve_project(intent.project_name)
        project_path, compose_files = self._validate_project_files(project)

        # 3. Service Scope Validation against Project Definitions
        self._validate_service_definitions(project, intent.service_scope)

        # 4. Pre-mutation Dependency & Health Readiness
        pre_check = self._check_pre_mutation_health(intent.project_name, intent.service_scope)
        if pre_check is not None:
            return ComposeExecutionResult(
                verification=pre_check,
                executed_commands=(),
                mutation_occurred=False,
                details=f"Pre-mutation check failed: {pre_check.details}",
            )

        # 5. Bounded Mutation Execution
        executed: list[tuple[str, ...]] = []
        mutation_started = False
        atomicity_tight = intent.atomicity == "atomic_tight" or len(intent.service_scope) > 1

        for service_name in intent.service_scope:
            # 5a. Pull step
            pull_cmd = self._build_pull_command(compose_files, service_name)
            executed.append(tuple(pull_cmd))
            pull_res = self._run_command(pull_cmd, cwd=project_path)
            if pull_res.returncode != 0:
                err_msg = (pull_res.stderr or pull_res.stdout or "pull failed").strip()
                code = (
                    ServiceUpdateVerificationCode.RECONCILIATION_REQUIRED
                    if mutation_started
                    else ServiceUpdateVerificationCode.SUPERVISOR_FAILURE
                )
                return ComposeExecutionResult(
                    verification=ServiceUpdateVerificationResult(
                        code=code,
                        verified_services=(),
                        details=f"Pull failed for {service_name}: {err_msg}",
                    ),
                    executed_commands=tuple(executed),
                    mutation_occurred=mutation_started,
                    details=f"Pull failed for {service_name}",
                )

            # 5b. Up step (--no-deps)
            up_cmd = self._build_up_command(compose_files, service_name)
            executed.append(tuple(up_cmd))
            mutation_started = True
            up_res = self._run_command(up_cmd, cwd=project_path)
            if up_res.returncode != 0:
                err_msg = (up_res.stderr or up_res.stdout or "up failed").strip()
                code = (
                    ServiceUpdateVerificationCode.RECONCILIATION_REQUIRED
                    if atomicity_tight
                    else ServiceUpdateVerificationCode.SUPERVISOR_FAILURE
                )
                return ComposeExecutionResult(
                    verification=ServiceUpdateVerificationResult(
                        code=code,
                        verified_services=(),
                        details=f"Compose up failed for {service_name}: {err_msg}",
                    ),
                    executed_commands=tuple(executed),
                    mutation_occurred=True,
                    details=f"Compose up failed for {service_name}",
                )

        # 6. Post-Mutation Independent Verification
        verification = self._verify_post_mutation(
            project_name=intent.project_name,
            service_scope=intent.service_scope,
            expected_target_digest=intent.expected_target_digest,
            expected_child_digest=intent.expected_child_digest,
            atomicity_tight=atomicity_tight,
        )

        return ComposeExecutionResult(
            verification=verification,
            executed_commands=tuple(executed),
            mutation_occurred=True,
            details="Execution completed with independent verification",
        )

    def _resolve_project(self, project_name: str) -> Any:
        if self._project_resolver is None:
            raise ComposeExecutionError("Project resolver is not configured", code="project_resolver_missing")
        if callable(self._project_resolver):
            project = self._project_resolver(project_name)
        elif isinstance(self._project_resolver, Mapping):
            project = self._project_resolver.get(project_name)
        else:
            raise ComposeExecutionError("Invalid project resolver contract", code="project_resolver_invalid")
        if project is None:
            raise ComposeExecutionError(f"Compose project not found: {project_name!r}", code="project_not_found")
        return project

    def _validate_project_files(self, project: Any) -> tuple[Path, tuple[Path, ...]]:
        raw_path = getattr(project, "path", None)
        if raw_path is None:
            raise ComposeExecutionError("Project missing path attribute", code="invalid_project_path")
        project_path = Path(raw_path).resolve()
        if not project_path.is_dir():
            raise ComposeExecutionError(f"Project path does not exist or is not a directory: {project_path}", code="invalid_project_path")

        raw_files = getattr(project, "compose_files", None)
        if not raw_files or not isinstance(raw_files, (list, tuple)):
            raise ComposeExecutionError("Project has no Compose files defined", code="missing_compose_files")

        validated_files: list[Path] = []
        for raw_file in raw_files:
            file_str = str(raw_file)
            if ".." in Path(file_str).parts:
                raise ComposeExecutionError(f"Path traversal detected in Compose file: {file_str}", code="path_traversal")
            f_path = Path(file_str)
            if not f_path.is_absolute():
                f_path = (project_path / f_path).resolve()
            else:
                f_path = f_path.resolve()
            if not f_path.is_file():
                raise ComposeExecutionError(f"Compose file does not exist: {f_path}", code="compose_file_not_found")
            validated_files.append(f_path)

        return project_path, tuple(validated_files)

    def _validate_service_definitions(self, project: Any, service_scope: tuple[str, ...]) -> None:
        known_services: set[str] | None = None
        local_build_services: set[str] = set()

        if hasattr(project, "services") and isinstance(project.services, (dict, set, list, tuple)):
            known_services = set(project.services.keys()) if isinstance(project.services, dict) else set(project.services)
        if hasattr(project, "local_build_services") and isinstance(project.local_build_services, (set, list, tuple)):
            local_build_services = set(project.local_build_services)

        for svc in service_scope:
            if known_services is not None and svc not in known_services:
                raise ComposeExecutionError(f"Service {svc!r} is not defined in Compose project", code="unrelated_service")
            if svc in local_build_services:
                raise ComposeExecutionError(f"Service {svc!r} is a local build and cannot enter registry mutation", code="local_build_forbidden")

    def _check_pre_mutation_health(self, project_name: str, service_scope: tuple[str, ...]) -> ServiceUpdateVerificationResult | None:
        if self._inspector is None:
            return None
        for svc in service_scope:
            try:
                obs = self._inspector(project_name, svc)
            except Exception as exc:
                return ServiceUpdateVerificationResult(
                    code=ServiceUpdateVerificationCode.SUPERVISOR_FAILURE,
                    verified_services=(),
                    details=f"Failed to inspect pre-mutation status of {svc}: {exc}",
                )
            if obs is None:
                continue
            health = getattr(obs, "health", None)
            state = getattr(obs, "state", "running")
            if state != "running":
                return ServiceUpdateVerificationResult(
                    code=ServiceUpdateVerificationCode.DEPENDENCY_HEALTH_FAILURE,
                    verified_services=(),
                    details=f"Service {svc} is not running prior to mutation (state={state})",
                )
            if health == "unhealthy":
                return ServiceUpdateVerificationResult(
                    code=ServiceUpdateVerificationCode.DEPENDENCY_HEALTH_FAILURE,
                    verified_services=(),
                    details=f"Service {svc} is unhealthy prior to mutation",
                )
        return None

    def _build_pull_command(self, compose_files: Sequence[Path], service_name: str) -> list[str]:
        cmd = ["docker", "compose"]
        for f in compose_files:
            cmd.extend(("-f", str(f)))
        cmd.extend(("pull", service_name))
        return cmd

    def _build_up_command(self, compose_files: Sequence[Path], service_name: str) -> list[str]:
        cmd = ["docker", "compose"]
        for f in compose_files:
            cmd.extend(("-f", str(f)))
        cmd.extend(("up", "-d", "--no-deps", service_name))
        return cmd

    def _run_command(self, cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr=f"Failed to execute command: {exc}",
            )

    def _verify_post_mutation(
        self,
        *,
        project_name: str,
        service_scope: tuple[str, ...],
        expected_target_digest: str | None,
        expected_child_digest: str | None,
        atomicity_tight: bool,
    ) -> ServiceUpdateVerificationResult:
        if self._inspector is None:
            # Command returning 0 is only intermediate; without an inspector,
            # we cannot assert VERIFIED_SUCCESS.
            return ServiceUpdateVerificationResult(
                code=ServiceUpdateVerificationCode.SUPERVISOR_FAILURE,
                verified_services=(),
                details="Post-mutation inspection hook is missing; command exit 0 is not verified success",
            )

        verified: list[str] = []
        for svc in service_scope:
            try:
                obs = self._inspector(project_name, svc)
            except Exception as exc:
                code = (
                    ServiceUpdateVerificationCode.RECONCILIATION_REQUIRED
                    if atomicity_tight and verified
                    else ServiceUpdateVerificationCode.SUPERVISOR_FAILURE
                )
                return ServiceUpdateVerificationResult(
                    code=code,
                    verified_services=tuple(verified),
                    details=f"Inspection exception for {svc}: {exc}",
                )

            if obs is None:
                code = (
                    ServiceUpdateVerificationCode.RECONCILIATION_REQUIRED
                    if atomicity_tight and verified
                    else ServiceUpdateVerificationCode.SUPERVISOR_FAILURE
                )
                return ServiceUpdateVerificationResult(
                    code=code,
                    verified_services=tuple(verified),
                    details=f"Service {svc} could not be inspected after mutation",
                )

            state = getattr(obs, "state", "")
            if state != "running":
                code = (
                    ServiceUpdateVerificationCode.RECONCILIATION_REQUIRED
                    if atomicity_tight and verified
                    else ServiceUpdateVerificationCode.SUPERVISOR_FAILURE
                )
                return ServiceUpdateVerificationResult(
                    code=code,
                    verified_services=tuple(verified),
                    details=f"Service {svc} is not running after mutation (state={state})",
                )

            health = getattr(obs, "health", None)
            if health == "unhealthy":
                code = (
                    ServiceUpdateVerificationCode.RECONCILIATION_REQUIRED
                    if atomicity_tight and verified
                    else ServiceUpdateVerificationCode.APPLICATION_PROBE_FAILURE
                )
                return ServiceUpdateVerificationResult(
                    code=code,
                    verified_services=tuple(verified),
                    details=f"Service {svc} health probe failed (health={health})",
                )

            running_digest = getattr(obs, "running_digest", None)
            if expected_target_digest is not None and running_digest is not None:
                def _norm(d: str) -> str:
                    return d.split("@")[1] if "@" in d else d

                norm_run = _norm(running_digest)
                norm_exp = _norm(expected_target_digest)
                norm_child = _norm(expected_child_digest) if expected_child_digest else None

                if norm_run != norm_exp and (norm_child is None or norm_run != norm_child):
                    code = (
                        ServiceUpdateVerificationCode.RECONCILIATION_REQUIRED
                        if atomicity_tight and verified
                        else ServiceUpdateVerificationCode.RUNTIME_DIGEST_MISMATCH
                    )
                    return ServiceUpdateVerificationResult(
                        code=code,
                        verified_services=tuple(verified),
                        details=f"Service {svc} running digest mismatch: {running_digest} != {expected_target_digest}",
                    )

            verified.append(svc)

        if atomicity_tight and len(verified) != len(service_scope):
            return ServiceUpdateVerificationResult(
                code=ServiceUpdateVerificationCode.RECONCILIATION_REQUIRED,
                verified_services=tuple(verified),
                details=f"Partial ATOMIC_TIGHT execution: verified {len(verified)}/{len(service_scope)}",
            )

        return ServiceUpdateVerificationResult(
            code=ServiceUpdateVerificationCode.VERIFIED_SUCCESS,
            verified_services=tuple(verified),
            details=f"Successfully verified {len(verified)} service(s): {', '.join(verified)}",
        )
