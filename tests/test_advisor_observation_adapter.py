import ast
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from aipm.models.advisor import AdvisorScope
from aipm.models.history import HostHistoryPoint
from aipm.repositories.telemetry.read_snapshot import (
    SnapshotCompleteness,
    TelemetryMetricCompleteness,
    TelemetrySnapshotExport,
    TelemetrySnapshotParent,
)
from aipm.services.advisor.composition import AdvisorCompositionRequest, CompositionError
from aipm.services.advisor.observation_adapter import (
    TelemetryObservationAdapterError,
    adapt_telemetry_snapshot,
)
from aipm.services.advisor.rules import ResourceHistoryEnvelope


UTC = timezone.utc
EVALUATION_TIME = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
WINDOW_START = EVALUATION_TIME - timedelta(minutes=5)


def _host_sample(at: datetime, *, available: bool = True, cpu: object = 85.0) -> HostHistoryPoint:
    return HostHistoryPoint(
        sampled_at=at,
        hostname="not-exported-hostname",
        cpu_percent=cpu,
        load_one=1.0,
        load_five=0.5,
        load_fifteen=0.25,
        memory_total_gb=4.0,
        memory_used_gb=1.0,
        memory_available_gb=3.0,
        memory_percent=85.0,
        swap_total_gb=1.0,
        swap_used_gb=0.1,
        swap_percent=10.0,
        disk_total_gb=20.0,
        disk_used_gb=5.0,
        disk_free_gb=15.0,
        disk_percent=90.0,
        network_interfaces=1,
        network_established=2,
        available=available,
    )


def _snapshot(*, samples: tuple[HostHistoryPoint, ...] | None = None, completeness: SnapshotCompleteness = SnapshotCompleteness.SUFFICIENT, invalid_source_rows: int = 0) -> TelemetrySnapshotExport:
    samples = samples if samples is not None else tuple(
        _host_sample(WINDOW_START + timedelta(seconds=offset))
        for offset in (0, 150, 300)
    )
    metadata = tuple(
        TelemetryMetricCompleteness(metric, completeness, len(samples), 300.0 if len(samples) >= 2 else 0.0)
        for metric in ("cpu_percent", "memory_percent", "disk_percent")
    )
    parents = tuple(
        TelemetrySnapshotParent(index + 1, WINDOW_START + timedelta(seconds=offset), True)
        for index, offset in enumerate((0, 150, 300))
    )
    return TelemetrySnapshotExport(
        host_id="agent",
        evaluation_time=EVALUATION_TIME,
        window_start=WINDOW_START,
        window_end=EVALUATION_TIME,
        cadence_seconds=150.0,
        sample_runs=parents,
        host_samples=samples,
        metric_completeness=metadata,
        completeness=completeness,
        invalid_source_rows=invalid_source_rows,
    )


def _request(snapshot: TelemetrySnapshotExport, *, request_id: str = "adapter-request") -> AdvisorCompositionRequest:
    return adapt_telemetry_snapshot(
        snapshot,
        request_id=request_id,
        evaluation_time=EVALUATION_TIME,
    )


def test_maps_cpu_memory_disk_and_preserves_caller_context():
    snapshot = _snapshot()
    request = _request(snapshot, request_id="caller-request")

    assert isinstance(request, AdvisorCompositionRequest)
    assert request.request_id == "caller-request"
    assert request.evaluation_time is EVALUATION_TIME
    assert request.scope is AdvisorScope.HOST
    assert request.expected_sources == {"history": 9}
    assert len(request.observations) == 9
    assert {record["fields"]["metric"] for record in request.observations} == {
        "cpu_percent",
        "memory_percent",
        "disk_percent",
    }
    assert all(record["source_id"] == "history" for record in request.observations)
    assert all(record["resource_type"] == "host" for record in request.observations)
    assert all(record["resource_id"] == "agent" for record in request.observations)
    assert len(request.history_envelopes) == 3
    assert all(isinstance(envelope, ResourceHistoryEnvelope) for envelope in request.history_envelopes)
    assert all(envelope.resource_id == "agent" for envelope in request.history_envelopes)
    assert all(envelope.cadence_seconds == 150.0 for envelope in request.history_envelopes)
    assert all(envelope.window_start == WINDOW_START for envelope in request.history_envelopes)
    assert all(envelope.window_end == EVALUATION_TIME for envelope in request.history_envelopes)
    assert all(envelope.complete for envelope in request.history_envelopes)


