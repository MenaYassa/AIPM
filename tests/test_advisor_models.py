from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from aipm.models.advisor import (
    AdvisorCategory,
    AdvisorResponse,
    AdvisorScope,
    AdvisorStatus,
    AdvisorValidationError,
    Confidence,
    ConfidenceImpact,
    Coverage,
    EvidenceBundle,
    EvidenceItem,
    EvidenceState,
    Finding,
    FindingSeverity,
    ProvenanceReference,
    Recommendation,
    RecommendationStatus,
    ResourceHistorySummary,
    ResourceHistorySummaryState,
    SafeLink,
    Uncertainty,
    UncertaintyKind,
)


EVALUATION_TIME = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def evidence(
    evidence_id: str = "telemetry-host-1",
    *,
    source_id: str = "telemetry",
    resource_type: str = "host",
    resource_id: str = "host-1",
    state: EvidenceState = EvidenceState.OBSERVED,
    observed_at: datetime | None = EVALUATION_TIME - timedelta(minutes=1),
    fields: tuple[tuple[str, object], ...] = (("cpu_percent", 42),),
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        source_id=source_id,
        resource_type=resource_type,
        resource_id=resource_id,
        state=state,
        observed_at=observed_at,
        fields=fields,
        safe_links=(SafeLink(route="/api/history/host", label="Host history"),),
    )


def bundle(*items: EvidenceItem, uncertainties: tuple[Uncertainty, ...] = (), provenance: tuple[ProvenanceReference, ...] = ()) -> EvidenceBundle:
    return EvidenceBundle(
        schema_version="1.0",
        bundle_id="bundle-1",
        evaluation_time=EVALUATION_TIME,
        generated_at=EVALUATION_TIME,
        freshness_deadline=EVALUATION_TIME + timedelta(minutes=5),
        status=AdvisorStatus.FRESH,
        scope=AdvisorScope.OVERVIEW,
        items=items,
        uncertainties=uncertainties,
        provenance=provenance,
        coverage=(Coverage(source_id="telemetry", expected=max(1, len(items)), observed=len(items)),),
    )


def finding(*, evidence_refs: tuple[str, ...] = ("telemetry-host-1",), confidence: Confidence = Confidence.HIGH, uncertainty_refs: tuple[str, ...] = ()) -> Finding:
    return Finding(
        finding_id="finding-1",
        category=AdvisorCategory.TELEMETRY_ANOMALY,
        severity=FindingSeverity.WARNING,
        confidence=confidence,
        title="Telemetry condition observed",
        condition="Telemetry evidence indicates a bounded condition",
        rule_id="telemetry.cadence.gap",
        rule_version="1.0",
        evidence_refs=evidence_refs,
        uncertainty_refs=uncertainty_refs,
    )


def recommendation(*, finding_refs: tuple[str, ...] = ("finding-1",), evidence_refs: tuple[str, ...] = ("telemetry-host-1",), confidence: Confidence = Confidence.HIGH, uncertainty_refs: tuple[str, ...] = ()) -> Recommendation:
    return Recommendation(
        recommendation_id="recommendation-1",
        category=AdvisorCategory.TELEMETRY_ANOMALY,
        priority=3,
        status=RecommendationStatus.NEW,
        title="Review telemetry evidence",
        summary="Review the bounded telemetry history and service-health evidence",
        rationale="The recommendation is linked to the observed telemetry finding",
        confidence=confidence,
        finding_refs=finding_refs,
        evidence_refs=evidence_refs,
        uncertainty_refs=uncertainty_refs,
    )


def test_models_are_immutable_and_normalize_enums_and_utc() -> None:
    item = evidence()
    assert item.state is EvidenceState.OBSERVED
    assert item.observed_at == EVALUATION_TIME - timedelta(minutes=1)
    with pytest.raises(FrozenInstanceError):
        item.resource_id = "other"  # type: ignore[misc]

    result = finding()
    assert result.category is AdvisorCategory.TELEMETRY_ANOMALY
    assert result.severity is FindingSeverity.WARNING


