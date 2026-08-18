from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from aipm.models.mission_control import Observation, ObservationError
from aipm.models.systemd import SYSTEMD_UNIT_REGISTRY, SystemdUnitId, SystemdUnitRegistryEntry, SystemdUnitSnapshot
from aipm.providers.systemd import SystemdProvider, SystemdProviderError


class SystemdObservationService:
    """Read-only orchestration for the backend-owned Systemd registry."""

    def __init__(
        self,
        provider: SystemdProvider,
        *,
        clock: Callable[[], datetime] | None = None,
        stale_after_seconds: int = 90,
        registry: tuple[SystemdUnitRegistryEntry, ...] = SYSTEMD_UNIT_REGISTRY,
    ) -> None:
        self.provider = provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.stale_after_seconds = stale_after_seconds
        self.registry = registry

    def units(self, *, limit: int = 20) -> dict[str, object]:
        limit = self._bounded_limit(limit)
        now = self._now()
        items: list[dict[str, object]] = []
        errors: list[dict[str, str]] = []
        for entry in self.registry[:limit]:
            observation = self._observe(entry, now)
            items.append(self._map_observation(observation))
            if observation.error is not None:
                errors.append({"unit_id": entry.id.value, "code": observation.error.code, "message": observation.error.message})
        return {
            "observation": self._observation_meta(items, errors, now),
            "units": items,
            "errors": errors,
        }

    def unit(self, unit_id: str) -> dict[str, object]:
        entry = next((item for item in self.registry if item.id.value == unit_id), None)
        if entry is None:
            return {
                "observation": self._meta_error("SYSTEMD_UNIT_NOT_ALLOWLISTED", "Systemd unit is not allow-listed"),
                "unit": None,
                "errors": [{"code": "SYSTEMD_UNIT_NOT_ALLOWLISTED", "message": "Systemd unit is not allow-listed"}],
            }
        observation = self._observe(entry, self._now())
        return {
            "observation": self._meta_from_observation(observation),
            "unit": self._map_observation(observation),
            "errors": [] if observation.error is None else [{"code": observation.error.code, "message": observation.error.message}],
        }

    def _observe(self, entry: SystemdUnitRegistryEntry, now: datetime) -> Observation[SystemdUnitSnapshot]:
        try:
            snapshot = self.provider.observe(entry)
            return Observation.from_sample(
                snapshot,
                observed_at=now,
                now=now,
                max_age_seconds=self.stale_after_seconds,
                available=True,
                transport_ok=True,
            )
        except SystemdProviderError:
            return Observation.from_sample(
                None,
                observed_at=None,
                now=now,
                max_age_seconds=self.stale_after_seconds,
                available=False,
                transport_ok=False,
                error=ObservationError("SYSTEMD_MANAGER_UNAVAILABLE", "Systemd observation unavailable"),
            )
        except Exception:
            return Observation.from_sample(
                None,
                observed_at=None,
                now=now,
                max_age_seconds=self.stale_after_seconds,
                available=False,
                transport_ok=False,
                error=ObservationError("SYSTEMD_OBSERVATION_FAILED", "Systemd observation unavailable"),
            )

    @staticmethod
    def _bounded_limit(limit: int) -> int:
        if limit < 1 or limit > 20:
            raise ValueError("limit must be between 1 and 20")
        return limit

    def _now(self) -> datetime:
        value = self.clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _map_observation(observation: Observation[SystemdUnitSnapshot]) -> dict[str, object]:
        snapshot = observation.data
        if snapshot is None:
            return {
                "id": None,
                "display_name": None,
                "load_state": None,
                "active_state": None,
                "sub_state": None,
                "enabled": None,
                "status": "unavailable",
                "evidence": [],
                "observation_state": observation.state.value,
            }
        return {
            "id": snapshot.id.value,
            "display_name": snapshot.display_name,
            "load_state": snapshot.load_state,
            "active_state": snapshot.active_state,
            "sub_state": snapshot.sub_state,
            "enabled": snapshot.enabled,
            "status": snapshot.status.value,
            "evidence": list(snapshot.evidence),
            "observation_state": observation.state.value,
        }

    @staticmethod
    def _meta_from_observation(observation: Observation[object]) -> dict[str, object]:
        return {
            "state": observation.state.value,
            "available": observation.available,
            "transport_ok": observation.transport_ok,
            "observed_at": observation.observed_at.isoformat() if observation.observed_at else None,
            "age_seconds": observation.age_seconds,
            "max_age_seconds": observation.max_age_seconds,
            "error": None if observation.error is None else {"code": observation.error.code, "message": observation.error.message},
        }

    @classmethod
    def _observation_meta(cls, items: list[dict[str, object]], errors: list[dict[str, str]], now: datetime) -> dict[str, object]:
        if not items:
            return {"state": "never_sampled", "available": False, "transport_ok": True, "observed_at": None, "age_seconds": None, "max_age_seconds": 90, "error": None}
        if errors and all(item["observation_state"] == "error" for item in items):
            return {"state": "error", "available": False, "transport_ok": False, "observed_at": None, "age_seconds": None, "max_age_seconds": 90, "error": {"code": "SYSTEMD_MANAGER_UNAVAILABLE", "message": "Systemd observation unavailable"}}
        return {"state": "fresh", "available": True, "transport_ok": True, "observed_at": now.isoformat(), "age_seconds": 0, "max_age_seconds": 90, "error": None}

    @staticmethod
    def _meta_error(code: str, message: str) -> dict[str, object]:
        return {"state": "error", "available": False, "transport_ok": True, "observed_at": None, "age_seconds": None, "max_age_seconds": 90, "error": {"code": code, "message": message}}