def test_source_timestamps_and_evidence_identity_are_deterministic():
    snapshot = _snapshot()
    first = _request(snapshot)
    second = _request(snapshot)

    assert first.observations == second.observations
    assert first.history_envelopes == second.history_envelopes
    assert [record["evidence_id"] for record in first.observations] == [
        record["evidence_id"] for record in second.observations
    ]
    assert [record["observed_at"] for record in first.observations[:3]] == [
        sample.sampled_at for sample in snapshot.host_samples
    ]
    assert [point.observed_at for point in first.history_envelopes[0].points] == [
        sample.sampled_at for sample in snapshot.host_samples
    ]


def test_incomplete_and_invalid_source_states_fail_closed():
    incomplete = _request(_snapshot(completeness=SnapshotCompleteness.INSUFFICIENT))
    assert all(not envelope.complete for envelope in incomplete.history_envelopes)

    invalid_sample = _host_sample(WINDOW_START, cpu=float("nan"))
    invalid = _request(_snapshot(samples=(invalid_sample,), completeness=SnapshotCompleteness.INVALID, invalid_source_rows=1))
    invalid_cpu = [record for record in invalid.observations if record["state"] == "invalid"][0]
    assert invalid_cpu["state"] == "invalid"
    assert invalid_cpu["fields"] == {}
    assert all(not envelope.complete for envelope in invalid.history_envelopes)


def test_unavailable_host_resource_is_explicit_without_fabricated_value():
    request = _request(
        _snapshot(
            samples=(_host_sample(WINDOW_START, available=False),),
            completeness=SnapshotCompleteness.INSUFFICIENT,
        )
    )
    for record in request.observations:
        assert record["state"] == "unavailable"
        assert "value" not in record["fields"]
        assert record["observed_at"] == WINDOW_START
    assert all(not envelope.points for envelope in request.history_envelopes)


def test_prohibited_telemetry_fields_never_cross_adapter_boundary():
    request = _request(_snapshot())
    prohibited = {"docker_available", "projects_available", "tunnel_state", "hostname"}

    assert not hasattr(request, "docker_available")
    assert not hasattr(request, "projects_available")
    assert not hasattr(request, "tunnel_state")
    for record in request.observations:
        assert not prohibited.intersection(record)
        assert not prohibited.intersection(record["fields"])
    for envelope in request.history_envelopes:
        for point in envelope.points:
            assert not prohibited.intersection(point.canonical())


def test_adapter_has_no_clock_randomness_or_infrastructure_access():
    module = __import__("aipm.services.advisor.observation_adapter", fromlist=["*"])
    tree = ast.parse(inspect.getsource(module))
    imported_names = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_names.update(
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        for alias in node.names
    )
    forbidden_imports = {
        "docker",
        "git",
        "httpx",
        "os",
        "pathlib",
        "platform",
        "psutil",
        "random",
        "requests",
        "shutil",
        "socket",
        "sqlite",
        "sqlite3",
        "subprocess",
        "systemd",
        "time",
        "urllib",
        "uuid",
    }
    assert not imported_names.intersection(forbidden_imports)

    forbidden_calls = {
        "connect",
        "exec",
        "gethostname",
        "now",
        "open",
        "popen",
        "Popen",
        "randint",
        "run",
        "socket",
        "system",
        "time",
        "utcnow",
        "uuid4",
    }
    called_names = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    called_names.update(
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    )
    assert not called_names.intersection(forbidden_calls)


def test_adapter_stops_at_phase4a_request_and_does_not_swallow_request_validation():
    request = _request(_snapshot())
    assert not hasattr(request, "findings")
    assert not hasattr(request, "recommendations")
    with pytest.raises(CompositionError):
        adapt_telemetry_snapshot(
            _snapshot(),
            request_id="invalid request id",
            evaluation_time=EVALUATION_TIME,
        )
    with pytest.raises(TelemetryObservationAdapterError):
        adapt_telemetry_snapshot(
            _snapshot(),
            request_id="adapter-request",
            evaluation_time=EVALUATION_TIME + timedelta(seconds=1),
        )
