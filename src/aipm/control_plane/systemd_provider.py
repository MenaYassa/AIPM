"""Typed systemd restart capability for the control plane.

This module implements the first real external capability:
``RESTART_ALLOWLISTED_SYSTEMD_UNIT``.

The provider executes exactly one operation —
``systemctl restart <trusted canonical unit>`` — via structured argv,
never shell mode, never caller-supplied arguments. The unit must resolve
through a typed allow-list before the command is constructed.

The capability is currently enabled for staging only, and requires
privilege escalation (systemd policy) that the current operator identity
does not possess. The provider is fully implemented and tested with a
fake/mocked subprocess adapter; real execution requires a separate
privilege grant.
"""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Sequence

from aipm.control_plane.audit.sanitize import AuditEventError, bounded_reference

SYSTEMD_CAPABILITY_VERSION = "1"
SNAPSHOT_VERSION = "mc612-systemd-snapshot-v1"
EXECUTOR_VERSION = "mc612-systemd-executor-v1"
_SYSTEMCTL_PATH = "/usr/bin/systemctl"
_MAX_OUTPUT_BYTES = 4096
_SUBPROCESS_TIMEOUT_SECONDS = 30


class SystemdRestartError(ValueError):
    """Raised when the systemd restart capability cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class SystemdRestartPolicy:
    """Typed allow-list entry; exactly one unit, no wildcards, no globs."""

    environment: str
    target_id: str
    unit_id: str
    canonical_unit_name: str
    policy_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", bounded_reference(self.environment, field="environment", maximum=32))
        object.__setattr__(self, "target_id", bounded_reference(self.target_id, field="target id"))
        object.__setattr__(self, "unit_id", bounded_reference(self.unit_id, field="unit id"))
        object.__setattr__(self, "canonical_unit_name", bounded_reference(self.canonical_unit_name, field="canonical unit name"))
        if not self.canonical_unit_name.endswith(".service"):
            raise SystemdRestartError("Canonical unit name must end in .service")
        if "/" in self.canonical_unit_name or "\\" in self.canonical_unit_name:
            raise SystemdRestartError("Canonical unit name must not contain path separators")
        object.__setattr__(self, "policy_version", bounded_reference(self.policy_version, field="policy version", maximum=64))


@dataclass(frozen=True, slots=True)
class SystemdUnitSnapshot:
    """Bounded pre-restart observation; digest-protected."""

    target_id: str
    unit_id: str
    canonical_unit_name: str
    load_state: str
    active_state: str
    sub_state: str
    enabled_state: str
    main_pid: str
    fragment_path: str
    captured_at: str
    snapshot_version: str = SNAPSHOT_VERSION

    def safe_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "unit_id": self.unit_id,
            "canonical_unit_name": self.canonical_unit_name,
            "load_state": self.load_state,
            "active_state": self.active_state,
            "sub_state": self.sub_state,
            "enabled_state": self.enabled_state,
            "main_pid": self.main_pid,
            "fragment_path": self.fragment_path,
            "captured_at": self.captured_at,
            "snapshot_version": self.snapshot_version,
        }


@dataclass(frozen=True, slots=True)
class SystemdRestartResult:
    """Bounded provider result; no raw stdout/stderr exposed."""

    action_id: str
    capability: str
    capability_version: str
    outcome: str  # succeeded / failed / unknown_outcome / refused
    started_at: str
    completed_at: str
    provider_code: str
    evidence_reference: str
    executor_version: str = EXECUTOR_VERSION

    def safe_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "capability": self.capability,
            "capability_version": self.capability_version,
            "outcome": self.outcome,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "provider_code": self.provider_code,
            "evidence_reference": self.evidence_reference,
            "executor_version": self.executor_version,
        }


@dataclass(frozen=True, slots=True)
class SubprocessResult:
    """Bounded subprocess output; never arbitrary stdout/stderr."""

    returncode: int
    stdout_bounded: str
    stderr_bounded: str
    timed_out: bool


def _default_runner(argv: Sequence[str], *, timeout: int = _SUBPROCESS_TIMEOUT_SECONDS) -> SubprocessResult:
    """Bounded subprocess adapter; the ONLY place subprocess is called."""
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
            env={"PATH": "/usr/bin:/usr/sbin:/bin:/sbin", "LC_ALL": "C"},
        )
        return SubprocessResult(
            returncode=proc.returncode,
            stdout_bounded=(proc.stdout or "")[:_MAX_OUTPUT_BYTES],
            stderr_bounded=(proc.stderr or "")[:_MAX_OUTPUT_BYTES],
            timed_out=False,
        )
    except subprocess.TimeoutExpired:
        return SubprocessResult(returncode=-1, stdout_bounded="", stderr_bounded="timeout", timed_out=True)
    except OSError as exc:
        return SubprocessResult(returncode=-1, stdout_bounded="", stderr_bounded=str(exc)[:256], timed_out=False)


class SystemdRestartProvider:
    """Executes exactly `systemctl restart <trusted canonical unit>` via argv."""

    __slots__ = ("_policies", "_runner", "_systemctl_path", "_initialized")

    def __init__(self, *, policies: Sequence[SystemdRestartPolicy], runner: Callable[..., SubprocessResult] | None = None, systemctl_path: str = _SYSTEMCTL_PATH) -> None:
        if not policies:
            raise SystemdRestartError("At least one restart policy is required")
        by_unit_id = {}
        for policy in policies:
            if not isinstance(policy, SystemdRestartPolicy):
                raise SystemdRestartError("Invalid policy entry")
            if policy.unit_id in by_unit_id:
                raise SystemdRestartError("Duplicate unit_id in allow-list")
            by_unit_id[policy.unit_id] = policy
        object.__setattr__(self, "_policies", by_unit_id)
        object.__setattr__(self, "_runner", runner or _default_runner)
        object.__setattr__(self, "_systemctl_path", systemctl_path)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("SystemdRestartProvider configuration is immutable")
        object.__setattr__(self, name, value)

    def resolve_unit(self, unit_id: str, *, environment: str) -> SystemdRestartPolicy:
        """Resolve unit_id → trusted canonical unit; fail closed on unknown."""

        if not isinstance(unit_id, str) or not unit_id:
            raise SystemdRestartError("Invalid unit id")
        policy = self._policies.get(unit_id)
        if policy is None:
            raise SystemdRestartError("Unit is not allow-listed")
        if policy.environment != environment:
            raise SystemdRestartError("Unit is not allow-listed for this environment")
        return policy

    def observe_unit(self, policy: SystemdRestartPolicy, *, now: datetime | None = None) -> SystemdUnitSnapshot:
        """Read-only observation of the unit's current state."""

        moment = (now or datetime.now(timezone.utc)).isoformat()
        argv = [
            self._systemctl_path, "show",
            policy.canonical_unit_name,
            "--property=LoadState,ActiveState,SubState,UnitFileState,MainPID,FragmentPath",
            "--no-pager",
        ]
        result = self._runner(argv)
        if result.returncode != 0:
            raise SystemdRestartError(f"Unit observation failed: {result.stderr_bounded[:128]}")
        properties = {}
        for line in result.stdout_bounded.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                properties[key.strip()] = value.strip()
        return SystemdUnitSnapshot(
            target_id=policy.target_id,
            unit_id=policy.unit_id,
            canonical_unit_name=policy.canonical_unit_name,
            load_state=properties.get("LoadState", "unknown"),
            active_state=properties.get("ActiveState", "unknown"),
            sub_state=properties.get("SubState", "unknown"),
            enabled_state=properties.get("UnitFileState", "unknown"),
            main_pid=properties.get("MainPID", "0"),
            fragment_path=properties.get("FragmentPath", ""),
            captured_at=moment,
        )

    def restart(self, policy: SystemdRestartPolicy, *, now: datetime | None = None) -> SubprocessResult:
        """Execute `systemctl restart <canonical_unit>` via structured argv."""

        argv = [self._systemctl_path, "restart", policy.canonical_unit_name]
        return self._runner(argv)
