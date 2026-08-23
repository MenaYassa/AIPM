from datetime import datetime, timedelta, timezone

import pytest

from aipm.models.advisor import AdvisorStatus, AdvisorValidationError, EvidenceState, UncertaintyKind
from aipm.services.advisor.normalizer import normalize_observations
from aipm.services.advisor.rules import (
    AdvisorRuleEngine,
    PHASE3_FIELD_SCHEMA,
    RULE_CATALOG,
    RULE_SET_VERSION,
    RULE_VERSION,
    ResourceHistoryEnvelope,
    ResourceHistoryPoint,
)


EVALUATION_TIME = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def record(
    evidence_id: str,
    *,
    source_id: str,
    resource_type: str,
    resource_id: str,
    state: str = "observed",
    observed_at: datetime | None = EVALUATION_TIME - timedelta(minutes=1),
    fields: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "source_id": source_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "state": state,
        "observed_at": observed_at,
        "fields": fields or {},
        "safe_links": [{"route": "/api/history/host", "label": "History"}],
    }


def evaluate(
    records: list[dict[str, object]],
    *,
    request_id: str = "request-1",
    history_envelopes: tuple[ResourceHistoryEnvelope, ...] = (),
):
    bundle = normalize_observations(records, evaluation_time=EVALUATION_TIME)
    return bundle, AdvisorRuleEngine().evaluate(
        bundle,
        request_id=request_id,
        evaluation_time=EVALUATION_TIME,
        history_envelopes=history_envelopes,
    )


def history_envelope(
    *,
    resource_id: str = "host-1",
    metric: str = "cpu_percent",
    cadence_seconds: float = 100,
    window_start: datetime = EVALUATION_TIME - timedelta(minutes=5),
    window_end: datetime = EVALUATION_TIME,
    complete: bool = True,
    offsets: tuple[int, ...] = (0, 150, 300),
    values: tuple[float, ...] = (85.0, 85.0, 85.0),
    states: tuple[str, ...] | None = None,
    evidence_prefix: str = "history",
) -> ResourceHistoryEnvelope:
    point_states = states if states is not None else tuple("observed" for _ in offsets)
    points = tuple(
        ResourceHistoryPoint(
            evidence_id=f"{evidence_prefix}-{index}",
            observed_at=window_start + timedelta(seconds=offset),
            metric=metric,
            value=value,
            unit="percent",
            state=state,
        )
        for index, (offset, value, state) in enumerate(zip(offsets, values, point_states, strict=True))
    )
    return ResourceHistoryEnvelope(
        resource_id=resource_id,
        metric=metric,
        unit="percent",
        cadence_seconds=cadence_seconds,
        window_start=window_start,
        window_end=window_end,
        complete=complete,
        points=points,
    )


def test_catalog_and_field_schema_are_fixed_and_explicit() -> None:
    assert RULE_SET_VERSION == "mc613-rules-v1"
    assert RULE_VERSION == "1.0.0"
    assert [item.rule_id for item in RULE_CATALOG] == [
        "service.health.unavailable",
        "service.health.stale",
        "resource.pressure.sustained",
        "resource.pressure.spike",
        "telemetry.cadence.gap",
        "telemetry.source.degraded",
        "deployment.revision.changed",
        "deployment.posture.unverified",
        "project.state.changed",
        "project.health.degraded",
    ]
    assert len({item.name for item in PHASE3_FIELD_SCHEMA}) == len(PHASE3_FIELD_SCHEMA)
    assert "baseline" not in {item.name for item in PHASE3_FIELD_SCHEMA}
    assert "current" not in {item.name for item in PHASE3_FIELD_SCHEMA}


