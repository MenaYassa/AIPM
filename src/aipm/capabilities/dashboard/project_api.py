"""Read-only Mission Control project/application intelligence façade."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from aipm.capabilities.dashboard.api import DashboardApi
from aipm.mappers.project_intelligence import ProjectIntelligenceMapper
from aipm.models.mission_control import Observation, ObservationError
from aipm.models.project_intelligence import InventoryScope
from aipm.services.compose.service import ComposeService
from aipm.services.docker.observation import DockerObservationService
from aipm.services.project.intelligence import ProjectIntelligenceService
from aipm.services.project.service import ProjectService
from aipm.services.telemetry.docker import DockerTelemetryService


class DashboardProjectApi:
    """Expose project/application observations only; no mutation methods exist."""

    MAX_PROJECTS = 200
    MAX_COMPONENTS = 200
    MAX_SEARCH = 128
    MAX_STATUS = 16
    MAX_SCOPE = 16

    def __init__(self, intelligence: ProjectIntelligenceService, *, clock: Callable[[], datetime] | None = None) -> None:
        self.intelligence = intelligence
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def from_application(cls, application: Any, *, dashboard_api: DashboardApi | None = None) -> "DashboardProjectApi":
        overview = dashboard_api or DashboardApi.from_application(application)
        project_service = ProjectService(app=application)
        docker_service = getattr(application, "docker", None)
        docker_observation = DockerObservationService(docker_service)
        docker_telemetry = getattr(getattr(overview, "telemetry", None), "docker", None)
        if docker_telemetry is None and docker_service is not None:
            docker_telemetry = DockerTelemetryService(docker_service)
        if docker_telemetry is None:
            docker_telemetry = _UnavailableDockerTelemetry()
        compose_service = ComposeService() if docker_service is not None else None
        return cls(ProjectIntelligenceService(project_service, docker_observation, docker_telemetry, compose_service=compose_service))

    def projects(self, *, limit: int = MAX_PROJECTS, search: str | None = None, status: str | None = None, scope: str = InventoryScope.ALL.value) -> dict[str, Any]:
        if not self._valid_status(status):
            return self._error("PROJECT_STATUS_INVALID", "Project status filter is invalid")
        scope_value = self._scope(scope)
        if scope_value is None:
            return self._error("PROJECT_SCOPE_INVALID", "Project inventory scope is invalid")
        inventory = self.intelligence.inventory(limit=self._limit(limit, self.MAX_PROJECTS), search=self._search(search), status=status, scope=scope_value)
        return ProjectIntelligenceMapper.inventory(inventory)

    def project(self, project_id: str) -> dict[str, Any]:
        identifier = self._identifier(project_id)
        if identifier is None:
            return self._error("PROJECT_ID_INVALID", "Project identifier is invalid")
        value = self.intelligence.detail(identifier)
        if value is None:
            return self._error("PROJECT_NOT_FOUND", "Project is unavailable")
        return self._success({"project": ProjectIntelligenceMapper.project(value)})

    def containers(self, project_id: str) -> dict[str, Any]:
        identifier = self._identifier(project_id)
        if identifier is None:
            return self._error("PROJECT_ID_INVALID", "Project identifier is invalid")
        values = self.intelligence.containers(identifier)
        if values is None:
            return self._error("PROJECT_NOT_FOUND", "Project is unavailable")
        return self._success({"containers": [ProjectIntelligenceMapper.component(item) for item in values[: self.MAX_COMPONENTS]], "truncated": len(values) > self.MAX_COMPONENTS})

    def health(self, project_id: str) -> dict[str, Any]:
        identifier = self._identifier(project_id)
        if identifier is None:
            return self._error("PROJECT_ID_INVALID", "Project identifier is invalid")
        value = self.intelligence.health(identifier)
        if value is None:
            return self._error("PROJECT_NOT_FOUND", "Project is unavailable")
        return self._success({"health": ProjectIntelligenceMapper.health(value)})

    def _success(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        observation = Observation.from_sample(payload, observed_at=now, now=now, max_age_seconds=60, available=True, transport_ok=True)
        return {"available": True, "status": "ok", "error": None, "observation": self._observation(observation), **payload}

    def _error(self, code: str, message: str) -> dict[str, Any]:
        now = self.clock()
        observation = Observation.from_sample(None, observed_at=None, now=now, max_age_seconds=60, available=False, transport_ok=True, error=ObservationError(code, message))
        return {"available": False, "status": "error", "error": message, "observation": self._observation(observation), "projects": [], "containers": [], "truncated": False}

    @staticmethod
    def _observation(observation: Observation[Any]) -> dict[str, Any]:
        return {
            "transport_ok": observation.transport_ok,
            "available": observation.available,
            "state": observation.state.value,
            "observed_at": observation.observed_at.isoformat() if observation.observed_at else None,
            "age_seconds": observation.age_seconds,
            "max_age_seconds": observation.max_age_seconds,
            "error": observation.error.message if observation.error else None,
        }

    @staticmethod
    def _limit(value: int, maximum: int) -> int:
        try:
            return max(1, min(int(value), maximum))
        except (TypeError, ValueError):
            return maximum

    @staticmethod
    def _search(value: str | None) -> str | None:
        value = str(value or "").strip()
        return value[:128] if value else None

    @staticmethod
    def _identifier(value: str | None) -> str | None:
        value = str(value or "").strip()
        return value if len(value) == 24 and all(char in "0123456789abcdef" for char in value) else None

    @staticmethod
    def _valid_status(value: str | None) -> bool:
        return value is None or value in {"green", "yellow", "red", "unknown"}

    @staticmethod
    def _scope(value: str | None) -> InventoryScope | None:
        value = str(value or "").strip().lower()[:16]
        try:
            return InventoryScope(value)
        except ValueError:
            return None


class _UnavailableDockerTelemetry:
    def fast_snapshot(self, *, now):
        raise RuntimeError("Docker runtime observation unavailable")


__all__ = ["DashboardProjectApi"]
