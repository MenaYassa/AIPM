from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class DashboardServiceHealthApi:
    """Read-only health projection for persisted telemetry and MC-3 observations."""

    def __init__(self, dashboard_api: Any, incidents_api: Any, *, stale_after_seconds: int = 45) -> None:
        self.dashboard_api = dashboard_api
        self.incidents_api = incidents_api
        self.stale_after_seconds = stale_after_seconds

    @classmethod
    def from_application(cls, application: Any, *, dashboard_api: Any | None = None, incidents_api: Any | None = None) -> "DashboardServiceHealthApi":
        from aipm.capabilities.dashboard.api import DashboardApi
        from aipm.capabilities.dashboard.incidents_api import DashboardIncidentsApi

        return cls(
            dashboard_api or DashboardApi.from_application(application),
            incidents_api or DashboardIncidentsApi.from_application(application),
            stale_after_seconds=max(45, int(application.config.telemetry.interval_seconds) * 3),
        )

    def services(self) -> dict[str, Any]:
        try:
            overview = self.dashboard_api.overview()
            telemetry = self._telemetry(overview)
            mc3 = self._mc3()
            states = {telemetry["state"], mc3["state"]}
            overall = "unavailable" if "unavailable" in states else "stale" if "stale" in states else "never_sampled" if states == {"never_sampled"} else "healthy"
            return {
                "available": True,
                "status": "ok",
                "error": None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "services": {"telemetry": telemetry, "mc3": mc3},
                "overall": overall,
            }
        except Exception:
            return {
                "available": False,
                "status": "unavailable",
                "error": "Service health unavailable",
                "services": {
                    "telemetry": self._unavailable("telemetry"),
                    "mc3": self._unavailable("mc3"),
                },
                "overall": "unavailable",
            }

    def _telemetry(self, overview: dict[str, Any]) -> dict[str, Any]:
        host = overview.get("host") or {}
        generated_at = overview.get("generated_at")
        if not host.get("available") or not generated_at:
            return self._unavailable("telemetry", reason="No persisted telemetry sample available")
        return self._observation("telemetry", generated_at, source="/api/overview")

    def _mc3(self) -> dict[str, Any]:
        payload = self.incidents_api.events(range_name="24h", limit=1)
        if not payload.get("available"):
            return self._unavailable("mc3", reason="MC-3 event query unavailable")
        events = payload.get("events") or []
        if not events:
            return {"name": "mc3", "state": "never_sampled", "available": True, "last_observed_at": None, "age_seconds": None, "max_age_seconds": self.stale_after_seconds, "source": "event_query"}
        latest = events[-1]
        observed_at = latest.get("occurred_at") or latest.get("created_at")
        return self._observation("mc3", observed_at, source="event_query")

    def _observation(self, name: str, observed_at: str | None, *, source: str) -> dict[str, Any]:
        if not observed_at:
            return self._unavailable(name, reason="Observation timestamp unavailable")
        try:
            timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            age_seconds = max(0, int((datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()))
            state = "fresh" if age_seconds <= self.stale_after_seconds else "stale"
            return {"name": name, "state": state, "available": True, "last_observed_at": timestamp.astimezone(timezone.utc).isoformat(), "age_seconds": age_seconds, "max_age_seconds": self.stale_after_seconds, "source": source}
        except (TypeError, ValueError):
            return self._unavailable(name, reason="Invalid observation timestamp")

    def _unavailable(self, name: str, *, reason: str = "Service observation unavailable") -> dict[str, Any]:
        return {"name": name, "state": "unavailable", "available": False, "last_observed_at": None, "age_seconds": None, "max_age_seconds": self.stale_after_seconds, "source": None, "error": reason}
