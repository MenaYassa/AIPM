from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from aipm.models.advisor import (
    AdvisorScope,
    AdvisorStatus,
    EvidenceState,
    ResourceHistorySummary,
    ResourceHistorySummaryState,
    UncertaintyKind,
)
from aipm.services.advisor.composition import (
    AdvisorCompositionRequest,
    CompositionError,
    MAX_COMPOSITION_OBSERVATIONS,
    compose_advisor,
)
from aipm.services.advisor.rules import AdvisorRuleEngine, ResourceHistoryEnvelope


UTC = timezone.utc
EVALUATION = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def observation(*, state: str = "observed", observed_at: datetime | None = None, fields: dict | None = None, evidence_id: str = "svc-1") -> dict:
    return {
        "evidence_id": evidence_id,
        "source_id": "service_health",
        "resource_type": "service",
        "resource_id": "aipm-dashboard",
        "state": state,
        "observed_at": observed_at or EVALUATION - timedelta(minutes=1),
        "fields": fields or {"service_status": "healthy"},
    }


def request(observations: list[dict], *, evaluation_time: datetime = EVALUATION, **kwargs) -> AdvisorCompositionRequest:
    return AdvisorCompositionRequest(
        request_id="req-phase4a",
        evaluation_time=evaluation_time,
        observations=observations,
        **kwargs,
    )


def test_resource_history_summary_is_transport_only_and_preserves_rule_output() -> None:
    summary = ResourceHistorySummary(
        metric="cpu_percent",
        state=ResourceHistorySummaryState.COMPLETE,
        valid_point_count=6,
        temporal_span_seconds=300.0,
        cadence_seconds=60.0,
        peak_value=35.1,
        peak_observed_at=EVALUATION,
    )
    req = request([observation()], resource_history_summary=(summary,))
    response = compose_advisor(req)
    baseline = compose_advisor(request([observation()]))

    assert req.resource_history_summary == (summary,)
    assert response.resource_history_summary == (summary,)
    assert response.findings == baseline.findings
    assert response.recommendations == baseline.recommendations
    assert response.uncertainties == baseline.uncertainties
    assert response.evidence_coverage == baseline.evidence_coverage


def test_successful_raw_observation_composes_to_advisor_response() -> None:
    response = compose_advisor(request([observation()]))

    assert response.request_id == "req-phase4a"
    assert response.evaluation_time == EVALUATION
    assert response.generated_at == EVALUATION
    assert response.status is AdvisorStatus.FRESH
    assert response.scope is AdvisorScope.OVERVIEW
    assert response.findings == ()


def test_evaluation_time_is_propagated_and_timezone_equivalent_inputs_are_deterministic() -> None:
    equivalent = EVALUATION.astimezone(timezone(timedelta(hours=2)))
    first = compose_advisor(request([observation()], evaluation_time=EVALUATION))
    second = compose_advisor(request([observation()], evaluation_time=equivalent))

    assert first.evaluation_time == second.evaluation_time
    assert first.canonical_json() == second.canonical_json()
    assert first.stable_id == second.stable_id


@pytest.mark.parametrize("value", [datetime(2026, 8, 23, 12, 0), None])
def test_naive_or_missing_evaluation_time_is_rejected(value: datetime | None) -> None:
    with pytest.raises(CompositionError):
        AdvisorCompositionRequest(request_id="req", evaluation_time=value, observations=())


def test_request_is_immutable_and_snapshots_nested_observation_containers() -> None:
    fields = {"service_status": "healthy"}
    raw = observation(fields=fields)
    req = request([raw])
    raw["fields"] = {"service_status": "unavailable"}
    fields["service_status"] = "unavailable"

    assert isinstance(req.observations[0], MappingProxyType)
    assert dict(req.observations[0]["fields"]) == {"service_status": "healthy"}
    with pytest.raises(AttributeError):
        req.request_id = "changed"  # type: ignore[misc]


