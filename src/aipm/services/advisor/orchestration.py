"""Server-owned live advisor orchestration over the bounded Phase 4D pipeline.

This module owns only evaluation context and sequencing. Telemetry storage remains
owned by ``export_telemetry_snapshot``; mapping remains owned by the Phase 4D
adapter; semantic evaluation remains owned by the Phase 4A composition boundary.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from aipm.models.advisor import AdvisorResponse, AdvisorScope
from aipm.models.config import AIPMConfig
from aipm.repositories.telemetry.read_snapshot import (
    INITIAL_HISTORY_WINDOW_SECONDS,
    TelemetrySnapshotExport,
    export_telemetry_snapshot,
)
from aipm.services.advisor.composition import AdvisorCompositionRequest, compose_advisor
from aipm.services.advisor.observation_adapter import adapt_telemetry_snapshot


class AdvisorOrchestrationError(RuntimeError):
    """Raised when the live advisor orchestration context is invalid."""


AdvisorClock = Callable[[], datetime]
AdvisorSnapshotExporter = Callable[..., TelemetrySnapshotExport]
AdvisorAdapter = Callable[..., AdvisorCompositionRequest]
AdvisorComposer = Callable[[AdvisorCompositionRequest], AdvisorResponse]


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AdvisorOrchestrationError("Advisor evaluation clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _request_id(host_id: str, evaluation_time: datetime) -> str:
    material = f"{host_id}|{evaluation_time.isoformat()}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:32]
    return f"advisor-live-{digest}"


class AdvisorOrchestrationService:
    """Evaluate the live host advisor through the existing bounded boundaries."""

    def __init__(
        self,
        config: AIPMConfig,
        *,
        clock: AdvisorClock = _default_clock,
        exporter: AdvisorSnapshotExporter = export_telemetry_snapshot,
        adapter: AdvisorAdapter = adapt_telemetry_snapshot,
        composer: AdvisorComposer = compose_advisor,
    ) -> None:
        if not isinstance(config, AIPMConfig):
            raise AdvisorOrchestrationError("Advisor orchestration requires AIPMConfig")
        if not callable(clock) or not callable(exporter) or not callable(adapter) or not callable(composer):
            raise AdvisorOrchestrationError("Advisor orchestration dependencies must be callable")
        self.config = config
        self.clock = clock
        self.exporter = exporter
        self.adapter = adapter
        self.composer = composer

    def evaluate(self) -> AdvisorResponse:
        """Run one bounded read-only evaluation with a service-owned context."""

        if not self.config.telemetry.enabled:
            raise AdvisorOrchestrationError("Advisor telemetry is unavailable")
        evaluation_time = _utc(self.clock())
        window_end = evaluation_time
        window_start = evaluation_time - timedelta(seconds=INITIAL_HISTORY_WINDOW_SECONDS)
        request_id = _request_id(self.config.host_id, evaluation_time)
        snapshot = self.exporter(
            self.config,
            evaluation_time=evaluation_time,
            window_start=window_start,
            window_end=window_end,
        )
        if not isinstance(snapshot, TelemetrySnapshotExport):
            raise AdvisorOrchestrationError("Telemetry export returned an invalid snapshot")
        request = self.adapter(
            snapshot,
            request_id=request_id,
            evaluation_time=evaluation_time,
            scope=AdvisorScope.HOST,
        )
        if not isinstance(request, AdvisorCompositionRequest):
            raise AdvisorOrchestrationError("Telemetry adapter returned an invalid advisor request")
        response = self.composer(request)
        if not isinstance(response, AdvisorResponse):
            raise AdvisorOrchestrationError("Advisor composition returned an invalid response")
        return response


__all__ = ["AdvisorOrchestrationError", "AdvisorOrchestrationService"]