def test_service_health_rules_detect_unavailable_and_stale() -> None:
    _, unavailable = evaluate([
        record("service-unavailable", source_id="service_health", resource_type="service", resource_id="api", state="unavailable", observed_at=None, fields={"service_status": "unavailable"}),
    ])
    assert any(item.rule_id == "service.health.unavailable" for item in unavailable.findings)

    _, stale = evaluate([
        record("service-stale", source_id="service_health", resource_type="service", resource_id="api", state="stale"),
    ])
    assert any(item.rule_id == "service.health.stale" for item in stale.findings)


def test_service_health_does_not_infer_from_absence_or_conflict() -> None:
    _, empty = evaluate([])
    assert not any(item.category.value == "service_health" for item in empty.findings)

    _, conflict = evaluate([
        record("service-unavailable", source_id="service_health", resource_type="service", resource_id="api", state="unavailable", observed_at=None, fields={"service_status": "unavailable"}),
        record("service-healthy", source_id="service_health", resource_type="service", resource_id="api", fields={"service_status": "healthy"}),
    ])
    assert not any(item.rule_id == "service.health.unavailable" for item in conflict.findings)


def test_sustained_resource_pressure_requires_canonical_continuous_history() -> None:
    records = [
        record(f"history-{index}", source_id="history", resource_type="host", resource_id="host-1", observed_at=EVALUATION_TIME - timedelta(minutes=5) + timedelta(seconds=offset), fields={"metric": "cpu_percent", "value": 85.0, "unit": "percent"})
        for index, offset in enumerate((0, 150, 300))
    ]
    _, response = evaluate(records, history_envelopes=(history_envelope(),))
    finding = next(item for item in response.findings if item.rule_id == "resource.pressure.sustained")
    assert finding.severity.value == "critical"

    _, negative = evaluate([
        record("history-0", source_id="history", resource_type="host", resource_id="host-1", fields={"metric": "cpu_percent", "value": 99.0, "unit": "percent"}),
        record("history-1", source_id="history", resource_type="host", resource_id="host-1", fields={"metric": "cpu_percent", "value": 99.0, "unit": "percent"}),
    ])
    assert not any(item.rule_id == "resource.pressure.sustained" for item in negative.findings)


def test_resource_spike_requires_canonical_comparison_fields() -> None:
    _, response = evaluate([
        record("resource-spike", source_id="history", resource_type="resource", resource_id="host-1", fields={"comparison_status": "changed", "baseline_value": 50, "current_value": 75, "metric": "cpu_percent", "unit": "percent"}),
    ])
    assert any(item.rule_id == "resource.pressure.spike" for item in response.findings)

    _, negative = evaluate([
        record("resource-flat", source_id="history", resource_type="resource", resource_id="host-1", fields={"comparison_status": "indeterminate", "baseline_value": 50, "current_value": 90, "metric": "cpu_percent", "unit": "percent"}),
    ])
    assert not any(item.rule_id == "resource.pressure.spike" for item in negative.findings)


def test_telemetry_cadence_gap_and_source_degradation_are_explicit() -> None:
    _, gap = evaluate([
        record("sample-1", source_id="telemetry", resource_type="history", resource_id="telemetry", observed_at=EVALUATION_TIME - timedelta(minutes=10), fields={"cadence_seconds": 60}),
        record("sample-2", source_id="telemetry", resource_type="history", resource_id="telemetry", observed_at=EVALUATION_TIME, fields={"cadence_seconds": 60}),
    ])
    assert any(item.rule_id == "telemetry.cadence.gap" for item in gap.findings)

    _, degraded = evaluate([
        record("telemetry-unavailable", source_id="telemetry", resource_type="resource", resource_id="telemetry", state="unavailable", observed_at=None, fields={"retention_status": "unavailable"}),
    ])
    assert any(item.rule_id == "telemetry.source.degraded" for item in degraded.findings)
    assert any(item.kind is UncertaintyKind.UNAVAILABLE_SOURCE for item in degraded.uncertainties)


