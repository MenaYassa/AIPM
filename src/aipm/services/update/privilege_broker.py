from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

_UNIT_RE = re.compile(r"^[A-Za-z0-9_.:-]+\.service$")
_ALLOWED_VERBS = frozenset({"try-restart", "restart"})
DEFAULT_BROKER_PATH = "/usr/local/libexec/aipm/aipm-systemd-restart"


class PrivilegeBrokerError(RuntimeError):
    """Raised when privilege broker execution fails or invariants are violated."""


@dataclass(frozen=True, slots=True)
class PrivilegeBrokerResult:
    success: bool
    returncode: int
    stdout: str
    stderr: str
    error: str | None = None


class PrivilegeBrokerClient:
    """Unprivileged executor-side client for the bounded systemd privilege broker.

    The client construct strictly validated argv parameters to pass to the
    narrowly-scoped broker binary. It possesses no inherent privileges itself.
    """

    def __init__(
        self,
        broker_path: str = DEFAULT_BROKER_PATH,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        self.broker_path = broker_path
        self._runner = runner or subprocess.run

    def restart_unit(self, unit_name: str, *, verb: str = "try-restart") -> PrivilegeBrokerResult:
        """Request restart of an approved systemd unit via the bounded privilege broker."""
        unit_name = str(unit_name or "").strip()
        if not _UNIT_RE.fullmatch(unit_name):
            raise PrivilegeBrokerError(f"Invalid unit name for privilege broker: {unit_name!r}")
        if "@" in unit_name or "/" in unit_name or "\\" in unit_name:
            raise PrivilegeBrokerError(f"Prohibited unit character in: {unit_name!r}")
        if verb not in _ALLOWED_VERBS:
            raise PrivilegeBrokerError(f"Prohibited verb for privilege broker: {verb!r}")

        argv = [self.broker_path, f"--unit={unit_name}", f"--verb={verb}"]
        try:
            completed = self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=30.0,
                check=False,
                shell=False,
            )
            return PrivilegeBrokerResult(
                success=(completed.returncode == 0),
                returncode=completed.returncode,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
                error=None if completed.returncode == 0 else f"Broker exited with code {completed.returncode}: {completed.stderr or completed.stdout}",
            )
        except FileNotFoundError as exc:
            return PrivilegeBrokerResult(
                success=False,
                returncode=127,
                stdout="",
                stderr=str(exc),
                error=f"Privilege broker executable not found: {self.broker_path}",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return PrivilegeBrokerResult(
                success=False,
                returncode=126,
                stdout="",
                stderr=str(exc),
                error=f"Privilege broker execution failed: {exc}",
            )