def test_resource_history_summary_request_is_bounded_and_typed() -> None:
    summary = ResourceHistorySummary("cpu_percent", "incomplete", 5, 240.0, 60.0)
    assert request([observation()], resource_history_summary=[summary]).resource_history_summary == (summary,)
    with pytest.raises(CompositionError):
        request([observation()], resource_history_summary=(summary,) * 4)
    with pytest.raises(CompositionError):
        request([observation()], resource_history_summary=({"metric": "cpu_percent"},))  # type: ignore[arg-type]


def test_request_bounds_and_malformed_observations_fail_closed() -> None:
    records = [observation(evidence_id=f"svc-{index}") for index in range(MAX_COMPOSITION_OBSERVATIONS + 1)]
    with pytest.raises(CompositionError):
        request(records)
    with pytest.raises(CompositionError):
        request(["not-a-mapping"])  # type: ignore[list-item]


def test_invalid_source_observation_remains_invalid_and_uncertain() -> None:
    response = compose_advisor(request([observation(fields={"service_status": "systemctl restart"})]))

    assert not response.findings
    assert any(item.kind is UncertaintyKind.INVALID_EVIDENCE for item in response.uncertainties)
    assert response.status is AdvisorStatus.ERROR


def test_stale_evidence_remains_stale() -> None:
    stale = observation(
        observed_at=EVALUATION - timedelta(minutes=10),
        fields={"service_status": "healthy"},
    )
    stale["freshness_deadline"] = EVALUATION - timedelta(minutes=1)

    response = compose_advisor(request([stale]))

    assert any(item.kind is UncertaintyKind.STALE_EVIDENCE for item in response.uncertainties)
    assert response.status is AdvisorStatus.STALE


def test_unavailable_evidence_remains_unavailable() -> None:
    response = compose_advisor(request([observation(state="unavailable", fields={"service_status": "unavailable"})]))

    assert any(item.kind is UncertaintyKind.UNAVAILABLE_SOURCE for item in response.uncertainties)
    assert response.findings
    assert response.findings[0].confidence.value == "unknown"


def test_not_observed_evidence_remains_explicit() -> None:
    response = compose_advisor(request([observation(state="not_observed", fields={"service_status": "unknown"})]))

    assert any(item.kind is UncertaintyKind.MISSING_EVIDENCE for item in response.uncertainties)
    assert response.status is AdvisorStatus.UNAVAILABLE


def test_conflicting_evidence_remains_explicit_without_preference() -> None:
    first = observation(evidence_id="svc-observed", fields={"service_status": "healthy"})
    second = observation(state="unavailable", evidence_id="svc-unavailable", fields={"service_status": "unavailable"})

    response = compose_advisor(request([first, second]))

    assert any(item.kind is UncertaintyKind.CONFLICTING_EVIDENCE for item in response.uncertainties)
    assert not any(finding.rule_id == "service.health.unavailable" for finding in response.findings)


def test_typed_history_envelopes_are_passed_without_reconstruction() -> None:
    envelope = ResourceHistoryEnvelope(
        resource_id="host-1",
        metric="cpu_percent",
        unit="percent",
        cadence_seconds=60,
        window_start=EVALUATION - timedelta(minutes=5),
        window_end=EVALUATION,
        complete=False,
        points=(),
    )
    req = request([], history_envelopes=(envelope,))

    assert req.history_envelopes[0] is envelope
    response = compose_advisor(req)
    assert any(item.kind is UncertaintyKind.MISSING_EVIDENCE for item in response.uncertainties)


def test_evidence_reference_integrity_is_preserved() -> None:
    response = compose_advisor(request([observation(state="unavailable", fields={"service_status": "unavailable"})]))
    finding_ids = {finding.finding_id for finding in response.findings}

    for finding in response.findings:
        assert finding.evidence_refs
    for recommendation in response.recommendations:
        assert set(recommendation.finding_refs).issubset(finding_ids)
        assert set(recommendation.evidence_refs).issubset(set().union(*(set(f.evidence_refs) for f in response.findings)))


