from __future__ import annotations

from typing import Any

from aipm.capabilities.dashboard.query_bounds import validate_limit
from aipm.core.app import Application
from aipm.mappers.systemd import SystemdResponseMapper
from aipm.providers.systemd import LocalSystemdProvider
from aipm.services.systemd.observation import SystemdObservationService


class DashboardSystemdApi:
    """GET-only Mission Control façade for allow-listed Systemd observations."""

    def __init__(self, service: SystemdObservationService, mapper: SystemdResponseMapper | None = None) -> None:
        self.service = service
        self.mapper = mapper or SystemdResponseMapper()

    @classmethod
    def from_application(cls, application: Application) -> "DashboardSystemdApi":
        return cls(SystemdObservationService(LocalSystemdProvider()))

    def units(self, *, limit: int = 20) -> dict[str, Any]:
        try:
            bounded = validate_limit(limit, maximum=20)
            return self.mapper.list_response(self.service.units(limit=bounded))
        except ValueError:
            raise
        except Exception:
            return self.mapper.list_response({
                "observation": {
                    "state": "error",
                    "available": False,
                    "transport_ok": False,
                    "error": {"code": "SYSTEMD_OBSERVATION_FAILED", "message": "Systemd observation unavailable"},
                },
                "units": [],
                "errors": [{"code": "SYSTEMD_OBSERVATION_FAILED", "message": "Systemd observation unavailable"}],
            })

    def unit(self, unit_id: str) -> dict[str, Any]:
        return self.mapper.detail_response(self.service.unit(unit_id))
