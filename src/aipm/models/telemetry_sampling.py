from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from aipm.models.telemetry import ContainerSnapshot, TelemetryError, FreshnessStatus, TelemetryFreshness




class SamplingMode(StrEnum):
    SPLIT = "split"
    LEGACY = "legacy"



@dataclass(frozen=True, slots=True)
class ResourceRefreshResult:
    sampled_at: datetime
    duration_ms: int
    status: str
    containers: tuple[ContainerSnapshot, ...]
    error: TelemetryError | None = None


@dataclass(frozen=True, slots=True)
class RetentionCleanupResult:
    deleted_rows: int
    duration_ms: int
    error: TelemetryError | None = None


@dataclass(frozen=True, slots=True)
class SlowTaskState:
    name: str
    running: bool = False
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_duration_ms: int | None = None
    last_status: str = "never_sampled"
    skipped_count: int = 0
    error: TelemetryError | None = None
