from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol, Sequence

from aipm.models.systemd import SystemdUnitRegistryEntry, SystemdUnitSnapshot, status_from_states


class SystemdProviderError(RuntimeError):
    """Internal adapter failure; never serialized directly."""


class SystemdProvider(Protocol):
    def observe(self, entry: SystemdUnitRegistryEntry) -> SystemdUnitSnapshot:
        ...


@dataclass(frozen=True, slots=True)
class SystemdCommandResult:
    stdout: str
    returncode: int


class LocalSystemdProvider:
    """Bounded read-only adapter for backend-owned unit registry entries."""

    max_output_bytes = 32_768
    timeout_seconds = 2.0

    def __init__(self, runner=None, *, manager_commands: dict[str, str] | None = None) -> None:
        self._runner = runner or subprocess.run
        self._manager_commands = manager_commands or {"user": "systemctl", "system": "systemctl"}

    def observe(self, entry: SystemdUnitRegistryEntry) -> SystemdUnitSnapshot:
        executable = self._manager_commands.get(entry.manager_scope)
        if not executable or executable != "systemctl":
            raise SystemdProviderError("unsupported systemd manager")
        command = self._command(entry)
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SystemdProviderError("systemd manager unavailable") from exc
        stdout = completed.stdout or ""
        if len(stdout.encode("utf-8", errors="replace")) > self.max_output_bytes:
            raise SystemdProviderError("systemd response exceeded bounds")
        if completed.returncode != 0:
            raise SystemdProviderError("systemd unit observation failed")
        fields = self._parse(stdout)
        required = ("LoadState", "ActiveState", "SubState")
        if any(not fields.get(key) for key in required):
            raise SystemdProviderError("systemd response malformed")
        enabled = self._parse_enabled(fields.get("UnitFileState"))
        active = fields["ActiveState"]
        sub = fields["SubState"]
        return SystemdUnitSnapshot(
            id=entry.id,
            display_name=entry.display_name,
            load_state=fields["LoadState"],
            active_state=active,
            sub_state=sub,
            enabled=enabled,
            status=status_from_states(active, sub),
            evidence=(),
        )

    @staticmethod
    def _command(entry: SystemdUnitRegistryEntry) -> tuple[str, ...]:
        prefix = ("systemctl", "--user") if entry.manager_scope == "user" else ("systemctl",)
        return prefix + ("show", entry.unit_name, "--no-pager", "--property=LoadState,ActiveState,SubState,UnitFileState")

    @staticmethod
    def _parse(output: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for line in output.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in {"LoadState", "ActiveState", "SubState", "UnitFileState"}:
                fields[key] = value[:256]
        return fields

    @staticmethod
    def _parse_enabled(value: str | None) -> bool | None:
        if value in {"enabled", "enabled-runtime", "static", "alias"}:
            return True
        if value in {"disabled", "masked", "indirect", "generated", "transient"}:
            return False
        return None