def test_evidence_bounds_and_safe_links_are_enforced() -> None:
    with pytest.raises(AdvisorValidationError):
        evidence(evidence_id="bad/id")
    with pytest.raises(AdvisorValidationError):
        EvidenceItem(
            evidence_id="evidence-1",
            source_id="telemetry",
            resource_type="host",
            resource_id="host-1",
            state=EvidenceState.OBSERVED,
            observed_at=None,
        )
    with pytest.raises(AdvisorValidationError):
        SafeLink(route="https://example.com", label="External")
    with pytest.raises(AdvisorValidationError):
        EvidenceItem(
            evidence_id="evidence-1",
            source_id="telemetry",
            resource_type="host",
            resource_id="host-1",
            state=EvidenceState.OBSERVED,
            observed_at=EVALUATION_TIME,
            fields=(("cpu", float("nan")),),
        )


def test_freshness_deadlines_and_evaluation_time_require_aware_consistent_timestamps() -> None:
    with pytest.raises(AdvisorValidationError):
        evidence(observed_at=datetime(2026, 8, 22, 12, 0))
    with pytest.raises(AdvisorValidationError):
        EvidenceItem(
            evidence_id="evidence-1",
            source_id="telemetry",
            resource_type="host",
            resource_id="host-1",
            state=EvidenceState.STALE,
            observed_at=EVALUATION_TIME,
            freshness_deadline=EVALUATION_TIME - timedelta(seconds=1),
        )
    with pytest.raises(AdvisorValidationError):
        EvidenceBundle(
            schema_version="1.0",
            bundle_id="bundle-1",
            evaluation_time=EVALUATION_TIME,
            generated_at=EVALUATION_TIME + timedelta(seconds=1),
            freshness_deadline=None,
            status=AdvisorStatus.FRESH,
            scope=AdvisorScope.OVERVIEW,
        )


def test_items_fields_and_collections_have_deterministic_canonical_order() -> None:
    first = evidence("telemetry-b", resource_id="host-2", fields=(("z", 2), ("a", 1)))
    second = evidence("telemetry-a", resource_id="host-1", fields=(("a", 1), ("z", 2)))
    left = bundle(first, second)
    right = bundle(second, first)
    assert [item.evidence_id for item in left.items] == ["telemetry-a", "telemetry-b"]
    assert left.canonical_json() == right.canonical_json()
    assert left.stable_id == right.stable_id


def test_uncertainty_is_bounded_and_references_only_bundle_evidence() -> None:
    uncertainty = Uncertainty(
        uncertainty_id="uncertainty-1",
        kind=UncertaintyKind.STALE_EVIDENCE,
        summary="One source is stale",
        evidence_refs=("telemetry-host-1",),
        confidence_impact=ConfidenceImpact.HIGH_TO_MEDIUM,
        resolution_hint="Review the bounded history view",
    )
    result = bundle(evidence(), uncertainties=(uncertainty,))
    assert result.uncertainties[0].kind is UncertaintyKind.STALE_EVIDENCE
    with pytest.raises(AdvisorValidationError):
        bundle(
            evidence(),
            uncertainties=(
                Uncertainty(
                    uncertainty_id="uncertainty-2",
                    kind=UncertaintyKind.MISSING_EVIDENCE,
                    summary="A source is missing",
                    evidence_refs=("not-in-bundle",),
                ),
            ),
        )
    with pytest.raises(AdvisorValidationError):
        Uncertainty(
            uncertainty_id="uncertainty-3",
            kind=UncertaintyKind.MISSING_EVIDENCE,
            summary="Run systemctl restart now",
        )


def test_findings_require_evidence_and_uncertainty_for_reduced_confidence() -> None:
    with pytest.raises(AdvisorValidationError):
        finding(evidence_refs=())
    with pytest.raises(AdvisorValidationError):
        finding(confidence=Confidence.LOW)
    uncertainty = Uncertainty(
        uncertainty_id="uncertainty-1",
        kind=UncertaintyKind.STALE_EVIDENCE,
        summary="Evidence is stale",
        evidence_refs=("telemetry-host-1",),
        confidence_impact=ConfidenceImpact.TO_UNKNOWN,
    )
    low = finding(confidence=Confidence.LOW, uncertainty_refs=("uncertainty-1",))
    result = bundle(evidence(), uncertainties=(uncertainty,))
    result.validate_finding(low)
    with pytest.raises(AdvisorValidationError):
        result.validate_finding(finding(evidence_refs=("missing-evidence",)))


