"""Telemetry-owned, bounded read snapshots for future advisor consumers.

The export boundary owns the SQLite connection and transaction. Consumers receive
only immutable typed records; they never receive a connection, path, cursor, SQL,
or transaction handle.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from aipm.core.exceptions import AIPMError
from aipm.models.config import AIPMConfig
from aipm.models.history import HostHistoryPoint


MAX_SNAPSHOT_PARENT_ROWS = 128
MAX_SNAPSHOT_CHILD_ROWS = 512
MAX_SNAPSHOT_SECONDS = 5.0
INITIAL_HISTORY_WINDOW_SECONDS = 300.0
RESOURCE_METRICS = ("cpu_percent", "memory_percent", "disk_percent")
_PERCENT_FIELDS = RESOURCE_METRICS
_NONNEGATIVE_FIELDS = (
    "load_one",
    "load_five",
    "load_fifteen",
    "memory_total_gb",
    "memory_used_gb",
    "memory_available_gb",
    "swap_total_gb",
    "swap_used_gb",
    "disk_total_gb",
    "disk_used_gb",
    "disk_free_gb",
)
_INTEGER_FIELDS = ("network_interfaces", "network_established")
_HOST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,63}$")


class TelemetrySnapshotError(AIPMError):
    """Raised when a bounded telemetry snapshot cannot be exported."""


class SnapshotCompleteness(str, Enum):
    """Conservative source-completeness state, not an advisor status."""

    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class TelemetryMetricCompleteness:
    """Completeness evidence for one initial host-resource metric."""

    metric: str
    status: SnapshotCompleteness
    point_count: int
    coverage_seconds: float
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class TelemetrySnapshotParent:
    """Minimal Phase 4D parent context projection."""

    id: int
    sampled_at: datetime
    host_available: bool

    def __post_init__(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, int) or self.id < 0:
            raise TelemetrySnapshotError("Invalid exported parent identity")
        if not isinstance(self.sampled_at, datetime) or self.sampled_at.tzinfo is None or self.sampled_at.utcoffset() is None:
            raise TelemetrySnapshotError("Invalid exported parent timestamp")
        if not isinstance(self.host_available, bool):
            raise TelemetrySnapshotError("Invalid exported host availability")
        object.__setattr__(self, "sampled_at", self.sampled_at.astimezone(timezone.utc))


@dataclass(frozen=True, slots=True)
class TelemetrySnapshotExport:
    """Immutable bounded payload returned to a future advisor consumer."""

    host_id: str
    evaluation_time: datetime
    window_start: datetime
    window_end: datetime
    cadence_seconds: float
    sample_runs: tuple[TelemetrySnapshotParent, ...]
    host_samples: tuple[HostHistoryPoint, ...]
    metric_completeness: tuple[TelemetryMetricCompleteness, ...]
    completeness: SnapshotCompleteness
    invalid_source_rows: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.host_id, str) or _HOST_ID_RE.fullmatch(self.host_id) is None:
            raise TelemetrySnapshotError("Invalid exported host identity")
        for name in ("evaluation_time", "window_start", "window_end"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise TelemetrySnapshotError(f"Exported {name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        if self.window_start >= self.window_end or self.window_end > self.evaluation_time:
            raise TelemetrySnapshotError("Exported snapshot window is not ordered")
        if isinstance(self.cadence_seconds, bool) or not isinstance(self.cadence_seconds, (int, float)):
            raise TelemetrySnapshotError("Invalid exported cadence")
        cadence = float(self.cadence_seconds)
        if not math.isfinite(cadence) or cadence <= 0:
            raise TelemetrySnapshotError("Invalid exported cadence")
        object.__setattr__(self, "cadence_seconds", cadence)
        if not isinstance(self.sample_runs, tuple) or not isinstance(self.host_samples, tuple):
            raise TelemetrySnapshotError("Exported records must be immutable tuples")
        if any(not isinstance(item, TelemetrySnapshotParent) for item in self.sample_runs):
            raise TelemetrySnapshotError("Exported parent records must be typed")
        if any(not isinstance(item, HostHistoryPoint) for item in self.host_samples):
            raise TelemetrySnapshotError("Exported host records must be typed")
        if not isinstance(self.metric_completeness, tuple) or tuple(item.metric for item in self.metric_completeness) != RESOURCE_METRICS:
            raise TelemetrySnapshotError("Exported metric completeness is not canonical")
        if any(not isinstance(item, TelemetryMetricCompleteness) for item in self.metric_completeness):
            raise TelemetrySnapshotError("Exported metric completeness must be typed")
        if not isinstance(self.completeness, SnapshotCompleteness):
            raise TelemetrySnapshotError("Invalid exported completeness")
        if isinstance(self.invalid_source_rows, bool) or not isinstance(self.invalid_source_rows, int) or self.invalid_source_rows < 0:
            raise TelemetrySnapshotError("Invalid exported source-row count")

    @property
    def complete(self) -> bool:
        """Whether all initial metrics have sufficient continuous evidence."""

        return self.completeness is SnapshotCompleteness.SUFFICIENT

    def canonical(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "evaluation_time": self.evaluation_time.isoformat(),
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "cadence_seconds": self.cadence_seconds,
            "sample_runs": [
                {
                    "id": run.id,
                    "sampled_at": run.sampled_at.isoformat(),
                    "host_available": run.host_available,
                }
                for run in self.sample_runs
            ],
            "host_samples": [_host_canonical(sample) for sample in self.host_samples],
            "metric_completeness": [
                {
                    "metric": item.metric,
                    "status": item.status.value,
                    "point_count": item.point_count,
                    "coverage_seconds": item.coverage_seconds,
                    "reason": item.reason,
                }
                for item in self.metric_completeness
            ],
            "completeness": self.completeness.value,
            "invalid_source_rows": self.invalid_source_rows,
        }

    @property
    def stable_id(self) -> str:
        encoded = json.dumps(self.canonical(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return f"telemetry-snapshot-{hashlib.sha256(encoded).hexdigest()[:32]}"


class _SQLiteTelemetryReadSnapshot(AbstractContextManager["_SQLiteTelemetryReadSnapshot"]):
    """One bounded read-only transaction owned by the telemetry boundary."""

    def __init__(self, database_path: str | Path, connection: sqlite3.Connection, monotonic: Callable[[], float]):
        self.database_path = Path(database_path).expanduser()
        self._connection = connection
        self._closed = False
        self._monotonic = monotonic
        self._deadline = monotonic() + MAX_SNAPSHOT_SECONDS

    @classmethod
    def _open_owned(cls, database_path: str | Path, *, monotonic: Callable[[], float] | None = None) -> "_SQLiteTelemetryReadSnapshot":
        """Open inside the telemetry owner boundary; never expose this to consumers."""
        path = Path(database_path).expanduser()
        if monotonic is None:
            monotonic = time.monotonic
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise TelemetrySnapshotError("Telemetry snapshot query-only mode was not enabled")
            connection.execute("BEGIN DEFERRED")
            snapshot = cls(path, connection, monotonic)
            snapshot._check_budget()
            return snapshot
        except TelemetrySnapshotError:
            if connection is not None:
                _close_connection(connection)
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                _close_connection(connection)
            raise TelemetrySnapshotError("Unable to open telemetry-owned snapshot") from exc

    @property
    def query_only_enabled(self) -> bool:
        self._ensure_open()
        self._check_budget()
        return int(self._connection.execute("PRAGMA query_only").fetchone()[0]) == 1

    def read_sample_runs(self, start: datetime, end: datetime, limit: int) -> tuple[TelemetrySnapshotParent, ...]:
        self._ensure_open()
        self._check_budget()
        start_ts, end_ts = _window(start, end)
        _limit(limit, MAX_SNAPSHOT_PARENT_ROWS)
        rows = self._connection.execute(
            """
            SELECT id, sampled_at, host_available
            FROM sample_runs
            WHERE sampled_at BETWEEN ? AND ?
            ORDER BY sampled_at, id
            LIMIT ?
            """,
            (start_ts, end_ts, limit),
        ).fetchall()
        self._check_budget()
        return tuple(_decode_run(row) for row in rows)

    def read_host_samples(self, run_ids: Sequence[int], limit: int) -> tuple[HostHistoryPoint, ...]:
        self._ensure_open()
        self._check_budget()
        _limit(limit, MAX_SNAPSHOT_CHILD_ROWS)
        normalized_ids = tuple(int(run_id) for run_id in run_ids)
        if len(normalized_ids) > MAX_SNAPSHOT_PARENT_ROWS:
            raise TelemetrySnapshotError("Telemetry snapshot run selection exceeds the bound")
        if not normalized_ids:
            return ()
        placeholders = ", ".join("?" for _ in normalized_ids)
        rows = self._connection.execute(
            f"""
            SELECT sampled_at, hostname, cpu_percent, load_one, load_five,
                   load_fifteen, memory_total_gb, memory_used_gb,
                   memory_available_gb, memory_percent, swap_total_gb,
                   swap_used_gb, swap_percent, disk_total_gb, disk_used_gb,
                   disk_free_gb, disk_percent, network_interfaces,
                   network_established, available
            FROM host_samples
            WHERE run_id IN ({placeholders})
            ORDER BY sampled_at, id
            LIMIT ?
            """,
            (*normalized_ids, limit),
        ).fetchall()
        self._check_budget()
        try:
            return tuple(_decode_host_sample(row) for row in rows)
        except (TypeError, ValueError, OverflowError) as exc:
            raise TelemetrySnapshotError("Telemetry snapshot contains an invalid host row") from exc

    def export(
        self,
        *,
        host_id: str,
        evaluation_time: datetime,
        window_start: datetime,
        window_end: datetime,
        cadence_seconds: float,
        parent_limit: int = MAX_SNAPSHOT_PARENT_ROWS,
        child_limit: int = MAX_SNAPSHOT_CHILD_ROWS,
    ) -> TelemetrySnapshotExport:
        """Read bounded parent/child records and return only immutable typed data."""

        self._ensure_open()
        _validate_host_id(host_id)
        evaluation_utc = _aware_utc(evaluation_time, "evaluation_time")
        window_start_utc = _aware_utc(window_start, "window_start")
        window_end_utc = _aware_utc(window_end, "window_end")
        if window_start_utc >= window_end_utc or window_end_utc > evaluation_utc:
            raise TelemetrySnapshotError("Telemetry snapshot window is not ordered")
        if (window_end_utc - window_start_utc).total_seconds() != INITIAL_HISTORY_WINDOW_SECONDS:
            raise TelemetrySnapshotError("Telemetry snapshot window must be five minutes")
        cadence = _validate_cadence(cadence_seconds)
        _limit(parent_limit, MAX_SNAPSHOT_PARENT_ROWS)
        _limit(child_limit, MAX_SNAPSHOT_CHILD_ROWS)
        self._check_budget()

        start_ts, end_ts = _window(window_start_utc, window_end_utc)
        run_rows = self._connection.execute(
            """
            SELECT id, sampled_at, host_available
            FROM sample_runs
            WHERE sampled_at BETWEEN ? AND ?
            ORDER BY sampled_at, id
            LIMIT ?
            """,
            (start_ts, end_ts, parent_limit),
        ).fetchall()
        runs = tuple(_decode_run(row) for row in run_rows)
        run_ids = tuple(run.id for run in runs)
        self._check_budget()

        host_rows = ()
        if run_ids:
            placeholders = ", ".join("?" for _ in run_ids)
            host_rows = self._connection.execute(
                f"""
                SELECT sampled_at, hostname, cpu_percent, load_one, load_five,
                       load_fifteen, memory_total_gb, memory_used_gb,
                       memory_available_gb, memory_percent, swap_total_gb,
                       swap_used_gb, swap_percent, disk_total_gb, disk_used_gb,
                       disk_free_gb, disk_percent, network_interfaces,
                       network_established, available
                FROM host_samples
                WHERE run_id IN ({placeholders})
                ORDER BY sampled_at, id
                LIMIT ?
                """,
                (*run_ids, child_limit),
            ).fetchall()
        self._check_budget()

        host_samples: list[HostHistoryPoint] = []
        invalid_rows = 0
        for row in host_rows:
            try:
                host_samples.append(_decode_host_sample(row))
            except ValueError:
                invalid_rows += 1
        completeness = tuple(_metric_status(metric, host_samples, cadence) for metric in RESOURCE_METRICS)
        if invalid_rows:
            overall = SnapshotCompleteness.INVALID
        elif all(item.status is SnapshotCompleteness.SUFFICIENT for item in completeness):
            overall = SnapshotCompleteness.SUFFICIENT
        else:
            overall = SnapshotCompleteness.INSUFFICIENT
        self._check_budget()
        return TelemetrySnapshotExport(
            host_id=host_id,
            evaluation_time=evaluation_utc,
            window_start=window_start_utc,
            window_end=window_end_utc,
            cadence_seconds=cadence,
            sample_runs=runs,
            host_samples=tuple(host_samples),
            metric_completeness=completeness,
            completeness=overall,
            invalid_source_rows=invalid_rows,
        )

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._connection.rollback()
        finally:
            self._connection.close()
            self._closed = True

    def _check_budget(self) -> None:
        if self._monotonic() > self._deadline:
            raise TelemetrySnapshotError("Telemetry snapshot exceeded its lifetime bound")

    def _ensure_open(self) -> None:
        if self._closed:
            raise TelemetrySnapshotError("Telemetry snapshot is closed")

    def __enter__(self) -> "_SQLiteTelemetryReadSnapshot":
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def export_telemetry_snapshot(
    config: AIPMConfig,
    *,
    evaluation_time: datetime,
    window_start: datetime,
    window_end: datetime,
    parent_limit: int = MAX_SNAPSHOT_PARENT_ROWS,
    child_limit: int = MAX_SNAPSHOT_CHILD_ROWS,
    monotonic: Callable[[], float] | None = None,
) -> TelemetrySnapshotExport:
    """Export a bounded snapshot without exposing SQLite or filesystem handles."""

    if not isinstance(config, AIPMConfig):
        raise TelemetrySnapshotError("Telemetry snapshot export requires AIPMConfig")
    if monotonic is None:
        monotonic = time.monotonic
    if not callable(monotonic):
        raise TelemetrySnapshotError("Telemetry snapshot export requires a monotonic clock")
    with _SQLiteTelemetryReadSnapshot._open_owned(config.telemetry.database_path, monotonic=monotonic) as snapshot:

        return snapshot.export(
            host_id=config.host_id,
            evaluation_time=evaluation_time,
            window_start=window_start,
            window_end=window_end,
            cadence_seconds=config.telemetry.interval_seconds,
            parent_limit=parent_limit,
            child_limit=child_limit,
        )


def _window(start: datetime, end: datetime) -> tuple[int, int]:
    start_utc = _aware_utc(start, "start")
    end_utc = _aware_utc(end, "end")
    if start_utc > end_utc:
        raise TelemetrySnapshotError("Telemetry snapshot window must be ordered")
    return int(start_utc.timestamp()), int(end_utc.timestamp())


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TelemetrySnapshotError(f"Telemetry snapshot {name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_cadence(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TelemetrySnapshotError("Invalid telemetry snapshot cadence")
    cadence = float(value)
    if not math.isfinite(cadence) or cadence <= 0 or cadence > 86_400:
        raise TelemetrySnapshotError("Invalid telemetry snapshot cadence")
    return cadence


def _validate_host_id(value: str) -> None:
    if not isinstance(value, str) or _HOST_ID_RE.fullmatch(value) is None:
        raise TelemetrySnapshotError("Invalid telemetry snapshot host identity")


def _limit(value: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > maximum:
        raise TelemetrySnapshotError("Telemetry snapshot limit is outside the bound")


def _decode_run(row: sqlite3.Row) -> TelemetrySnapshotParent:

    try:
        return TelemetrySnapshotParent(
            id=int(row["id"]),
            sampled_at=_from_timestamp(row["sampled_at"]),
            host_available=_strict_bool(row["host_available"]),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise TelemetrySnapshotError("Telemetry snapshot contains an invalid parent row") from exc


def _decode_host_sample(row: sqlite3.Row) -> HostHistoryPoint:
    try:
        available = _strict_bool(row["available"])
        values: dict[str, object] = {}
        for name in _PERCENT_FIELDS:
            values[name] = _optional_bounded_number(row[name], 0.0, 100.0)
        for name in _NONNEGATIVE_FIELDS:
            values[name] = _optional_bounded_number(row[name], 0.0, None)
        for name in _INTEGER_FIELDS:
            values[name] = _optional_bounded_integer(row[name])
        hostname = row["hostname"]
        if hostname is not None and (not isinstance(hostname, str) or len(hostname) > 256):
            raise ValueError("invalid hostname")
        return HostHistoryPoint(
            sampled_at=_from_timestamp(row["sampled_at"]),
            hostname=hostname,
            cpu_percent=values["cpu_percent"],
            load_one=values["load_one"],
            load_five=values["load_five"],
            load_fifteen=values["load_fifteen"],
            memory_total_gb=values["memory_total_gb"],
            memory_used_gb=values["memory_used_gb"],
            memory_available_gb=values["memory_available_gb"],
            memory_percent=values["memory_percent"],
            swap_total_gb=values["swap_total_gb"],
            swap_used_gb=values["swap_used_gb"],
            swap_percent=_optional_bounded_number(row["swap_percent"], 0.0, 100.0),
            disk_total_gb=values["disk_total_gb"],
            disk_used_gb=values["disk_used_gb"],
            disk_free_gb=values["disk_free_gb"],
            disk_percent=values["disk_percent"],
            network_interfaces=values["network_interfaces"],
            network_established=values["network_established"],
            available=available,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid host row") from exc


def _strict_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError("invalid boolean")


def _bounded_text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError("invalid bounded text")
    return value


def _optional_bounded_number(value: object, minimum: float, maximum: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid number")
    number = float(value)
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
        raise ValueError("number outside bounds")
    return number


def _optional_bounded_integer(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid integer")
    return int(value)


def _metric_status(metric: str, samples: Sequence[HostHistoryPoint], cadence: float) -> TelemetryMetricCompleteness:
    points = []
    for sample in samples:
        value = getattr(sample, metric)
        if sample.available and value is not None:
            points.append((sample.sampled_at, float(value)))
    points.sort(key=lambda item: item[0])
    if len(points) != len({observed_at for observed_at, _ in points}):
        return TelemetryMetricCompleteness(metric, SnapshotCompleteness.INVALID, len(points), 0.0, "duplicate_timestamp")
    if len(points) < 3:
        return TelemetryMetricCompleteness(metric, SnapshotCompleteness.INSUFFICIENT, len(points), _coverage(points), "insufficient_points")
    coverage = _coverage(points)
    if coverage < INITIAL_HISTORY_WINDOW_SECONDS:
        return TelemetryMetricCompleteness(metric, SnapshotCompleteness.INSUFFICIENT, len(points), coverage, "insufficient_coverage")
    maximum_gap = max(((b - a).total_seconds() for (a, _), (b, _) in zip(points, points[1:])), default=0.0)
    if maximum_gap > cadence * 1.5:
        return TelemetryMetricCompleteness(metric, SnapshotCompleteness.INSUFFICIENT, len(points), coverage, "gap_exceeds_cadence")
    return TelemetryMetricCompleteness(metric, SnapshotCompleteness.SUFFICIENT, len(points), coverage)


def _coverage(points: Sequence[tuple[datetime, float]]) -> float:
    if len(points) < 2:
        return 0.0
    return (points[-1][0] - points[0][0]).total_seconds()


def _from_timestamp(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError("invalid timestamp")
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _host_canonical(sample: HostHistoryPoint) -> dict[str, Any]:
    return {
        "sampled_at": sample.sampled_at.isoformat(),
        "hostname": sample.hostname,
        "cpu_percent": sample.cpu_percent,
        "load_one": sample.load_one,
        "load_five": sample.load_five,
        "load_fifteen": sample.load_fifteen,
        "memory_total_gb": sample.memory_total_gb,
        "memory_used_gb": sample.memory_used_gb,
        "memory_available_gb": sample.memory_available_gb,
        "memory_percent": sample.memory_percent,
        "swap_total_gb": sample.swap_total_gb,
        "swap_used_gb": sample.swap_used_gb,
        "swap_percent": sample.swap_percent,
        "disk_total_gb": sample.disk_total_gb,
        "disk_used_gb": sample.disk_used_gb,
        "disk_free_gb": sample.disk_free_gb,
        "disk_percent": sample.disk_percent,
        "network_interfaces": sample.network_interfaces,
        "network_established": sample.network_established,
        "available": sample.available,
    }


def _close_connection(connection: sqlite3.Connection) -> None:
    try:
        connection.rollback()
    except sqlite3.Error:
        pass
    try:
        connection.close()
    except sqlite3.Error:
        pass


__all__ = [
    "INITIAL_HISTORY_WINDOW_SECONDS",
    "MAX_SNAPSHOT_CHILD_ROWS",
    "MAX_SNAPSHOT_PARENT_ROWS",
    "MAX_SNAPSHOT_SECONDS",
    "SnapshotCompleteness",
    "TelemetryMetricCompleteness",
    "TelemetrySnapshotParent",
    "TelemetrySnapshotError",
    "TelemetrySnapshotExport",
    "export_telemetry_snapshot",
]