def test_composition_has_no_clock_or_runtime_authority_imports() -> None:
    source = open("src/aipm/services/advisor/composition.py", encoding="utf-8").read()
    tree = ast.parse(source)
    imported = {node.names[0].name for node in ast.walk(tree) if isinstance(node, ast.Import)}
    imported.update(node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module)

    forbidden = {"os", "pathlib", "subprocess", "socket", "requests", "urllib", "sqlite3", "psutil", "docker", "random", "uuid"}
    assert not imported.intersection(forbidden)
    assert "datetime.now" not in source
    assert "time.time" not in source


def test_provenance_and_expected_sources_are_forwarded_without_new_semantics() -> None:
    response = compose_advisor(
        request(
            [observation()],
            expected_sources={"service_health": 1},
        )
    )

    assert response.evidence_coverage[0].source_id == "service_health"
    assert response.evidence_coverage[0].expected == 1
    assert response.provenance == ()


def test_request_scope_is_preserved() -> None:
    response = compose_advisor(request([observation()], scope=AdvisorScope.SERVICES))
    assert response.scope is AdvisorScope.SERVICES


def test_composition_determinism_covers_all_relevant_collection_permutations() -> None:
    service = observation(evidence_id="svc-permutation")
    telemetry = {
        "evidence_id": "telemetry-permutation",
        "source_id": "telemetry",
        "resource_type": "history",
        "resource_id": "telemetry-cadence",
        "state": "observed",
        "observed_at": EVALUATION - timedelta(minutes=2),
        "fields": {"cadence_seconds": 60},
    }
    history_one = ResourceHistoryEnvelope(
        resource_id="resource-one",
        metric="cpu_percent",
        unit="percent",
        cadence_seconds=60,
        window_start=EVALUATION - timedelta(minutes=5),
        window_end=EVALUATION,
        complete=False,
        points=(),
    )
    history_two = ResourceHistoryEnvelope(
        resource_id="resource-two",
        metric="memory_percent",
        unit="percent",
        cadence_seconds=60,
        window_start=EVALUATION - timedelta(minutes=5),
        window_end=EVALUATION,
        complete=False,
        points=(),
    )

    request_a = request(
        [service, telemetry],
        expected_sources={"service_health": 1, "telemetry": 1},
        history_envelopes=(history_one, history_two),
    )
    request_b = request(
        [telemetry, service],
        expected_sources={"telemetry": 1, "service_health": 1},
        history_envelopes=(history_two, history_one),
    )

    response_a = compose_advisor(request_a)
    response_b = compose_advisor(request_b)

    assert response_a.canonical_json() == response_b.canonical_json()
    assert response_a.stable_id == response_b.stable_id


def test_expected_sources_mapping_is_snapshotted_and_composition_uses_original() -> None:
    expected_sources = {"service_health": 1, "telemetry": 2}
    req = request([observation()], expected_sources=expected_sources)

    expected_sources["service_health"] = 99
    expected_sources.pop("telemetry")
    expected_sources["new-source"] = 7

    assert dict(req.expected_sources) == {"service_health": 1, "telemetry": 2}
    response = compose_advisor(req)
    coverage = {item.source_id: item.expected for item in response.evidence_coverage}
    assert coverage == {"service_health": 1, "telemetry": 2}


def test_provenance_mapping_and_collection_are_snapshotted() -> None:
    provenance_record = {
        "provenance_ref_id": "prov-1",
        "source_id": "service_health",
        "provenance_id": "source-prov-1",
        "key_id": "key-1",
        "signature_verified": True,
    }
    provenance = [provenance_record]
    req = request([observation()], provenance=provenance)

    provenance_record["key_id"] = "key-mutated"
    provenance_record["signature_verified"] = False
    provenance.append({"provenance_ref_id": "prov-2", "source_id": "service_health", "provenance_id": "source-prov-2", "key_id": "key-2", "signature_verified": True})

    assert len(req.provenance) == 1
    assert req.provenance[0]["key_id"] == "key-1"
    assert req.provenance[0]["signature_verified"] is True
    response = compose_advisor(req)
    assert tuple(item.provenance_ref_id for item in response.provenance) == ("prov-1",)


