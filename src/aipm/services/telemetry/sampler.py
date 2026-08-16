from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from aipm.mappers.telemetry_history import TelemetryHistoryMapper
from aipm.models.config import TelemetryConfig
from aipm.models.history import SampleResult
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
            cutoff = mapped.run.sampled_at - timedelta(days=self.config.retention_days)
            retention_deleted = self.repository.delete_older_than(cutoff)
            return SampleResult(
                sampled_at=mapped.run.sampled_at,
                run_id=run_id,
                host_rows=1 if mapped.host is not None else 0,
                container_rows=len(mapped.containers),
                project_rows=len(mapped.projects),
                tunnel_rows=1 if mapped.tunnel is not None else 0,
                retention_deleted=retention_deleted,
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
