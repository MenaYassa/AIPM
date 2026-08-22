from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Any, Callable

from aipm.mappers.telemetry_history import TelemetryHistoryMapper
from aipm.models.config import TelemetryConfig
from aipm.models.history import SampleResult
from aipm.models.telemetry import TelemetryError
from aipm.models.telemetry_sampling import RetentionCleanupResult
from aipm.repositories.telemetry.base import HistoryRepository
from aipm.services.telemetry.dashboard import DashboardTelemetryService


class TelemetrySampler:
    """Persist typed dashboard snapshots without inspecting or mutating infrastructure."""

    def __init__(
        self,
        telemetry_service: DashboardTelemetryService,
        mapper: TelemetryHistoryMapper,
        repository: HistoryRepository,
        config: TelemetryConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        logger: Any | None = None,
    ) -> None:
        self.telemetry_service = telemetry_service
        self.mapper = mapper
        self.repository = repository
        self.config = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.logger = logger

    def sample_fast_once(self) -> SampleResult:
        """Persist one fast state snapshot without waiting for slow work."""
        started_at = self.clock()
        if not self.config.enabled:
            return SampleResult(sampled_at=started_at, run_id=None, host_rows=0, container_rows=0, project_rows=0, tunnel_rows=0, retention_deleted=0, skipped=True)
        started = self.monotonic()
        try:
            snapshot = self.telemetry_service.fast_snapshot()
            mapped = self.mapper.to_sample(snapshot, duration_ms=max(0, int((self.monotonic() - started) * 1000)))
            run_id = self.repository.save_sample(mapped.run, mapped.host, mapped.containers, mapped.projects, mapped.tunnel)
            return SampleResult(sampled_at=mapped.run.sampled_at, run_id=run_id, host_rows=1 if mapped.host else 0, container_rows=len(mapped.containers), project_rows=len(mapped.projects), tunnel_rows=1 if mapped.tunnel else 0, retention_deleted=0)
        except Exception as exc:
            self._log("Fast telemetry sampling failed", exc)
            return SampleResult(sampled_at=_utc(started_at), run_id=None, host_rows=0, container_rows=0, project_rows=0, tunnel_rows=0, retention_deleted=0, error="Fast telemetry sampling unavailable")

    def cleanup_retention(self) -> RetentionCleanupResult:
        """Run bounded history retention separately from fast telemetry sampling."""
        started = self.monotonic()
        if not self.config.enabled:
            return RetentionCleanupResult(deleted_rows=0, duration_ms=0)
        if self.logger is not None:
            self.logger.info("Telemetry retention started")
        try:
            cutoff = self.clock() - timedelta(days=self.config.retention_days)
            deleted = self.repository.delete_older_than(cutoff)
            duration_ms = max(0, int((self.monotonic() - started) * 1000))
            if self.logger is not None:
                self.logger.info(
                    "Telemetry retention completed",
                    extra={"deleted_rows": deleted, "duration_ms": duration_ms},
                )
            return RetentionCleanupResult(deleted_rows=deleted, duration_ms=duration_ms)
        except Exception:
            duration_ms = max(0, int((self.monotonic() - started) * 1000))
            if self.logger is not None:
                self.logger.error(
                    "Telemetry retention failed",
                    extra={"deleted_rows": 0, "duration_ms": duration_ms},
                )
            return RetentionCleanupResult(
                deleted_rows=0,
                duration_ms=duration_ms,
                error=TelemetryError("RETENTION_UNAVAILABLE", "Telemetry retention unavailable"),
            )

    def refresh_resource_once(self) -> SampleResult:
        """Run one aggregate slow resource refresh and persist sparse resource history."""
        started_at = self.clock()
        if not self.config.enabled or not self.config.resource_sampling_enabled:
            return SampleResult(sampled_at=started_at, run_id=None, host_rows=0, container_rows=0, project_rows=0, tunnel_rows=0, retention_deleted=0, skipped=True)
        try:
            result = self.telemetry_service.refresh_resources(timeout_seconds=self.config.resource_timeout_seconds, now=started_at)
            points = tuple(self.mapper._container(item, result.sampled_at) for item in result.containers)
            run_id = self.repository.save_resource_sample(result.sampled_at, points, duration_ms=result.duration_ms, status=result.status, error_code=result.error.code if result.error else None)
            return SampleResult(sampled_at=result.sampled_at, run_id=run_id, host_rows=0, container_rows=len(points), project_rows=0, tunnel_rows=0, retention_deleted=0, error=None if result.status == "healthy" else "Resource telemetry unavailable")
        except Exception as exc:
            self._log("Resource telemetry refresh failed", exc)
            return SampleResult(sampled_at=_utc(started_at), run_id=None, host_rows=0, container_rows=0, project_rows=0, tunnel_rows=0, retention_deleted=0, error="Resource telemetry unavailable")

    def refresh_project_once(self, cancel_event: Event | None = None, deadline: float | None = None) -> None:
        if self.config.enabled:
            self.telemetry_service.projects.snapshot(cancel_event=cancel_event, deadline=deadline, bounded=True)

    def sample_once(self) -> SampleResult:
        started_at = self.clock()
        if not self.config.enabled:
            return SampleResult(
                sampled_at=started_at,
                run_id=None,
                host_rows=0,
                container_rows=0,
                project_rows=0,
                tunnel_rows=0,
                retention_deleted=0,
                skipped=True,
            )

        started = self.monotonic()
        try:
            snapshot = self.telemetry_service.snapshot()
            duration_ms = max(0, int((self.monotonic() - started) * 1000))
            mapped = self.mapper.to_sample(snapshot, duration_ms=duration_ms)
            run_id = self.repository.save_sample(
                mapped.run,
                mapped.host,
                mapped.containers,
                mapped.projects,
                mapped.tunnel,
            )
            return SampleResult(
                sampled_at=mapped.run.sampled_at,
                run_id=run_id,
                host_rows=1 if mapped.host is not None else 0,
                container_rows=len(mapped.containers),
                project_rows=len(mapped.projects),
                tunnel_rows=1 if mapped.tunnel is not None else 0,
                retention_deleted=0,
            )
        except Exception as exc:
            self._log("Telemetry sampling failed", exc)
            return SampleResult(
                sampled_at=_utc(started_at),
                run_id=None,
                host_rows=0,
                container_rows=0,
                project_rows=0,
                tunnel_rows=0,
                retention_deleted=0,
                error="Telemetry sampling unavailable",
            )

    def _log(self, message: str, exc: Exception) -> None:
        if self.logger is not None:
            self.logger.exception(message, exc_info=exc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