def test_provenance_is_metadata_only_and_must_match_bundle_source() -> None:
    provenance = ProvenanceReference(
        provenance_ref_id="provenance-1",
        source_id="telemetry",
        provenance_id="provenance-envelope-1",
        key_id="key-1",
        signature_verified=True,
        plan_id="plan-1",
        plan_digest="a" * 64,
        observed_at=EVALUATION_TIME,
    )
    result = bundle(evidence(), provenance=(provenance,))
    assert result.provenance[0].signature_verified is True
    with pytest.raises(AdvisorValidationError):
        bundle(
            evidence(),
            provenance=(
                ProvenanceReference(
                    provenance_ref_id="provenance-2",
                    source_id="events",
                    provenance_id="provenance-envelope-2",
                    key_id="key-2",
                    signature_verified=False,
                ),
            ),
        )


def test_recommendations_are_traceable_and_cannot_express_operations() -> None:
    current_bundle = bundle(evidence())
    current_finding = finding()
    current_recommendation = recommendation()
    response = AdvisorResponse(
        schema_version="1.0",
        request_id="request-1",
        available=True,
        status=AdvisorStatus.FRESH,
        evaluation_time=EVALUATION_TIME,
        generated_at=EVALUATION_TIME,
        freshness_deadline=EVALUATION_TIME + timedelta(minutes=5),
        scope=AdvisorScope.OVERVIEW,
        findings=(current_finding,),
        recommendations=(current_recommendation,),
    )
    response.validate_against_bundle(current_bundle)
    assert "restart" not in response.canonical_json().casefold()

    with pytest.raises(AdvisorValidationError):
        Recommendation(
            recommendation_id="recommendation-2",
            category=AdvisorCategory.TELEMETRY_ANOMALY,
            priority=1,
            status=RecommendationStatus.NEW,
            title="Restart service",
            summary="Restart the telemetry service now",
            rationale="Execute the command",
            confidence=Confidence.HIGH,
            finding_refs=("finding-1",),
            evidence_refs=("telemetry-host-1",),
        )
    with pytest.raises(AdvisorValidationError):
        response.validate_against_bundle(bundle(evidence(evidence_id="different-evidence")))


def test_recommendation_must_reference_existing_finding_and_its_evidence() -> None:
    with pytest.raises(AdvisorValidationError):
        AdvisorResponse(
            schema_version="1.0",
            request_id="request-1",
            available=True,
            status=AdvisorStatus.FRESH,
            evaluation_time=EVALUATION_TIME,
            generated_at=EVALUATION_TIME,
            freshness_deadline=None,
            scope=AdvisorScope.OVERVIEW,
            findings=(),
            recommendations=(recommendation(),),
        )
    with pytest.raises(AdvisorValidationError):
        recommendation(evidence_refs=("not-finding-evidence",)).validate_against_findings((finding(),))


def test_resource_history_summary_is_bounded_ordered_and_peak_paired() -> None:
    peak_at = EVALUATION_TIME - timedelta(seconds=30)
    summaries = (
        ResourceHistorySummary("disk_percent", ResourceHistorySummaryState.COMPLETE, 6, 300, 60, 56.1, peak_at),
        ResourceHistorySummary("cpu_percent", "complete", 6, 300, 60, 35.1, peak_at),
        ResourceHistorySummary("memory_percent", ResourceHistorySummaryState.COMPLETE, 6, 300, 60, 56.4, peak_at),
    )
    response = AdvisorResponse(
        schema_version="1.0",
        request_id="request-summary",
        available=True,
        status=AdvisorStatus.FRESH,
        evaluation_time=EVALUATION_TIME,
        generated_at=EVALUATION_TIME,
        freshness_deadline=EVALUATION_TIME + timedelta(minutes=5),
        scope=AdvisorScope.HOST,
        resource_history_summary=summaries,
    )

    assert [item.metric for item in response.resource_history_summary] == ["cpu_percent", "memory_percent", "disk_percent"]
    assert response.resource_history_summary[0].peak_value == 35.1
    assert response.resource_history_summary[0].peak_observed_at == peak_at
    assert response.canonical()["resource_history_summary"][0]["metric"] == "cpu_percent"
    assert response.stable_id == response.stable_id

    with pytest.raises(AdvisorValidationError):
        ResourceHistorySummary("network_percent", "complete", 6, 300, 60)
    with pytest.raises(AdvisorValidationError):
        ResourceHistorySummary("cpu_percent", "complete", 129, 300, 60)
    with pytest.raises(AdvisorValidationError):
        ResourceHistorySummary("cpu_percent", "complete", 6, 300, 60, 35.1)


