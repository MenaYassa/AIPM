"""Read-only Docker/container detail façade for Mission Control."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from aipm.models.mission_control import Observation, ObservationError
from aipm.models.telemetry import DockerSnapshot, ResourceStats, TelemetryError
from aipm.services.docker.observation import DockerObservationService
from aipm.services.telemetry.docker import DockerTelemetryService
from aipm.mappers.docker_detail import DockerDetailMapper


class DashboardDockerApi:
    """Bounded Docker observation API; lifecycle methods are unreachable here."""

    MAX_CONTAINERS = 200
    MAX_INVENTORY = 200
    MAX_PROJECT_FILTER = 128
    STALE_AFTER_SECONDS = 180

    def __init__(
        self,
        telemetry: DockerTelemetryService,
        observations: DockerObservationService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.telemetry = telemetry
        self.observations = observations
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def from_application(cls, application: Any, *, dashboard_api: Any | None = None) -> "DashboardDockerApi":
        overview = dashboard_api
        if overview is None:
            from aipm.capabilities.dashboard.api import DashboardApi

            overview = DashboardApi.from_application(application)
        docker_service = application.docker
        telemetry = getattr(getattr(overview, "telemetry", None), "docker", None)
        if telemetry is None:
            telemetry = DockerTelemetryService(docker_service)
        return cls(telemetry, DockerObservationService(docker_service))

    def summary(self, *, limit: int = MAX_CONTAINERS, project: str | None = None) -> dict[str, Any]:
        snapshot = self.telemetry.fast_snapshot(now=self.clock())
        details = self._details(snapshot, limit=limit, project=project)
        response = self._snapshot_response(snapshot, details)
        response["groups"] = self._groups(details)
        return response

    def containers(self, *, limit: int = MAX_CONTAINERS, project: str | None = None) -> dict[str, Any]:
        snapshot = self.telemetry.fast_snapshot(now=self.clock())
        details = self._details(snapshot, limit=limit, project=project)
        return self._snapshot_response(snapshot, details)

    def container(self, identifier: str) -> dict[str, Any]:
        selector = self._selector(identifier)
        if not selector:
            return self._error_response("CONTAINER_SELECTOR_INVALID", "Container identifier is invalid")
        try:
            raw = self.observations.container(selector)
            snapshot = self.telemetry.fast_snapshot(now=self.clock())
            resource = self._resource_for(snapshot, selector, raw)
            detail = DockerDetailMapper.container(raw, resources=resource)
            return self._success_response({"container": self._container(detail)})
        except Exception:
            return self._error_response("DOCKER_CONTAINER_UNAVAILABLE", "Container detail unavailable")

    def images(self, *, limit: int = MAX_INVENTORY) -> dict[str, Any]:
        return self._inventory("images", self.observations.images, DockerDetailMapper.image, limit)

    def volumes(self, *, limit: int = MAX_INVENTORY) -> dict[str, Any]:
        return self._inventory("volumes", self.observations.volumes, DockerDetailMapper.volume, limit)

    def networks(self, *, limit: int = MAX_INVENTORY) -> dict[str, Any]:
        return self._inventory("networks", self.observations.networks, DockerDetailMapper.network, limit)

    def _inventory(self, key: str, loader: Callable[[], list[Any]], mapper: Callable[[Any], Any], limit: int) -> dict[str, Any]:
        bounded = self._limit(limit, self.MAX_INVENTORY)
        try:
            values = [mapper(item) for item in loader()[:bounded]]
            return self._success_response({key: [self._dataclass(item) for item in values], "truncated": len(values) >= bounded})
        except Exception:
            return self._error_response("DOCKER_INVENTORY_UNAVAILABLE", f"Docker {key} inventory unavailable")

    def _snapshot_response(self, snapshot: DockerSnapshot, details: list[Any]) -> dict[str, Any]:
        if not snapshot.available:
            message = self._typed_error(snapshot.error) or "Docker telemetry unavailable"
            return self._error_response("DOCKER_TELEMETRY_UNAVAILABLE", message, unavailable=True)
        return self._success_response({
            "containers": [self._container(item) for item in details],
            "summary": {
                "total": len(snapshot.containers),
                "running": snapshot.running,
                "stopped": snapshot.stopped,
                "unhealthy": snapshot.unhealthy,
            },
            "truncated": len(details) < len(snapshot.containers),
        }, observed_at=snapshot.state_sampled_at)

    def _success_response(self, payload: dict[str, Any], *, observed_at: datetime | None = None) -> dict[str, Any]:
        now = self.clock()
        observation = Observation.from_sample(
            payload,
            observed_at=observed_at or now,
            now=now,
            max_age_seconds=self.STALE_AFTER_SECONDS,
            available=True,
            transport_ok=True,
        )
        return {
            "available": True,
            "status": "ok",
            "error": None,
            "observation": self._observation(observation),
            **payload,
        }

    def _error_response(self, code: str, message: str, *, unavailable: bool = False) -> dict[str, Any]:
        now = self.clock()
        error = None if unavailable else ObservationError(code, message)
        observation = Observation.from_sample(
            None,
            observed_at=None,
            now=now,
            max_age_seconds=self.STALE_AFTER_SECONDS,
            available=False,
            transport_ok=unavailable,
            error=error,
        )
        return {
            "available": False,
            "status": "unavailable" if unavailable else "error",
            "error": message,
            "observation": self._observation(observation),
            "items": [],
            "truncated": False,
        }

    def _details(self, snapshot: DockerSnapshot, *, limit: int, project: str | None) -> list[Any]:
        bounded = self._limit(limit, self.MAX_CONTAINERS)
        project_filter = self._project(project)
        values = []
        for item in snapshot.containers:
            detail = DockerDetailMapper.container(item.container, resources=item.resources)
            if project_filter and detail.project_key != project_filter:
                continue
            values.append(detail)
            if len(values) >= bounded:
                break
        return values

    @staticmethod
    def _resource_for(snapshot: DockerSnapshot, selector: str, raw: Any) -> ResourceStats | None:
        raw_name = str(getattr(raw, "name", ""))
        raw_id = str(getattr(raw, "short_id", getattr(raw, "id", "")))
        for item in snapshot.containers:
            if item.container.name in {selector, raw_name} or item.container.id in {selector, raw_id}:
                return item.resources
        return None

    @staticmethod
    def _groups(details: list[Any]) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for item in details:
            key = item.project_key or "ungrouped"
            group = groups.setdefault(key, {"project_key": key, "total": 0, "running": 0, "unhealthy": 0})
            group["total"] += 1
            group["running"] += item.state == "running"
            group["unhealthy"] += item.health == "unhealthy"
        return sorted(groups.values(), key=lambda item: item["project_key"])

    @staticmethod
    def _container(item: Any) -> dict[str, Any]:
        resources = item.resources
        return {
            "id": item.id,
            "name": item.name,
            "project_key": item.project_key,
            "service_name": item.service_name,
            "image": item.image,
            "state": item.state,
            "health": item.health,
            "restart_count": item.restart_count,
            "started_at": item.started_at,
            "ports": list(item.ports),
            "networks": list(item.networks),
            "mount_kinds": list(item.mount_kinds),
            "resources": {
                "cpu_percent": resources.cpu_percent if resources else None,
                "memory_used_mb": resources.memory_used_mb if resources else None,
                "memory_limit_mb": resources.memory_limit_mb if resources else None,
                "memory_percent": resources.memory_percent if resources else None,
                "available": resources.available if resources else False,
                "error": DashboardDockerApi._typed_error(resources.error) if resources else "Resource sample unavailable",
                "freshness": DashboardDockerApi._freshness(resources.freshness) if resources else {"status": "never_sampled", "sampled_at": None, "age_seconds": None},
            },
        }

    @staticmethod
    def _dataclass(item: Any) -> dict[str, Any]:
        if hasattr(item, "__dataclass_fields__"):
            return {name: getattr(item, name) for name in item.__dataclass_fields__}
        return {}

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
    def _freshness(freshness: Any) -> dict[str, Any]:
        if freshness is None:
            return {"status": "never_sampled", "sampled_at": None, "age_seconds": None}
        return {
            "status": freshness.status.value,
            "sampled_at": freshness.sampled_at.isoformat() if freshness.sampled_at else None,
            "age_seconds": freshness.age_seconds,
            "max_age_seconds": freshness.max_age_seconds,
            "error": DashboardDockerApi._typed_error(freshness.error),
        }

    @staticmethod
    def _typed_error(error: Any) -> str | None:
        return error.message if isinstance(error, (TelemetryError, ObservationError)) else None

    @staticmethod
    def _limit(value: int, maximum: int) -> int:
        try:
            return max(1, min(int(value), maximum))
        except (TypeError, ValueError):
            return maximum

    @staticmethod
    def _selector(value: str) -> str | None:
        value = str(value or "").strip()
        return value[:128] if value and len(value) <= 128 else None

    @staticmethod
    def _project(value: str | None) -> str | None:
        value = str(value or "").strip()
        return value[:128] if value else None