def test_history_envelope_collection_is_snapshotted_and_passed_by_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    history_one = ResourceHistoryEnvelope(
        resource_id="resource-one",
        metric="cpu_percent",
        unit="percent",
        cadence_seconds=60,
        window_start=EVALUATION - timedelta(minutes=5),
        window_end=EVALUATION,
        complete=False,
        points=(),
    )
    history_two = ResourceHistoryEnvelope(
        resource_id="resource-two",
        metric="memory_percent",
        unit="percent",
        cadence_seconds=60,
        window_start=EVALUATION - timedelta(minutes=5),
        window_end=EVALUATION,
        complete=False,
        points=(),
    )
    histories = [history_one, history_two]
    req = request([], history_envelopes=histories)

    replacement = ResourceHistoryEnvelope(
        resource_id="replacement",
        metric="disk_percent",
        unit="percent",
        cadence_seconds=60,
        window_start=EVALUATION - timedelta(minutes=5),
        window_end=EVALUATION,
        complete=False,
        points=(),
    )
    histories.reverse()
    histories[0] = replacement
    histories.append(replacement)
    histories.pop()

    assert req.history_envelopes == (history_one, history_two)
    assert req.history_envelopes[0] is history_one
    assert req.history_envelopes[1] is history_two

    received: dict[str, object] = {}
    original_evaluate = AdvisorRuleEngine.evaluate

    def observe_evaluate(
        engine: AdvisorRuleEngine,
        bundle: object,
        *,
        request_id: str,
        evaluation_time: datetime,
        history_envelopes: tuple[ResourceHistoryEnvelope, ...],
    ) -> object:
        received["history_envelopes"] = history_envelopes
        return original_evaluate(
            engine,
            bundle,
            request_id=request_id,
            evaluation_time=evaluation_time,
            history_envelopes=history_envelopes,
        )

    monkeypatch.setattr(AdvisorRuleEngine, "evaluate", observe_evaluate)
    compose_advisor(req)

    forwarded = received["history_envelopes"]
    assert isinstance(forwarded, tuple)
    assert forwarded[0] is history_one
    assert forwarded[1] is history_two


def test_nested_safe_link_input_is_snapshotted() -> None:
    safe_link = {"route": "/services/aipm-dashboard", "label": "Dashboard"}
    raw = observation()
    raw["safe_links"] = [safe_link]
    req = request([raw])

    safe_link["label"] = "Mutated"
    raw["safe_links"].append({"route": "/services/other", "label": "Other"})

    response = compose_advisor(req)
    assert tuple(link.route for link in response.links) == ("/services/aipm-dashboard",)
    assert tuple(link.label for link in response.links) == ("Dashboard",)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", ""),
        ("expected_sources", "not-a-sequence"),
        ("provenance", "not-a-sequence"),
        ("history_envelopes", "not-a-sequence"),
    ],
)
def test_invalid_top_level_request_shapes_are_rejected_before_composition(field: str, value: object) -> None:
    kwargs = {"request_id": "req-valid", "evaluation_time": EVALUATION, "observations": []}
    kwargs[field] = value

    with pytest.raises(CompositionError):
        AdvisorCompositionRequest(**kwargs)  # type: ignore[arg-type]


def test_compose_advisor_invokes_lower_layers_without_interception() -> None:
    source = open("src/aipm/services/advisor/composition.py", encoding="utf-8").read()
    tree = ast.parse(source)
    compose_function = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "compose_advisor"
    )

    assert not any(isinstance(node, ast.Try) for node in ast.walk(compose_function))
    called_methods = {
        node.func.attr
        for node in ast.walk(compose_function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert {"normalize", "evaluate"}.issubset(called_methods)
