from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aipm.models.telemetry import DashboardSnapshot, HostSnapshot, TelemetryError, TelemetryFreshness


class DashboardResponseMapper:
    """Convert typed dashboard snapshots into the stable frontend response shape."""

    def to_response(self, snapshot: DashboardSnapshot) -> dict[str, Any]:
        return {
            "generated_at": snapshot.generated_at.astimezone(timezone.utc).isoformat(),
            "host": self._host(snapshot.host),
            "docker": self._docker(snapshot),
            "tunnel": self._tunnel(snapshot),
            "projects": self._projects(snapshot),
            "handbook": [
                {
                    "id": route.id,
                    "title": route.title,
                    "description": route.description,
                    "commands": list(route.commands),
                }
                for route in snapshot.handbook
            ],
        }

    def _host(self, host: HostSnapshot) -> dict[str, Any]:
        system = host.system
        cpu = system.cpu if system else None
        memory = system.memory if system else None
        disk = system.disk if system else None
        host_info = system.host if system else None
        uptime_seconds = host.uptime_seconds or 0
        return {
            "available": host.available,
            "status": "healthy" if host.available else "unknown",
            "error": self._error(host.error),
            "hostname": host_info.hostname if host_info else "unknown",
            "os": host_info.os if host_info else "unknown",
            "kernel": host_info.kernel if host_info else "unknown",
            "architecture": host_info.architecture if host_info else "unknown",
            "python": host_info.python if host_info else "unknown",
            "uptime": {"seconds": uptime_seconds, "label": self._uptime_label(uptime_seconds)},
            "load": {
                "one": host.load_one or 0.0,
                "five": host.load_five or 0.0,
                "fifteen": host.load_fifteen or 0.0,
            },
            "cpu": {
                "usage_percent": cpu.usage_percent if cpu else 0.0,
                "physical_cores": cpu.physical_cores if cpu else 0,
                "logical_cores": cpu.logical_cores if cpu else 0,
            },
            "memory": {
                "total_gb": memory.total_gb if memory else 0.0,
                "used_gb": memory.used_gb if memory else 0.0,
                "available_gb": memory.available_gb if memory else 0.0,
                "percent": memory.percent if memory else 0.0,
            },
            "swap": {
                "available": host.swap.available,
                "total_gb": host.swap.total_gb,
                "used_gb": host.swap.used_gb,
                "percent": host.swap.percent,
                "error": self._error(host.swap.error),
            },
            "disk": {
                "total_gb": disk.total_gb if disk else 0.0,
                "used_gb": disk.used_gb if disk else 0.0,
                "free_gb": disk.free_gb if disk else 0.0,
                "percent": disk.percent if disk else 0.0,
            },
            "network": {
                "available": host.network.available,
                "interfaces": host.network.interfaces,
                "established": host.network.established if host.network.established is not None else 0,
                "error": self._error(host.network.error),
            },
        }

    def _docker(self, snapshot: DashboardSnapshot) -> dict[str, Any]:
        docker = snapshot.docker
        return {
            "available": docker.available,
            "status": docker.status,
            "containers": [
                {
                    "id": item.container.id,
                    "name": item.container.name,
                    "image": item.container.image,
                    "status": item.container.state,
                    "health": item.container.health,
                    "restart_count": item.restart_count,
                    "started_at": item.started_at,
                    "ports": item.container.ports,
                    "cpu_percent": item.resources.cpu_percent,
                    "memory_percent": item.resources.memory_percent,
                    "memory_used_mb": item.resources.memory_used_mb,
                    "memory_limit_mb": item.resources.memory_limit_mb,
                    "stats": {
                        "available": item.resources.available,
                        "error": self._error(item.resources.error),
                        **self._freshness(item.resources.freshness),
                    },
                }
                for item in docker.containers
            ],
            "summary": {
                "total": len(docker.containers),
                "running": docker.running,
                "stopped": docker.stopped,
                "unhealthy": docker.unhealthy,
            },
            "error": self._error(docker.error),
            "state_sampled_at": docker.state_sampled_at.astimezone(timezone.utc).isoformat() if docker.state_sampled_at else None,
            "resource_freshness": self._freshness(docker.resource_freshness),
        }

    def _projects(self, snapshot: DashboardSnapshot) -> dict[str, Any]:
        projects = snapshot.projects
        return {
            "available": projects.available,
            "status": projects.status,
            "search_paths": list(projects.search_paths),
            "freshness": self._freshness(projects.freshness),
            "projects": [
                {
                    "name": item.project.name,
                    "path": item.project.path,
                    "has_git": item.project.capabilities.has_git,
                    "has_compose": item.project.capabilities.has_compose,
                    "compose_files": item.project.compose_files,
                    "branch": item.project.git.branch if item.project.git else None,
                    "dirty": item.project.git.dirty if item.project.git else None,
                    "ahead": item.project.git.ahead if item.project.git else None,
                    "behind": item.project.git.behind if item.project.git else None,
                }
                for item in projects.projects
            ],
            "error": self._error(projects.error),
        }

    def _tunnel(self, snapshot: DashboardSnapshot) -> dict[str, Any]:
        tunnel = snapshot.tunnel
        return {
            "state": tunnel.state,
            "source": tunnel.source,
            "local_containers": list(tunnel.local_containers),
            "systemd": tunnel.systemd,
            "available": tunnel.available,
            "error": self._error(tunnel.error),
            "note": "Local cloudflared visibility only. Remote Cloudflare account status is intentionally not queried from the VPS agent.",
        }

    @staticmethod
    def _freshness(freshness: TelemetryFreshness | None) -> dict[str, Any]:
        if freshness is None:
            return {"sampled_at": None, "age_seconds": None, "status": "never_sampled", "max_age_seconds": None, "error": None}
        return {"sampled_at": freshness.sampled_at.astimezone(timezone.utc).isoformat() if freshness.sampled_at else None, "age_seconds": freshness.age_seconds, "status": freshness.status.value, "max_age_seconds": freshness.max_age_seconds, "error": DashboardResponseMapper._error(freshness.error)}

    @staticmethod
    def _error(error: TelemetryError | None) -> str | None:
        if error is None:
            return None
        return error.message

    @staticmethod
    def _uptime_label(seconds: int) -> str:
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _seconds = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m"
