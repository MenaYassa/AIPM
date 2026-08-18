from __future__ import annotations

from typing import Any


class SystemdResponseMapper:
    """Keep the Systemd API payload to explicitly safe observation fields."""

    @staticmethod
    def list_response(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "observation": SystemdResponseMapper._observation(payload.get("observation")),
            "units": [SystemdResponseMapper.unit(item) for item in payload.get("units", [])],
            "errors": SystemdResponseMapper._errors(payload.get("errors", [])),
        }

    @staticmethod
    def detail_response(payload: dict[str, Any]) -> dict[str, Any]:
        unit = payload.get("unit")
        return {
            "observation": SystemdResponseMapper._observation(payload.get("observation")),
            "unit": None if unit is None else SystemdResponseMapper.unit(unit),
            "errors": SystemdResponseMapper._errors(payload.get("errors", [])),
        }

    @staticmethod
    def unit(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("id"),
            "display_name": item.get("display_name"),
            "load_state": item.get("load_state"),
            "active_state": item.get("active_state"),
            "sub_state": item.get("sub_state"),
            "enabled": item.get("enabled"),
            "status": item.get("status", "unknown"),
            "observation_state": item.get("observation_state", "unknown"),
            "evidence": [str(value)[:160] for value in item.get("evidence", [])][:8],
        }

    @staticmethod
    def _observation(item: dict[str, Any] | None) -> dict[str, Any]:
        item = item or {}
        error = item.get("error")
        safe_error = None
        if isinstance(error, dict):
            safe_error = {"code": str(error.get("code", "OBSERVATION_ERROR"))[:80], "message": str(error.get("message", "Observation unavailable"))[:160]}
        return {
            "state": str(item.get("state", "unknown")),
            "available": bool(item.get("available", False)),
            "transport_ok": bool(item.get("transport_ok", False)),
            "observed_at": item.get("observed_at"),
            "age_seconds": item.get("age_seconds"),
            "max_age_seconds": item.get("max_age_seconds"),
            "error": safe_error,
        }

    @staticmethod
    def _errors(items: Any) -> list[dict[str, str]]:
        if not isinstance(items, list):
            return []
        result: list[dict[str, str]] = []
        for item in items[:20]:
            if isinstance(item, dict):
                result.append({"code": str(item.get("code", "OBSERVATION_ERROR"))[:80], "message": str(item.get("message", "Observation unavailable"))[:160]})
        return result
