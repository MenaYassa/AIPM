from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest
import sqlite3

from aipm.models.config import AIPMConfig, TelemetryConfig
from aipm.models.history import HistoricalRun, HostHistoryPoint, SampleRunRecord
from aipm.models.advisor import UncertaintyKind
from aipm.repositories.telemetry.read_snapshot import (
    SnapshotCompleteness,
    TelemetrySnapshotError,
    TelemetrySnapshotParent,
    export_telemetry_snapshot,
)
from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository
from aipm.services.advisor.normalizer import normalize_observations
from aipm.services.advisor.rules import AdvisorRuleEngine, ResourceHistoryEnvelope, ResourceHistoryPoint


UTC = timezone.utc
EVALUATION_TIME = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _seed(path, at=EVALUATION_TIME - timedelta(minutes=5)):
    repository = SQLiteHistoryRepository(path)
    run = SampleRunRecord(at, True, False, False, "unknown", duration_ms=4)
    host = HostHistoryPoint(
        at,
        "runtime-hostname",
        85.0,
        1.0,
        0.5,
        0.25,
        4.0,
        1.0,
        3.0,
        25.0,
        1.0,
        0.1,
        10.0,
        20.0,
        5.0,
        15.0,
        25.0,
        1,
        2,
        True,
    )
    return repository.save_sample(run, host, (), (), None)



def test_package_exposes_only_owner_export_and_immutable_payload_types():
    import aipm.repositories.telemetry as telemetry

    assert hasattr(telemetry, "export_telemetry_snapshot")
    assert hasattr(telemetry, "TelemetrySnapshotExport")
    assert not hasattr(telemetry, "SQLiteTelemetryReadSnapshot")
    assert "SQLiteTelemetryReadSnapshot" not in telemetry.__all__


def _history_envelope(*, complete, offsets=(0, 150, 300), window_start=EVALUATION_TIME - timedelta(minutes=5), window_end=EVALUATION_TIME, cadence_seconds=100):
    points = tuple(
        ResourceHistoryPoint(
            evidence_id=f"history-{index}",
            observed_at=window_start + timedelta(seconds=offset),
            metric="cpu_percent",
            value=85.0,
            unit="percent",
        )
        for index, offset in enumerate(offsets)
    )
    return ResourceHistoryEnvelope(
        resource_id="agent",
        metric="cpu_percent",
        unit="percent",
        cadence_seconds=cadence_seconds,
        window_start=window_start,
        window_end=window_end,
        complete=complete,
        points=points,
    )


def _evaluate_history(envelope):
    records = [
        {
            "evidence_id": point.evidence_id,
            "source_id": "history",
            "resource_type": "host",
            "resource_id": "agent",
            "state": "observed",
            "observed_at": point.observed_at,
            "fields": {"metric": "cpu_percent", "value": point.value, "unit": "percent"},
        }
        for point in envelope.points
    ]
    bundle = normalize_observations(records, evaluation_time=EVALUATION_TIME)
    return AdvisorRuleEngine().evaluate(
        bundle,
        request_id="phase4d-completeness",
        evaluation_time=EVALUATION_TIME,
        history_envelopes=(envelope,),
    )


def test_phase3_completeness_contract_allows_positive_claim_only_for_complete_snapshot_history():
    complete_response = _evaluate_history(_history_envelope(complete=True))
    incomplete_response = _evaluate_history(_history_envelope(complete=False))
    assert any(finding.rule_id == "resource.pressure.sustained" for finding in complete_response.findings)
    assert not any(finding.rule_id == "resource.pressure.sustained" for finding in incomplete_response.findings)
    assert any(uncertainty.kind is UncertaintyKind.MISSING_EVIDENCE for uncertainty in incomplete_response.uncertainties)


def test_phase3_completeness_contract_rejects_history_beyond_configured_gap():
    envelope = _history_envelope(
        complete=True,
        offsets=(0, 150, 600),
        window_start=EVALUATION_TIME - timedelta(minutes=10),
    )
    response = _evaluate_history(envelope)
    assert not any(finding.rule_id == "resource.pressure.sustained" for finding in response.findings)
    assert any(uncertainty.kind is UncertaintyKind.MISSING_EVIDENCE for uncertainty in response.uncertainties)


def _seed_export_history(path):
    repository = SQLiteHistoryRepository(path)
    window_start = EVALUATION_TIME - timedelta(minutes=5)
    for offset in (0, 150, 300):
        at = window_start + timedelta(seconds=offset)
        _seed(path, at=at)
    return window_start


def _export_config(path):
    return AIPMConfig(
        telemetry=TelemetryConfig(interval_seconds=150, database_path=str(path)),
        host_id="agent",
    )


