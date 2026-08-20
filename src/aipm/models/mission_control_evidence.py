from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from aipm.models.mission_control import ObservationState
from aipm.models.pagination import KeysetCursor


class ComparisonStatus(StrEnum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    MISSING_BASELINE = "missing_baseline"
    MISSING_CURRENT = "missing_current"
    UNAVAILABLE_BASELINE = "unavailable_baseline"
    UNAVAILABLE_CURRENT = "unavailable_current"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class RelatedResourceLink:
    kind: str
    identifier: str
    label: str | None = None
    route: str | None = None


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    entry_id: int
    occurred_at: datetime
    transition: str
    previous_status: str | None
    current_status: str
    previous_severity: str | None
    current_severity: str
    event_id: int | None
    source_event_key: str | None
    resource: dict[str, Any]
    title: str
    summary: str
    links: tuple[RelatedResourceLink, ...] = ()


@dataclass(frozen=True, slots=True)
class TimelinePage:
    available: bool
    status: str
    error: str | None
    observation: ObservationState
    entries: tuple[TimelineEntry, ...] = ()
    next_cursor: KeysetCursor | None = None
    partial: bool = False


@dataclass(frozen=True, slots=True)
class HistoricalPoint:
    point: Any
    run_id: int | None


@dataclass(frozen=True, slots=True)
class ComparisonSide:
    available: bool
    status: str
    observed_at: datetime | None
    run_id: int | None
    value: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ComparisonField:
    name: str
    status: ComparisonStatus
    before: Any = None
    after: Any = None
    delta: Any = None


@dataclass(frozen=True, slots=True)
class HistoryComparison:
    available: bool
    status: str
    error: str | None
    resource_type: str
    resource_id: str | None
    baseline: ComparisonSide
    current: ComparisonSide
    changes: tuple[ComparisonField, ...] = ()
    links: tuple[RelatedResourceLink, ...] = ()