def test_deployment_rules_distinguish_revision_change_from_unverified_posture() -> None:
    _, changed = evaluate([
        record("deployment-change", source_id="project", resource_type="deployment", resource_id="aipm", fields={"comparison_status": "changed", "baseline_revision": "abc123", "current_revision": "def456"}),
    ])
    assert any(item.rule_id == "deployment.revision.changed" for item in changed.findings)

    _, unverified = evaluate([
        record("deployment-unverified", source_id="project", resource_type="deployment", resource_id="aipm", fields={"revision": "def456", "runtime_confirmation_status": "unavailable"}),
    ])
    assert any(item.rule_id == "deployment.posture.unverified" for item in unverified.findings)


def test_project_rules_require_proven_identity_and_supporting_health_evidence() -> None:
    _, changed = evaluate([
        record("project-change", source_id="project", resource_type="project", resource_id="aipm", fields={"comparison_status": "changed", "identity_proven": True, "changed_field": "dirty"}),
    ])
    assert any(item.rule_id == "project.state.changed" for item in changed.findings)

    _, degraded = evaluate([
        record("project-health", source_id="project", resource_type="project", resource_id="aipm", fields={"health_status": "degraded", "supporting_evidence_count": 1}),
    ])
    assert any(item.rule_id == "project.health.degraded" for item in degraded.findings)

    _, unresolved = evaluate([
        record("project-unresolved", source_id="project", resource_type="project", resource_id="candidate", fields={"comparison_status": "changed", "changed_field": "dirty", "identity_proven": False}),
    ])
    assert not any(item.rule_id == "project.state.changed" for item in unresolved.findings)


def test_aliases_are_not_accepted_as_canonical_substitutes() -> None:
    _, response = evaluate([
        record("resource-alias", source_id="history", resource_type="resource", resource_id="host-1", fields={"comparison_status": "changed", "baseline": 50, "current": 75, "metric": "cpu_percent", "unit": "percent"}),
    ])
    assert not any(item.rule_id == "resource.pressure.spike" for item in response.findings)
    assert any(item.kind is UncertaintyKind.MISSING_EVIDENCE for item in response.uncertainties)


def test_stale_and_invalid_evidence_never_becomes_a_high_confidence_positive_claim() -> None:
    _, stale = evaluate([
        record("deployment-stale", source_id="project", resource_type="deployment", resource_id="aipm", state="stale", fields={"comparison_status": "changed", "baseline_revision": "abc123", "current_revision": "def456"}),
    ])
    assert not any(item.rule_id == "deployment.revision.changed" for item in stale.findings)

    bundle, invalid = evaluate([
        record("telemetry-invalid", source_id="telemetry", resource_type="resource", resource_id="telemetry", state="invalid", fields={"retention_status": "failed"}),
    ])
    assert not any(item.rule_id == "telemetry.source.degraded" for item in invalid.findings)
    assert any(item.kind is UncertaintyKind.INVALID_EVIDENCE for item in invalid.uncertainties)
    assert bundle.items[0].fields == ()


def test_traceability_and_response_evaluation_context_are_preserved() -> None:
    bundle, response = evaluate([
        record("resource-spike", source_id="history", resource_type="resource", resource_id="host-1", fields={"comparison_status": "changed", "baseline_value": 50, "current_value": 75, "metric": "cpu_percent", "unit": "percent"}),
    ])
    response.validate_against_bundle(bundle)
    assert response.evaluation_time == EVALUATION_TIME
    assert response.generated_at == EVALUATION_TIME
    assert all(set(item.evidence_refs).issubset(bundle.evidence_ids) for item in response.findings)
    assert all(set(item.finding_refs).issubset({finding.finding_id for finding in response.findings}) for item in response.recommendations)
    with pytest.raises(Exception):
        AdvisorRuleEngine().evaluate(bundle, request_id="request-1", evaluation_time=EVALUATION_TIME + timedelta(seconds=1))


