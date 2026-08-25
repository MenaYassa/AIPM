"""Pure Phase 4D adapter from telemetry export to canonical advisor input.

The adapter consumes only the immutable telemetry-owned export. It does not open
SQLite, inspect filesystems, read clocks, collect observations, or evaluate rules.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from aipm.models.advisor import AdvisorScope, EvidenceState
from aipm.repositories.telemetry.read_snapshot import (
    RESOURCE_METRICS,
    SnapshotCompleteness,
    TelemetryMetricCompleteness,
    TelemetrySnapshotExport,
)
from aipm.services.advisor.composition import AdvisorCompositionRequest
from aipm.services.advisor.rules import ResourceHistoryEnvelope, ResourceHistoryPoint


class TelemetryObservationAdapterError(ValueError):
    """Raised when an exported telemetry slice cannot be mapped safely."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:32]


def _evidence_id(snapshot: TelemetrySnapshotExport, metric: str, sample: Any, state: EvidenceState, value: float | None) -> str:
    identity = {
        "snapshot_id": snapshot.stable_id,
        "metric": metric,
        "sampled_at": sample.sampled_at.isoformat() if sample is not None else None,
        "state": state.value,
        "value": value,
    }
    return f"telemetry-history-{_digest(identity)}"


def _metric_metadata(snapshot: TelemetrySnapshotExport) -> Mapping[str, TelemetryMetricCompleteness]:
    return {item.metric: item for item in snapshot.metric_completeness}


def _valid_percent(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 100.0
    )


def _valid_sampled_at(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _observation(
    snapshot: TelemetrySnapshotExport,
    *,
    metric: str,
    sample: Any,
    state: EvidenceState,
    value: float | None = None,
) -> dict[str, Any]:
    observed_at = sample.sampled_at if sample is not None else None
    if observed_at is not None and not _valid_sampled_at(observed_at):
        raise TelemetryObservationAdapterError("Telemetry sample timestamp is not timezone-aware")
    evidence_id = _evidence_id(snapshot, metric, sample, state, value)
    fields: dict[str, Any] = {"metric": metric, "unit": "percent"}
    if state is EvidenceState.OBSERVED:
        if value is None:
            raise TelemetryObservationAdapterError("Observed telemetry value is missing")
        fields["value"] = float(value)
    elif state is EvidenceState.INVALID:
        fields = {}
    return {
        "evidence_id": evidence_id,
        "source_id": "history",
        "resource_type": "host",
        "resource_id": snapshot.host_id,
        "state": state.value,
        "observed_at": observed_at,
        "fields": fields,
    }


def _history_envelope(
    snapshot: TelemetrySnapshotExport,
    *,
    metric: str,
    metadata: TelemetryMetricCompleteness,
    samples: tuple[Any, ...],
    evidence_by_sample: Mapping[tuple[str, datetime], str],
) -> ResourceHistoryEnvelope:
    points: list[ResourceHistoryPoint] = []
    for sample in samples:
        value = getattr(sample, metric)
        if not sample.available or not _valid_percent(value):
            continue
        evidence_id = evidence_by_sample[(metric, sample.sampled_at)]
        points.append(
            ResourceHistoryPoint(
                evidence_id=evidence_id,
                observed_at=sample.sampled_at,
                metric=metric,
                value=float(value),
                unit="percent",
                state=EvidenceState.OBSERVED,
            )
        )
    return ResourceHistoryEnvelope(
        resource_id=snapshot.host_id,
        metric=metric,
        unit="percent",
        cadence_seconds=snapshot.cadence_seconds,
        window_start=snapshot.window_start,
        window_end=snapshot.window_end,
        complete=(metadata.status is SnapshotCompleteness.SUFFICIENT and snapshot.invalid_source_rows == 0),
        points=tuple(points),
    )


def adapt_telemetry_snapshot(
    snapshot: TelemetrySnapshotExport,
    *,
    request_id: str,
    evaluation_time: datetime,
    scope: AdvisorScope = AdvisorScope.HOST,
) -> AdvisorCompositionRequest:
    """Map the private-VPS host resource export into canonical advisor input.

    This function intentionally stops at ``AdvisorCompositionRequest``. The
    existing Phase 4A caller remains responsible for composition and evaluation.
    """

    if not isinstance(snapshot, TelemetrySnapshotExport):
        raise TelemetryObservationAdapterError("Adapter requires TelemetrySnapshotExport")
    if scope is not AdvisorScope.HOST:
        raise TelemetryObservationAdapterError("Telemetry adapter supports host scope only")
    if not isinstance(evaluation_time, datetime) or evaluation_time.tzinfo is None or evaluation_time.utcoffset() is None:
        raise TelemetryObservationAdapterError("Adapter evaluation_time must be timezone-aware")
    if evaluation_time != snapshot.evaluation_time:
        raise TelemetryObservationAdapterError("Adapter evaluation_time must match the export evaluation_time")
    if len(snapshot.sample_runs) != 0 and len(snapshot.host_samples) > 0 and len(snapshot.host_samples) > len(snapshot.sample_runs):
        raise TelemetryObservationAdapterError("Telemetry host samples exceed exported parent context")

    metadata_by_metric = _metric_metadata(snapshot)
    if tuple(metadata_by_metric) != RESOURCE_METRICS:
        raise TelemetryObservationAdapterError("Telemetry metric completeness is not canonical")

    samples = tuple(snapshot.host_samples)
    observations: list[dict[str, Any]] = []
    evidence_by_sample: dict[tuple[str, datetime], str] = {}
    for metric in RESOURCE_METRICS:
        emitted = False
        for sample in samples:
            value = getattr(sample, metric)
            if not _valid_sampled_at(sample.sampled_at):
                raise TelemetryObservationAdapterError("Telemetry sample timestamp is not timezone-aware")
            if sample.available and _valid_percent(value):
                state = EvidenceState.OBSERVED
                numeric_value = float(value)
            elif sample.available and value is not None:
                state = EvidenceState.INVALID
                numeric_value = None
            else:
                state = EvidenceState.UNAVAILABLE
                numeric_value = None
            record = _observation(snapshot, metric=metric, sample=sample, state=state, value=numeric_value)
            observations.append(record)
            if state is EvidenceState.OBSERVED:
                evidence_by_sample[(metric, sample.sampled_at)] = record["evidence_id"]
            emitted = True
        if not emitted:
            observations.append(_observation(snapshot, metric=metric, sample=None, state=EvidenceState.NOT_OBSERVED))

    envelopes = tuple(
        _history_envelope(
            snapshot,
            metric=metric,
            metadata=metadata_by_metric[metric],
            samples=samples,
            evidence_by_sample=evidence_by_sample,
        )
        for metric in RESOURCE_METRICS
    )
    expected_count = max(1, len(snapshot.sample_runs)) * len(RESOURCE_METRICS)
    return AdvisorCompositionRequest(
        request_id=request_id,
        evaluation_time=evaluation_time,
        observations=tuple(observations),
        scope=scope,
        expected_sources={"history": expected_count},
        history_envelopes=envelopes,
    )


__all__ = ["TelemetryObservationAdapterError", "adapt_telemetry_snapshot"]
