from __future__ import annotations

import json
import re
import subprocess
from typing import Any

import docker

from aipm.core.exceptions import ContainerNotFound, DockerError


class DockerProvider:
    def __init__(self, client: Any | None = None):
        if client is not None:
            self.client = client
            return
        try:
            self.client = docker.from_env()
        except docker.errors.DockerException as exc:
            raise DockerError(f"Docker is unavailable: {exc}") from exc

    def _container(self, name: str):
        try:
            return self.client.containers.get(name)
        except docker.errors.NotFound as exc:
            raise ContainerNotFound(f"Container '{name}' not found.") from exc
        except docker.errors.DockerException as exc:
            raise DockerError(f"Unable to access Docker container '{name}': {exc}") from exc

    def list_containers(self):
        try:
            return self.client.containers.list(all=True)
        except docker.errors.DockerException as exc:
            raise DockerError(f"Docker is unavailable: {exc}") from exc

    def inspect(self, name: str):
        return self._container(name)

    def stats(self, container):
        """Return a one-shot read-only stats payload for a container object."""
        try:
            return container.stats(stream=False)
        except docker.errors.DockerException as exc:
            raise DockerError("Docker stats are unavailable.") from exc

    def stats_all(self, *, timeout_seconds: int = 15) -> dict[str, dict[str, float | None]]:
        """Return one aggregate read-only resource snapshot for running containers."""
        try:
            completed = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DockerError("Aggregate Docker resource telemetry is unavailable.") from exc
        result: dict[str, dict[str, float | None]] = {}
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                name = str(row.get("Name") or row.get("ID") or "")
                if name:
                    used, limit = _memory_pair(str(row.get("MemUsage") or ""))
                    result[name] = {
                        "cpu_percent": _percent(row.get("CPUPerc")),
                        "memory_used_mb": used,
                        "memory_limit_mb": limit,
                        "memory_percent": _percent(row.get("MemPerc")),
                    }
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return result

    def start(self, name: str) -> None:
        try:
            self._container(name).start()
        except docker.errors.DockerException as exc:
            raise DockerError(f"Unable to start container '{name}': {exc}") from exc

    def stop(self, name: str) -> None:
        try:
            self._container(name).stop()
        except docker.errors.DockerException as exc:
            raise DockerError(f"Unable to stop container '{name}': {exc}") from exc

    def restart(self, name: str) -> None:
        try:
            self._container(name).restart()
        except docker.errors.DockerException as exc:
            raise DockerError(f"Unable to restart container '{name}': {exc}") from exc

    def images(self):
        try:
            return self.client.images.list()
        except docker.errors.DockerException as exc:
            raise DockerError(f"Docker error while fetching images: {exc}") from exc

    def volumes(self):
        try:
            return self.client.volumes.list()
        except docker.errors.DockerException as exc:
            raise DockerError(f"Docker error while fetching volumes: {exc}") from exc

    def networks(self):
        try:
            return self.client.networks.list()
        except docker.errors.DockerException as exc:
            raise DockerError(f"Docker error while fetching networks: {exc}") from exc

    def logs(self, name: str, tail: int = 100) -> str:
        if tail < 0:
            raise DockerError("Log tail must be zero or greater.")
        try:
            return self._container(name).logs(tail=tail).decode("utf-8", errors="replace")
        except docker.errors.DockerException as exc:
            raise DockerError(f"Docker error while fetching logs: {exc}") from exc


def _percent(value: object) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return round(float(match.group(0)), 1) if match else None


def _memory_bytes(value: str) -> float | None:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([kmgtpe]?i?b)?", value.strip(), re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    factors = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000**2, "mib": 1024**2, "gb": 1000**3, "gib": 1024**3, "tb": 1000**4, "tib": 1024**4}
    return number * factors.get(unit, 1)


def _memory_pair(value: str) -> tuple[float | None, float | None]:
    parts = [part.strip() for part in value.split("/")]
    if len(parts) != 2:
        return None, None
    used = _memory_bytes(parts[0])
    limit = _memory_bytes(parts[1])
    return (round(used / (1024**2), 1) if used is not None else None, round(limit / (1024**2), 1) if limit is not None else None)