def test_equivalent_input_permutations_have_identical_response_serialization_and_ids() -> None:
    records = [
        record("deployment-change", source_id="project", resource_type="deployment", resource_id="aipm", fields={"comparison_status": "changed", "baseline_revision": "abc123", "current_revision": "def456"}),
        record("project-change", source_id="project", resource_type="project", resource_id="aipm", fields={"comparison_status": "changed", "identity_proven": True, "changed_field": "revision"}),
    ]
    first_bundle, first = evaluate(records)
    second_bundle, second = evaluate(list(reversed(records)))
    assert first.canonical_json() == second.canonical_json()
    assert first.stable_id == second.stable_id
    assert first_bundle.canonical_json() == second_bundle.canonical_json()


def test_rule_outputs_use_only_safe_explanatory_text_and_no_authority_fields() -> None:
    _, response = evaluate([
        record("service-unavailable", source_id="service_health", resource_type="service", resource_id="api", state="unavailable", observed_at=None, fields={"service_status": "unavailable"}),
    ])
    for recommendation in response.recommendations:
        text = " ".join((recommendation.title, recommendation.summary, recommendation.rationale)).casefold()
        assert "systemctl" not in text
        assert "restart" not in text
        assert "execute" not in text
        assert "approve" not in text
        assert not hasattr(recommendation, "command")
        assert not hasattr(recommendation, "operation")
        assert not hasattr(recommendation, "provider")


def _history_records(
    *,
    resource_id: str,
    metric: str,
    offsets: tuple[int, ...],
    values: tuple[float, ...],
    states: tuple[str, ...] | None = None,
    evidence_prefix: str,
) -> list[dict[str, object]]:
    point_states = states if states is not None else tuple("observed" for _ in offsets)
    return [
        record(
            f"{evidence_prefix}-{index}",
            source_id="history",
            resource_type="host",
            resource_id=resource_id,
            state=state,
            observed_at=EVALUATION_TIME - timedelta(minutes=5) + timedelta(seconds=offset),
            fields={"metric": metric, "value": value, "unit": "percent"},
        )
        for index, (offset, value, state) in enumerate(zip(offsets, values, point_states, strict=True))
    ]


def test_continuity_accepts_more_than_three_valid_points_and_exact_maximum_gap() -> None:
    envelope = history_envelope(offsets=(0, 100, 200, 300), values=(85.0, 86.0, 87.0, 88.0), evidence_prefix="continuous")
    records = _history_records(resource_id="host-1", metric="cpu_percent", offsets=(0, 100, 200, 300), values=(85.0, 86.0, 87.0, 88.0), evidence_prefix="continuous")
    _, response = evaluate(records, history_envelopes=(envelope,))
    assert any(item.rule_id == "resource.pressure.sustained" for item in response.findings)
    assert envelope.maximum_allowable_gap_seconds == 150.0


def test_sparse_history_and_gap_above_policy_are_uncertain_and_not_sustained() -> None:
    offsets = (0, 151, 300)
    envelope = history_envelope(offsets=offsets, values=(90.0, 90.0, 90.0), evidence_prefix="sparse")
    records = _history_records(resource_id="host-1", metric="cpu_percent", offsets=offsets, values=(90.0, 90.0, 90.0), evidence_prefix="sparse")
    _, response = evaluate(records, history_envelopes=(envelope,))
    assert not any(item.rule_id == "resource.pressure.sustained" for item in response.findings)
    assert any(item.kind is UncertaintyKind.MISSING_EVIDENCE for item in response.uncertainties)


