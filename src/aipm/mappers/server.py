"""Safe mapper for the additive Mission Control Server contract."""

from __future__ import annotations

from typing import Any

from aipm.models.mission_control import Observation, ObservationError
from aipm.models.server import ServerHostSnapshot
from aipm.models.telemetry import TelemetryError


class ServerResponseMapper:
    """Map a typed Server snapshot into a bounded JSON-ready response."""

    def to_response(
        self,
        snapshot: ServerHostSnapshot,
        observation: Observation[object],
        *,
        health: dict[str, Any],
        incidents: dict[str, Any],
    ) -> dict[str, Any]:
        host = snapshot.host
        system = host.system
        identity = system.host if system else None
        cpu = system.cpu if system else None
        memory = system.memory if system else None
        disk = system.disk if system else None
        network = host.network
        root = {
            "path": "/",
            "available": disk is not None,
            "total_gb": disk.total_gb if disk else None,
            "used_gb": disk.used_gb if disk else None,
            "free_gb": disk.free_gb if disk else None,
            "percent": disk.percent if disk else None,
        }
        incident_summary = self._incident_summary(incidents)
        return {
            "available": observation.available,
            "status": self._status(observation),
            "error": self._error(observation.error),
            "observation": self._observation(observation),
            "identity": {
                "hostname": identity.hostname if identity else None,
                "os": identity.os if identity else None,
                "kernel": identity.kernel if identity else None,
                "architecture": identity.architecture if identity else None,
                "python": identity.python if identity else None,
            },
            "uptime": {
                "seconds": host.uptime_seconds,
                "label": self._uptime_label(host.uptime_seconds),
            },
            "cpu": {
                "usage_percent": cpu.usage_percent if cpu else None,
                "physical_cores": cpu.physical_cores if cpu else None,
                "logical_cores": cpu.logical_cores if cpu else None,
                "load": {
                    "one": host.load_one,
                    "five": host.load_five,
                    "fifteen": host.load_fifteen,
                },
            },
            "memory": {
                "total_gb": memory.total_gb if memory else None,
                "used_gb": memory.used_gb if memory else None,
                "available_gb": memory.available_gb if memory else None,
                "percent": memory.percent if memory else None,
            },
            "swap": {
                "available": host.swap.available,
                "total_gb": host.swap.total_gb if host.swap.available else None,
                "used_gb": host.swap.used_gb if host.swap.available else None,
                "percent": host.swap.percent if host.swap.available else None,
                "error": self._error(host.swap.error),
            },
            "disk": {
                "root": root,
                "filesystems": [
                    {
                        "mountpoint": item.mountpoint,
                        "filesystem": item.filesystem,
                        "total_gb": item.total_gb,
                        "used_gb": item.used_gb,
                        "free_gb": item.free_gb,
                        "percent": item.percent,
                    }
                    for item in snapshot.filesystems
                ],
                "filesystem_detail": {
                    "available": snapshot.filesystem_available,
                    "error": self._error(snapshot.filesystem_error),
                },
                "error": None if disk else self._error(host.error),
            },
            "network": {
                "available": network.available,
                "interfaces": [
                    {
                        "name": item.name,
                        "is_up": item.is_up,
                        "rx_bytes": item.rx_bytes,
                        "tx_bytes": item.tx_bytes,
                    }
                    for item in snapshot.interfaces
                ],
                "interface_detail": {
                    "available": snapshot.interface_detail_available,
                    "error": self._error(snapshot.interface_detail_error),
                },
                "established": network.established,
                "states": dict(snapshot.connection_states),
                "connection_states_detail": {
                    "available": snapshot.connection_states_available,
                    "error": self._error(snapshot.connection_states_error),
                },
                "error": self._error(network.error),
            },
            "health": {
                "state": self._health_state(observation, health, incident_summary),
                "service_pulse": self._safe_health(health),
                "incidents": incident_summary,
                "warnings": {
                    "available": False,
                    "items": [],
                    "error": "Resource warning projection unavailable from current telemetry",
                },
            },
        }

    @staticmethod
    def _status(observation: Observation[object]) -> str:
        if observation.state.value == "error":
            return "error"
        if observation.state.value in {"unavailable", "never_sampled", "unknown"}:
            return observation.state.value
        return "ok"

    @staticmethod
    def _observation(observation: Observation[object]) -> dict[str, Any]:
        return {
            "transport_ok": observation.transport_ok,
            "available": observation.available,
            "state": observation.state.value,
            "observed_at": observation.observed_at.isoformat() if observation.observed_at else None,
            "age_seconds": observation.age_seconds,
            "max_age_seconds": observation.max_age_seconds,
            "error": ServerResponseMapper._error(observation.error),
        }

    @staticmethod
    def _error(error: Any) -> str | None:
        if isinstance(error, (ObservationError, TelemetryError)):
            return error.message
        return None

    @staticmethod
    def _incident_summary(payload: dict[str, Any]) -> dict[str, Any]:
        if not payload.get("available"):
            return {"available": False, "open": None, "error": payload.get("error") or "Incident summary unavailable"}
        incidents = payload.get("incidents") or []
        return {"available": True, "open": len(incidents), "error": None}

    @staticmethod
    def _safe_health(payload: dict[str, Any]) -> dict[str, Any]:
        if not payload.get("available"):
            return {"available": False, "status": "unavailable", "overall": "unavailable", "services": {}, "error": payload.get("error") or "Service health unavailable"}
        return {
            "available": True,
            "status": payload.get("status", "ok"),
            "overall": payload.get("overall", "unknown"),
            "services": payload.get("services") or {},
            "error": None,
        }

    @staticmethod
    def _health_state(observation: Observation[object], health: dict[str, Any], incidents: dict[str, Any]) -> str:
        if observation.state.value in {"error", "unavailable", "never_sampled", "unknown"}:
            return observation.state.value
        if not health.get("available") or not incidents.get("available"):
            return "unavailable"
        overall = health.get("overall")
        return overall if overall in {"stale", "unavailable", "never_sampled"} else "fresh"

    @staticmethod
    def _uptime_label(seconds: int | None) -> str | None:
        if seconds is None:
            return None
        days, remainder = divmod(max(0, seconds), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _seconds = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m"