def test_phase4d_parent_projection_excludes_prohibited_history_fields_and_preserves_historical_run():
    assert tuple(field.name for field in fields(HistoricalRun)) == (
        "id",
        "sampled_at",
        "host_available",
        "docker_available",
        "projects_available",
        "tunnel_state",
    )
    assert tuple(field.name for field in fields(TelemetrySnapshotParent)) == (
        "id",
        "sampled_at",
        "host_available",
    )


def test_owner_export_returns_bounded_immutable_payload_with_configured_identity_and_cadence(tmp_path):
    path = tmp_path / "telemetry.db"
    window_start = _seed_export_history(path)
    evaluation_time = EVALUATION_TIME
    payload = export_telemetry_snapshot(
        _export_config(path),
        evaluation_time=evaluation_time,
        window_start=window_start,
        window_end=evaluation_time,
        parent_limit=10,
        child_limit=10,
        monotonic=lambda: 1.0,
    )
    assert payload.host_id == "agent"
    assert payload.evaluation_time == evaluation_time
    assert payload.cadence_seconds == 150.0
    assert isinstance(payload.sample_runs, tuple)
    assert isinstance(payload.host_samples, tuple)
    assert len(payload.sample_runs) == 3
    assert len(payload.host_samples) == 3
    assert all(isinstance(parent, TelemetrySnapshotParent) for parent in payload.sample_runs)
    assert all(not hasattr(parent, "docker_available") for parent in payload.sample_runs)
    assert all(not hasattr(parent, "projects_available") for parent in payload.sample_runs)
    assert all(not hasattr(parent, "tunnel_state") for parent in payload.sample_runs)
    canonical_text = str(payload.canonical())
    assert "docker_available" not in canonical_text
    assert "projects_available" not in canonical_text
    assert "tunnel_state" not in canonical_text
    assert payload.completeness is SnapshotCompleteness.SUFFICIENT
    assert all(item.status is SnapshotCompleteness.SUFFICIENT for item in payload.metric_completeness)
    assert not hasattr(payload, "_connection")
    assert not hasattr(payload, "database_path")
    assert payload.stable_id == payload.stable_id
    with pytest.raises(AttributeError):
        payload.host_id = "other"


def test_owner_export_stable_identity_ignores_prohibited_parent_state(tmp_path):
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    first_window = _seed_export_history(first_path)
    second_window = _seed_export_history(second_path)
    # The source parent rows differ only in fields that are outside the Phase 4D projection.
    for path, values in ((first_path, (False, False, "unknown")), (second_path, (True, True, "connected"))):
        connection = sqlite3.connect(path)
        connection.execute(
            "UPDATE sample_runs SET docker_available = ?, projects_available = ?, tunnel_state = ?",
            values,
        )
        connection.commit()
        connection.close()
    kwargs = {
        "evaluation_time": EVALUATION_TIME,
        "window_start": first_window,
        "window_end": EVALUATION_TIME,
        "parent_limit": 10,
        "child_limit": 10,
        "monotonic": lambda: 1.0,
    }
    first = export_telemetry_snapshot(_export_config(first_path), **kwargs)
    second = export_telemetry_snapshot(
        _export_config(second_path),
        **{**kwargs, "window_start": second_window},
    )
    assert first.sample_runs == second.sample_runs
    assert first.canonical() == second.canonical()
    assert first.stable_id == second.stable_id


def test_owner_export_is_deterministic_and_enforces_bounded_limits(tmp_path):
    path = tmp_path / "telemetry.db"
    window_start = _seed_export_history(path)
    config = _export_config(path)
    kwargs = {
        "evaluation_time": EVALUATION_TIME,
        "window_start": window_start,
        "window_end": EVALUATION_TIME,
        "parent_limit": 2,
        "child_limit": 2,
        "monotonic": lambda: 1.0,
    }
    first = export_telemetry_snapshot(config, **kwargs)
    second = export_telemetry_snapshot(config, **kwargs)
    assert first.canonical() == second.canonical()
    assert first.stable_id == second.stable_id
    assert len(first.sample_runs) == 2
    assert len(first.host_samples) == 2
    assert first.completeness is SnapshotCompleteness.INSUFFICIENT


def test_owner_export_fails_closed_for_invalid_window_without_partial_payload(tmp_path):
    path = tmp_path / "telemetry.db"
    _seed_export_history(path)
    with pytest.raises(TelemetrySnapshotError):
        export_telemetry_snapshot(
            _export_config(path),
            evaluation_time=EVALUATION_TIME,
            window_start=EVALUATION_TIME,
            window_end=EVALUATION_TIME - timedelta(minutes=5),
            monotonic=lambda: 1.0,
        )


def test_owner_export_does_not_open_direct_read_only_filesystem_path(tmp_path):
    path = tmp_path / "telemetry.db"
    window_start = _seed_export_history(path)
    payload = export_telemetry_snapshot(
        _export_config(path),
        evaluation_time=EVALUATION_TIME,
        window_start=window_start,
        window_end=EVALUATION_TIME,
        monotonic=lambda: 1.0,
    )
    assert payload.sample_runs