def test_incomplete_or_short_history_is_withheld() -> None:
    incomplete = history_envelope(complete=False, evidence_prefix="incomplete")
    records = _history_records(resource_id="host-1", metric="cpu_percent", offsets=(0, 150, 300), values=(90.0, 90.0, 90.0), evidence_prefix="incomplete")
    _, incomplete_response = evaluate(records, history_envelopes=(incomplete,))
    assert not any(item.rule_id == "resource.pressure.sustained" for item in incomplete_response.findings)
    assert any(item.kind is UncertaintyKind.MISSING_EVIDENCE for item in incomplete_response.uncertainties)

    short_start = EVALUATION_TIME - timedelta(minutes=4)
    short = history_envelope(window_start=short_start, offsets=(0, 120, 240), values=(90.0, 90.0, 90.0), evidence_prefix="short")
    short_records = _history_records(resource_id="host-1", metric="cpu_percent", offsets=(-60, 60, 180), values=(90.0, 90.0, 90.0), evidence_prefix="short")
    _, short_response = evaluate(short_records, history_envelopes=(short,))
    assert not any(item.rule_id == "resource.pressure.sustained" for item in short_response.findings)
    assert any(item.kind is UncertaintyKind.MISSING_EVIDENCE for item in short_response.uncertainties)


def test_below_threshold_and_isolated_spike_do_not_become_sustained() -> None:
    offsets = (0, 150, 300)
    below = history_envelope(values=(85.0, 84.99, 85.0), evidence_prefix="below")
    below_records = _history_records(resource_id="host-1", metric="cpu_percent", offsets=offsets, values=(85.0, 84.99, 85.0), evidence_prefix="below")
    _, below_response = evaluate(below_records, history_envelopes=(below,))
    assert not any(item.rule_id == "resource.pressure.sustained" for item in below_response.findings)

    spike = history_envelope(values=(100.0, 40.0, 40.0), evidence_prefix="spike")
    spike_records = _history_records(resource_id="host-1", metric="cpu_percent", offsets=offsets, values=(100.0, 40.0, 40.0), evidence_prefix="spike")
    _, spike_response = evaluate(spike_records, history_envelopes=(spike,))
    assert not any(item.rule_id == "resource.pressure.sustained" for item in spike_response.findings)


def test_degraded_history_points_prevent_continuity() -> None:
    for state in ("stale", "unavailable", "not_observed", "invalid"):
        prefix = f"{state}-history"
        offsets = (0, 150, 300)
        states = ("observed", state, "observed")
        envelope = history_envelope(states=states, evidence_prefix=prefix)
        records = _history_records(resource_id="host-1", metric="cpu_percent", offsets=offsets, values=(90.0, 90.0, 90.0), states=states, evidence_prefix=prefix)
        _, response = evaluate(records, history_envelopes=(envelope,))
        assert not any(item.rule_id == "resource.pressure.sustained" for item in response.findings)
        assert response.uncertainties


def test_malformed_continuity_metadata_is_rejected() -> None:
    with pytest.raises(AdvisorValidationError):
        history_envelope(cadence_seconds=0)
    with pytest.raises(AdvisorValidationError):
        history_envelope(cadence_seconds=float("nan"))
    with pytest.raises(AdvisorValidationError):
        history_envelope(metric="unsupported")


def test_resource_threshold_boundaries_are_inclusive_for_cpu_memory_and_disk() -> None:
    thresholds = {"cpu_percent": 85.0, "memory_percent": 85.0, "disk_percent": 90.0}
    for metric, threshold in thresholds.items():
        prefix = f"boundary-{metric}"
        offsets = (0, 150, 300)
        envelope = history_envelope(resource_id=f"resource-{metric}", metric=metric, values=(threshold, threshold, threshold), evidence_prefix=prefix)
        records = _history_records(resource_id=f"resource-{metric}", metric=metric, offsets=offsets, values=(threshold, threshold, threshold), evidence_prefix=prefix)
        _, response = evaluate(records, history_envelopes=(envelope,))
        assert any(item.rule_id == "resource.pressure.sustained" for item in response.findings)


