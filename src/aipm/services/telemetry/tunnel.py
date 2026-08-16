from __future__ import annotations

import subprocess
from typing import Any, Callable

from aipm.models.telemetry import DockerSnapshot, TelemetryError, TunnelSnapshot


class TunnelTelemetryService:
    """Detect local cloudflared state without account-level Cloudflare access."""

    def __init__(
        self,
        *,
        command_runner: Callable[..., Any] | None = None,
        logger: Any | None = None,
    ) -> None:
        self.command_runner = command_runner or subprocess.run
        self.logger = logger

    def snapshot(self, docker: DockerSnapshot) -> TunnelSnapshot:
        local_containers = tuple(
            item.container.name
            for item in docker.containers
            if "cloudflared" in item.container.name.lower() or "cloudflare" in item.container.name.lower()
        )
        active_containers = tuple(
            item for item in docker.containers if item.container.name in local_containers and item.container.state == "running"
        )
        systemd = self._systemd_state()
        if active_containers:
            return TunnelSnapshot(state="healthy", source="docker", local_containers=local_containers, systemd=systemd)
        if systemd == "active":
            return TunnelSnapshot(state="healthy", source="systemd", local_containers=local_containers, systemd=systemd)
        if local_containers or systemd in {"inactive", "failed"}:
            return TunnelSnapshot(state="down", source="local-agent", local_containers=local_containers, systemd=systemd)
        return TunnelSnapshot(state="unknown", source="not-detected", local_containers=local_containers, systemd=None)

    def _systemd_state(self) -> str | None:
        try:
            result = self.command_runner(
                ["systemctl", "is-active", "cloudflared"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            value = (result.stdout or "").strip()
            if result.returncode != 0 and value not in {"inactive", "failed", "unknown"}:
                return None
            return value or "unknown"
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            if self.logger is not None:
                self.logger.exception("Cloudflared systemd detection unavailable", exc_info=exc)
            return None
