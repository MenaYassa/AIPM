from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+\.service$")
_MAX_OUTPUT_BYTES = 32_768
_TIMEOUT_SECONDS = 3.0


class SystemdTrustError(ValueError):
    """Raised when a systemd unit cannot be trusted or verified."""


@dataclass(frozen=True, slots=True)
class SystemdUnitObservation:
    unit_name: str
    load_state: str
    active_state: str
    sub_state: str
    unit_file_state: str
    working_directory: str | None
    exec_start: str | None
    user: str | None
    fragment_path: str | None
    primary_id: str | None
    invocation_id: str | None
    active_enter_timestamp: str | None
    restart_count: int


class SystemdObservationProvider:
    """Read-only systemd observation and trust validation provider.

    Verifies systemd unit identity, existence, provenance, and execution
    invariants without modifying any systemd state.
    """

    def __init__(self, runner: Callable[..., Any] | None = None) -> None:
        self._runner = runner or subprocess.run

    def observe_unit(self, unit_name: str) -> SystemdUnitObservation:
        """Query systemd for a unit and return parsed observation."""
        unit_name = str(unit_name or "").strip()
        if not _UNIT_NAME_RE.fullmatch(unit_name):
            raise SystemdTrustError(f"Invalid systemd unit name: {unit_name!r}")

        cmd = (
            "systemctl",
            "show",
            unit_name,
            "--no-pager",
            "--property=Id,LoadState,ActiveState,SubState,UnitFileState,WorkingDirectory,ExecStart,User,FragmentPath,InvocationID,ActiveEnterTimestampMonotonic,NRestarts",
        )
        try:
            completed = self._runner(
                cmd,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SystemdTrustError(f"systemd observation failed for {unit_name}: {exc}") from exc

        if completed.returncode != 0:
            raise SystemdTrustError(f"systemctl show exited with code {completed.returncode} for {unit_name}")

        stdout = completed.stdout or ""
        if len(stdout.encode("utf-8", errors="replace")) > _MAX_OUTPUT_BYTES:
            raise SystemdTrustError(f"systemd response for {unit_name} exceeded byte bound")

        fields = self._parse_properties(stdout)
        primary_id = fields.get("Id")
        if primary_id and primary_id != unit_name:
            raise SystemdTrustError(f"Unit {unit_name} alias/id mismatch: reports Id={primary_id}")

        n_restarts = 0
        raw_restarts = fields.get("NRestarts", "0")
        try:
            n_restarts = int(raw_restarts)
        except (ValueError, TypeError):
            pass

        return SystemdUnitObservation(
            unit_name=unit_name,
            load_state=fields.get("LoadState", "unknown"),
            active_state=fields.get("ActiveState", "unknown"),
            sub_state=fields.get("SubState", "unknown"),
            unit_file_state=fields.get("UnitFileState", "unknown"),
            working_directory=fields.get("WorkingDirectory") or None,
            exec_start=fields.get("ExecStart") or None,
            user=fields.get("User") or None,
            fragment_path=fields.get("FragmentPath") or None,
            primary_id=primary_id,
            invocation_id=fields.get("InvocationID") or None,
            active_enter_timestamp=fields.get("ActiveEnterTimestampMonotonic") or None,
            restart_count=n_restarts,
        )

    def validate_trust(self, unit_name: str, project_path: str | Path) -> tuple[bool, str | None, SystemdUnitObservation | None]:
        """Validate whether a systemd unit is trustworthy and owned by project_path.

        Invariants checked:
        - canonical unit name format
        - unit load_state == "loaded"
        - unit not masked
        - WorkingDirectory or ExecStart resolves within canonical project_path
        - no alias divergence
        """
        try:
            obs = self.observe_unit(unit_name)
        except SystemdTrustError as exc:
            return False, str(exc), None

        if obs.load_state != "loaded":
            return False, f"Unit {unit_name} is not loaded (LoadState={obs.load_state})", obs

        if obs.unit_file_state == "masked" or obs.load_state == "masked":
            return False, f"Unit {unit_name} is masked", obs

        canonical_project_dir = Path(project_path).resolve()
        path_proven = False

        if obs.working_directory:
            try:
                unit_work_dir = Path(obs.working_directory).resolve()
                if unit_work_dir == canonical_project_dir or canonical_project_dir in unit_work_dir.parents:
                    path_proven = True
            except (ValueError, OSError):
                pass

        if not path_proven and obs.exec_start:
            # Check if binary in ExecStart is inside project_path
            # ExecStart format often starts with: { path=/... ; argv[]=/... ; ... } or /path/to/bin
            for token in obs.exec_start.split():
                if token.startswith("path="):
                    token = token[5:].rstrip(";").strip()
                try:
                    p = Path(token).resolve()
                    if canonical_project_dir in p.parents or p == canonical_project_dir:
                        path_proven = True
                        break
                except (ValueError, OSError):
                    continue

        if not path_proven:
            return False, f"Unit {unit_name} provenance mismatch: WorkingDirectory and ExecStart are outside {canonical_project_dir}", obs

        return True, None, obs

    @staticmethod
    def _parse_properties(output: str) -> dict[str, str]:
        properties: dict[str, str] = {}
        for line in output.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                properties[key.strip()] = value.strip()
        return properties