def test_continuity_envelope_and_response_are_deterministic_under_input_permutation() -> None:
    offsets = (0, 150, 300)
    envelope = history_envelope(values=(85.0, 86.0, 87.0), evidence_prefix="deterministic")
    records = _history_records(resource_id="host-1", metric="cpu_percent", offsets=offsets, values=(85.0, 86.0, 87.0), evidence_prefix="deterministic")
    first_bundle, first = evaluate(records, history_envelopes=(envelope,))
    second_bundle, second = evaluate(list(reversed(records)), history_envelopes=(envelope,))
    assert envelope.canonical() == history_envelope(values=(85.0, 86.0, 87.0), evidence_prefix="deterministic").canonical()
    assert first.canonical_json() == second.canonical_json()
    assert first.stable_id == second.stable_id
    assert first_bundle.canonical_json() == second_bundle.canonical_json()


def test_history_envelope_rejects_duplicate_evidence_ids_and_timestamps() -> None:
    start = EVALUATION_TIME - timedelta(minutes=5)
    duplicate_id_points = (
        ResourceHistoryPoint("duplicate-id", start, "cpu_percent", 85.0),
        ResourceHistoryPoint("duplicate-id", start + timedelta(seconds=150), "cpu_percent", 85.0),
    )
    with pytest.raises(AdvisorValidationError, match="Duplicate resource-history evidence ID"):
        ResourceHistoryEnvelope("host-1", "cpu_percent", "percent", 100, start, EVALUATION_TIME, True, duplicate_id_points)

    duplicate_timestamp_points = (
        ResourceHistoryPoint("timestamp-a", start, "cpu_percent", 85.0),
        ResourceHistoryPoint("timestamp-b", start, "cpu_percent", 85.0),
    )
    with pytest.raises(AdvisorValidationError, match="Duplicate resource-history observed_at timestamp"):
        ResourceHistoryEnvelope("host-1", "cpu_percent", "percent", 100, start, EVALUATION_TIME, True, duplicate_timestamp_points)


@pytest.mark.parametrize("mismatch", ("metric", "value", "unit", "observed_at", "resource_id", "state"))
def test_sustained_history_requires_exact_evidence_point_binding(mismatch: str) -> None:
    prefix = f"binding-{mismatch}"
    offsets = (0, 150, 300)
    values = (85.0, 85.0, 85.0)
    envelope = history_envelope(evidence_prefix=prefix)
    records = _history_records(resource_id="host-1", metric="cpu_percent", offsets=offsets, values=values, evidence_prefix=prefix)
    target = records[1]
    if mismatch == "metric":
        target["fields"] = {"metric": "memory_percent", "value": 85.0, "unit": "percent"}
    elif mismatch == "value":
        target["fields"] = {"metric": "cpu_percent", "value": 84.0, "unit": "percent"}
    elif mismatch == "unit":
        target["fields"] = {"metric": "cpu_percent", "value": 85.0, "unit": "bytes"}
    elif mismatch == "observed_at":
        target["observed_at"] = EVALUATION_TIME - timedelta(minutes=2, seconds=-1)
    elif mismatch == "resource_id":
        target["resource_id"] = "other-resource"
    elif mismatch == "state":
        target["state"] = "stale"
    else:
        raise AssertionError(mismatch)

    _, response = evaluate(records, history_envelopes=(envelope,))
    assert not any(item.rule_id == "resource.pressure.sustained" for item in response.findings)
    assert any(
        item.kind is UncertaintyKind.INVALID_EVIDENCE and "binding-" in " ".join(item.evidence_refs)
        for item in response.uncertainties
    )


def test_exact_envelope_and_evidence_binding_produces_sustained_finding() -> None:
    prefix = "binding-exact"
    offsets = (0, 150, 300)
    values = (85.0, 86.0, 87.0)
    envelope = history_envelope(values=values, evidence_prefix=prefix)
    records = _history_records(resource_id="host-1", metric="cpu_percent", offsets=offsets, values=values, evidence_prefix=prefix)
    _, response = evaluate(records, history_envelopes=(envelope,))
    assert any(item.rule_id == "resource.pressure.sustained" for item in response.findings)
