from __future__ import annotations

from datetime import datetime
from typing import Any

from aipm.models.logs import LogPage, LogSource
from aipm.models.mission_control import Observation, ObservationError


class LogsResponseMapper:
    """Serialize only the bounded, redacted Logs response contract."""

    def response(self, observation: Observation[LogPage], *, source: LogSource | None = None) -> dict[str, Any]:
        page = observation.data
        selected = page.source if page is not None else source
        payload: dict[str, Any] = {
            "observation": self._observation(observation),
            "source": self._source(selected),
            "entries": [],
            "next_cursor": None,
            "truncated": False,
            "returned_lines": 0,
            "returned_bytes": 0,
            "errors": [],
        }
        if observation.error is not None:
            payload["errors"] = [self._error(observation.error)]
        if page is not None:
            payload.update(
                {
                    "entries": [self._entry(entry) for entry in page.entries],
                    "next_cursor": page.next_cursor,
                    "truncated": page.truncated,
                    "returned_lines": page.returned_lines,
                    "returned_bytes": page.returned_bytes,
                    "errors": [self._error(error) for error in page.errors],
                }
            )
        return payload

    def sources(self, sources: list[LogSource]) -> list[dict[str, str]]:
        return [self._source(source) for source in sources]

    @staticmethod
    def _source(source: LogSource | None) -> dict[str, str] | None:
        if source is None:
            return None
        return {"id": source.id, "label": source.label, "kind": source.kind.value, "owner": source.owner.value}

    @staticmethod
    def _entry(entry: Any) -> dict[str, Any]:
        return {
            "timestamp": _timestamp(entry.timestamp),
            "severity": entry.severity.value,
            "message": entry.message,
            "redacted": bool(entry.redacted),
            "evidence": list(entry.evidence),
            "unit": entry.unit_id,
            "project": entry.project_id,
        }

    @staticmethod
    def _error(error: ObservationError | Any) -> dict[str, str]:
        if isinstance(error, ObservationError):
            return {"code": error.code[:64], "message": error.message[:256]}
        return {"code": "LOG_OBSERVATION_FAILED", "message": "Log observation unavailable"}

    @staticmethod
    def _observation(observation: Observation[Any]) -> dict[str, Any]:
        return {
            "state": observation.state.value,
            "available": observation.available,
            "transport_ok": observation.transport_ok,
            "observed_at": _timestamp(observation.observed_at),
            "age_seconds": observation.age_seconds,
            "max_age_seconds": observation.max_age_seconds,
            "error": LogsResponseMapper._error(observation.error) if observation.error else None,
        }


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None
