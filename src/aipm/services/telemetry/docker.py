from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any, Callable

from aipm.core.exceptions import DockerError
from aipm.mappers.docker import DockerMapper
from aipm.models.history import ContainerHistoryPoint
from aipm.models.telemetry import ContainerSnapshot, DockerSnapshot, ResourceStats, TelemetryError, TelemetryFreshness
from aipm.models.telemetry_sampling import ResourceRefreshResult
from aipm.services.docker.service import DockerService


class DockerTelemetryService:
    """Collect read-only Docker state quickly and resources through one aggregate slow task."""

    def __init__(self, docker_service: DockerService, *, logger: Any | None = None, clock: Callable[[], datetime] | None = None, monotonic: Callable[[], float] | None = None, resource_stale_after_seconds: int = 180) -> None:
        self.docker_service = docker_service
        self.logger = logger
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.resource_stale_after_seconds = resource_stale_after_seconds
        self._resource_cache: dict[str, ResourceStats] = {}
        self._resource_sampled_at: datetime | None = None
        self._resource_error: TelemetryError | None = None

    def snapshot(self) -> DockerSnapshot:
        """Legacy synchronous snapshot retained for rollback and compatibility."""
        now = self.clock()
        try:
            raw_containers = self.docker_service.provider.list_containers()
        except Exception as exc:
            return self._unavailable(exc)
        snapshots = []
        for raw_container in sorted(raw_containers, key=lambda item: getattr(item, "name", "").lower()):
            base = DockerMapper.container(raw_container)
            stats = None
            stats_error = None
            if base.state == "running":
                try:
                    stats = self.docker_service.provider.stats(raw_container)
                except Exception as exc:
                    stats_error = self._error("CONTAINER_STATS_UNAVAILABLE", "Container resource telemetry unavailable", exc)
            resources = DockerMapper.resource_stats(stats)
            if stats_error is not None:
                resources = ResourceStats(available=False, error=stats_error)
            resources = self._fresh_resources(resources, now, now if resources.available else None)
            snapshots.append(ContainerSnapshot(container=base, resources=resources, restart_count=_restart_count(raw_container), started_at=_started_at(raw_container)))
        return DockerSnapshot(available=True, status="healthy", containers=tuple(snapshots), state_sampled_at=now, resource_freshness=self._aggregate_freshness(now))

    def hydrate_resources(self, points: list[ContainerHistoryPoint], *, now: datetime | None = None) -> None:
        now = now or self.clock()
        for point in points:
            sampled_at = point.resource_sampled_at or point.sampled_at
            resources = ResourceStats(cpu_percent=point.cpu_percent, memory_used_mb=point.memory_used_mb, memory_limit_mb=point.memory_limit_mb, memory_percent=point.memory_percent, available=point.stats_available, freshness=TelemetryFreshness.from_sample(sampled_at, now=now, max_age_seconds=self.resource_stale_after_seconds, available=point.stats_available))
            self._resource_cache[point.container_name] = resources
        if points:
            self._resource_sampled_at = max(point.resource_sampled_at or point.sampled_at for point in points)

    def fast_snapshot(self, *, now: datetime | None = None) -> DockerSnapshot:
        """Fast state-only snapshot; it never calls per-container stats."""
        now = now or self.clock()
        try:
            raw_containers = self.docker_service.provider.list_containers()
        except Exception as exc:
            return self._unavailable(exc)
        snapshots = []
        for raw_container in sorted(raw_containers, key=lambda item: getattr(item, "name", "").lower()):
            base = DockerMapper.container(raw_container)
            key = _container_key(raw_container, base.id)
            resources = self._resource_cache.get(key) or ResourceStats(freshness=TelemetryFreshness.never_sampled(self.resource_stale_after_seconds))
            snapshots.append(ContainerSnapshot(container=base, resources=self._fresh_resources(resources, now, self._resource_sampled_at), restart_count=_restart_count(raw_container), started_at=_started_at(raw_container)))
        return DockerSnapshot(available=True, status="healthy", containers=tuple(snapshots), state_sampled_at=now, resource_freshness=self._aggregate_freshness(now))

    def refresh_resources(self, *, timeout_seconds: int = 15, now: datetime | None = None) -> ResourceRefreshResult:
        now = now or self.clock()
        started_monotonic = self.monotonic()
        try:
            raw_containers = self.docker_service.provider.list_containers()
            aggregate = self.docker_service.provider.stats_all(timeout_seconds=timeout_seconds)
            snapshots = []
            for raw_container in sorted(raw_containers, key=lambda item: getattr(item, "name", "").lower()):
                base = DockerMapper.container(raw_container)
                key = _container_key(raw_container, base.id)
                data = aggregate.get(getattr(raw_container, "name", "")) or aggregate.get(key) or aggregate.get(base.id)
                resources = _normalized_resources(data)
                if base.state != "running":
                    resources = ResourceStats(available=False, error=TelemetryError("RESOURCE_NOT_RUNNING", "Container is not running"))
                elif resources is None:
                    resources = ResourceStats(available=False, error=TelemetryError("RESOURCE_NOT_RETURNED", "Aggregate resource telemetry did not return this container"))
                resources = self._fresh_resources(resources, now, now if resources.available else None)
                self._resource_cache[key] = resources
                snapshots.append(ContainerSnapshot(container=base, resources=resources, restart_count=_restart_count(raw_container), started_at=_started_at(raw_container)))
            self._resource_sampled_at = now
            self._resource_error = None
            return ResourceRefreshResult(sampled_at=now, duration_ms=max(0, int((self.monotonic() - started_monotonic) * 1000)), status="healthy", containers=tuple(snapshots))
        except Exception as exc:
            error = self._error("AGGREGATE_RESOURCE_UNAVAILABLE", "Aggregate Docker resource telemetry unavailable", exc)
            self._resource_error = error
            return ResourceRefreshResult(sampled_at=now, duration_ms=max(0, int((self.monotonic() - started_monotonic) * 1000)), status="unavailable", containers=(), error=error)

    def _fresh_resources(self, resources: ResourceStats, now: datetime, sampled_at: datetime | None) -> ResourceStats:
        source_time = sampled_at if sampled_at is not None else (resources.freshness.sampled_at if resources.freshness else None)
        freshness = TelemetryFreshness.from_sample(source_time, now=now, max_age_seconds=self.resource_stale_after_seconds, available=resources.available, error=resources.error)
        return ResourceStats(resources.cpu_percent, resources.memory_used_mb, resources.memory_limit_mb, resources.memory_percent, resources.available, resources.error, freshness)

    def _aggregate_freshness(self, now: datetime) -> TelemetryFreshness:
        return TelemetryFreshness.from_sample(self._resource_sampled_at, now=now, max_age_seconds=self.resource_stale_after_seconds, available=self._resource_error is None and self._resource_sampled_at is not None, error=self._resource_error)

    def _unavailable(self, exc: Exception) -> DockerSnapshot:
        error = self._error("DOCKER_TELEMETRY_UNAVAILABLE", "Docker telemetry unavailable", exc)
        return DockerSnapshot.unavailable_snapshot(error)

    def _error(self, code: str, message: str, exc: Exception) -> TelemetryError:
        if self.logger is not None:
            self.logger.exception(message, exc_info=exc)
        return TelemetryError(code=code, message=message)


def _container_key(raw_container: Any, mapped_id: str) -> str:
    return str(getattr(raw_container, "name", "") or mapped_id)


def _restart_count(raw_container: Any) -> int:
    return int(((getattr(raw_container, "attrs", {}) or {}).get("State", {}) or {}).get("RestartCount", 0) or 0)


def _started_at(raw_container: Any) -> str | None:
    return ((getattr(raw_container, "attrs", {}) or {}).get("State", {}) or {}).get("StartedAt")


def _normalized_resources(data: object) -> ResourceStats | None:
    if data is None:
        return None
    if isinstance(data, ResourceStats):
        return data
    if isinstance(data, dict) and "cpu_percent" in data:
        return ResourceStats(cpu_percent=data.get("cpu_percent"), memory_used_mb=data.get("memory_used_mb"), memory_limit_mb=data.get("memory_limit_mb"), memory_percent=data.get("memory_percent"))
    return DockerMapper.resource_stats(data if isinstance(data, dict) else None)