def test_response_orders_outputs_and_enforces_availability_state() -> None:
    response = AdvisorResponse(
        schema_version="1.0",
        request_id="request-1",
        available=False,
        status=AdvisorStatus.UNAVAILABLE,
        evaluation_time=EVALUATION_TIME,
        generated_at=EVALUATION_TIME,
        freshness_deadline=None,
        scope=AdvisorScope.OVERVIEW,
    )
    assert response.canonical_json().find('"status":"unavailable"') >= 0
    assert response.stable_id == response.stable_id
    with pytest.raises(AdvisorValidationError):
        AdvisorResponse(
            schema_version="1.0",
            request_id="request-2",
            available=True,
            status=AdvisorStatus.ERROR,
            evaluation_time=EVALUATION_TIME,
            generated_at=EVALUATION_TIME,
            freshness_deadline=None,
            scope=AdvisorScope.OVERVIEW,
        )


def test_stale_unavailable_invalid_states_remain_explicit() -> None:
    stale = evidence(state=EvidenceState.STALE)
    unavailable = evidence("telemetry-unavailable", state=EvidenceState.UNAVAILABLE, observed_at=None, fields=())
    invalid = evidence("telemetry-invalid", state=EvidenceState.INVALID, observed_at=None, fields=())
    result = bundle(stale, unavailable, invalid)
    assert [item.state for item in result.items] == [EvidenceState.STALE, EvidenceState.INVALID, EvidenceState.UNAVAILABLE]


@pytest.mark.parametrize(
    "unsafe_text",
    (
        "restart the service",
        "run this command",
        "approve this plan",
        "please deploy the project",
        "you should delete the rows",
        "execute the operation",
    ),
)
def test_explicit_action_directives_are_rejected(unsafe_text: str) -> None:
    with pytest.raises(AdvisorValidationError):
        Recommendation(
            recommendation_id="recommendation-unsafe",
            category=AdvisorCategory.TELEMETRY_ANOMALY,
            priority=1,
            status=RecommendationStatus.SUPERSEDED,
            title=unsafe_text,
            summary="Review bounded evidence",
            rationale="Historical projection only",
            confidence=Confidence.HIGH,
            finding_refs=(),
            evidence_refs=(),
        )


@pytest.mark.parametrize(
    "neutral_text",
    (
        "The sampling start time is recorded in the evidence",
        "The stop time is missing from the bounded history",
        "The run count changed between observations",
        "The deployment revision is different from the baseline",
        "The project state change is explicitly observed",
        "The delete count is zero for this retention interval",
        "The modified timestamp is preserved by the source",
    ),
)
def test_neutral_descriptive_prose_with_sensitive_verbs_is_accepted(neutral_text: str) -> None:
    recommendation = Recommendation(
        recommendation_id="recommendation-neutral",
        category=AdvisorCategory.PROJECT_STATE_CHANGE,
        priority=3,
        status=RecommendationStatus.SUPERSEDED,
        title="Review project evidence",
        summary=neutral_text,
        rationale="The text describes bounded evidence without instructing an operation",
        confidence=Confidence.HIGH,
        finding_refs=(),
        evidence_refs=(),
    )
    assert recommendation.summary == neutral_text


def test_recommendation_statuses_remain_read_only_and_bounded() -> None:
    superseded = Recommendation(
        recommendation_id="recommendation-superseded",
        category=AdvisorCategory.PROJECT_STATE_CHANGE,
        priority=5,
        status=RecommendationStatus.SUPERSEDED,
        title="Review prior project change",
        summary="Review the prior bounded project-state evidence",
        rationale="This is a historical projection only",
        confidence=Confidence.HIGH,
        finding_refs=(),
        evidence_refs=(),
    )
    assert superseded.status is RecommendationStatus.SUPERSEDED
    with pytest.raises(AdvisorValidationError):
        Recommendation(
            recommendation_id="recommendation-bad-priority",
            category=AdvisorCategory.PROJECT_STATE_CHANGE,
            priority=6,
            status=RecommendationStatus.SUPERSEDED,
            title="Review project state",
            summary="Review bounded project evidence",
            rationale="Historical projection",
            confidence=Confidence.HIGH,
            finding_refs=(),
            evidence_refs=(),
        )
