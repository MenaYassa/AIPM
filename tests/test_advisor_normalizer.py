from datetime import datetime, timedelta, timezone

import pytest

from aipm.models.advisor import AdvisorScope, AdvisorStatus, EvidenceState, UncertaintyKind
from aipm.services.advisor.normalizer import EvidenceNormalizer, NormalizationError, normalize_observations


EVALUATION_TIME = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
OBSERVED_AT = EVALUATION_TIME - timedelta(minutes=1)


def observation(
    *,
    evidence_id: str | None = "telemetry-host-1",
    source_id: str = "telemetry",
    resource_type: str = "host",
    resource_id: str = "host-1",
    state: str = "observed",
    observed_at: datetime | str | None = OBSERVED_AT,
    fields: dict[str, object] | None = None,
    **extra: object,
) -> dict[str, object]:
    result: dict[str, object] = {
        "evidence_id": evidence_id,
        "source_id": source_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "state": state,
        "observed_at": observed_at,
        "fields": fields if fields is not None else {"cpu_percent": 42.0, "available": True},
        "safe_links": [{"route": "/api/history/host", "label": "Host history"}],
    }
    result.update(extra)
    return result


def test_complete_observation_normalizes_to_bounded_immutable_bundle() -> None:
    bundle = normalize_observations(
        [
            observation(
                source_revision="revision-1",
                max_age_seconds=300,
            )
        ],
        evaluation_time=EVALUATION_TIME,
        scope=AdvisorScope.HOST,
        expected_sources={"telemetry": 1},
    )

    assert bundle.status is AdvisorStatus.FRESH
    assert bundle.scope is AdvisorScope.HOST
    assert bundle.generated_at == EVALUATION_TIME
    assert bundle.items[0].state is EvidenceState.OBSERVED
    assert bundle.items[0].freshness_deadline == OBSERVED_AT + timedelta(seconds=300)
    assert bundle.items[0].fields == (("available", True), ("cpu_percent", 42.0))
    assert bundle.items[0].safe_links[0].route == "/api/history/host"
    assert bundle.stable_id == bundle.stable_id


def test_missing_expected_source_is_explicit_and_unavailable() -> None:
    bundle = normalize_observations([], evaluation_time=EVALUATION_TIME, expected_sources=("telemetry",))

    assert bundle.status is AdvisorStatus.UNAVAILABLE
    assert bundle.coverage[0].expected == 1
    assert bundle.coverage[0].omitted == 1
    assert bundle.uncertainties[0].kind is UncertaintyKind.MISSING_EVIDENCE


def test_stale_observation_is_derived_from_evaluation_time_and_declared_age() -> None:
    bundle = normalize_observations(
        [observation(max_age_seconds=30)],
        evaluation_time=EVALUATION_TIME,
    )

    assert bundle.items[0].state is EvidenceState.STALE
    assert bundle.status is AdvisorStatus.STALE
    assert any(item.kind is UncertaintyKind.STALE_EVIDENCE for item in bundle.uncertainties)


def test_bounded_fields_survive_derived_stale_state() -> None:
    bundle = normalize_observations(
        [observation(max_age_seconds=30, fields={"cpu_percent": 91.0, "sample_count": 4})],
        evaluation_time=EVALUATION_TIME,
    )

    assert bundle.items[0].state is EvidenceState.STALE
    assert bundle.items[0].fields == (("cpu_percent", 91.0), ("sample_count", 4))


def test_bounded_fields_survive_explicit_unavailable_state() -> None:
    bundle = normalize_observations(
        [
            observation(
                state="unavailable",
                observed_at=None,
                fields={"last_known_state": "degraded", "sample_count": 4},
            )
        ],
        evaluation_time=EVALUATION_TIME,
    )

    assert bundle.items[0].state is EvidenceState.UNAVAILABLE
    assert bundle.items[0].fields == (("last_known_state", "degraded"), ("sample_count", 4))


def test_explicit_invalid_state_never_copies_source_fields() -> None:
    bundle = normalize_observations(
        [observation(state="invalid", fields={"last_known_state": "degraded", "sample_count": 4})],
        evaluation_time=EVALUATION_TIME,
    )

    assert bundle.items[0].state is EvidenceState.INVALID
    assert bundle.items[0].fields == ()


def test_degraded_field_serialization_and_stable_id_are_deterministic() -> None:
    records = [
        observation(evidence_id="telemetry-unavailable", resource_id="host-unavailable", state="unavailable", observed_at=None, fields={"sample_count": 4}),
        observation(evidence_id="telemetry-stale", resource_id="host-stale", max_age_seconds=30, fields={"cpu_percent": 91.0}),
    ]
    first = normalize_observations(records, evaluation_time=EVALUATION_TIME)
    second = normalize_observations(list(reversed(records)), evaluation_time=EVALUATION_TIME)

    assert first.canonical_json() == second.canonical_json()
    assert first.stable_id == second.stable_id


def test_unavailable_and_not_observed_states_are_preserved() -> None:
    bundle = normalize_observations(
        [
            observation(evidence_id="telemetry-unavailable", resource_id="host-unavailable", state="unavailable", observed_at=None),
            observation(evidence_id="telemetry-missing", resource_id="host-missing", state="never_sampled", observed_at=None),
        ],
        evaluation_time=EVALUATION_TIME,
    )

    assert {item.state for item in bundle.items} == {EvidenceState.UNAVAILABLE, EvidenceState.NOT_OBSERVED}
    assert {item.kind for item in bundle.uncertainties} == {
        UncertaintyKind.UNAVAILABLE_SOURCE,
        UncertaintyKind.MISSING_EVIDENCE,
    }
    assert bundle.status is AdvisorStatus.UNAVAILABLE


def test_invalid_source_is_retained_as_invalid_evidence_not_silently_dropped() -> None:
    bundle = normalize_observations(
        [observation(fields={"unsafe": float("nan")})],
        evaluation_time=EVALUATION_TIME,
    )

    assert len(bundle.items) == 1
    assert bundle.items[0].state is EvidenceState.INVALID
    assert any(item.kind is UncertaintyKind.INVALID_EVIDENCE for item in bundle.uncertainties)
    assert bundle.status is AdvisorStatus.ERROR


def test_conflicting_states_create_explicit_conflicting_uncertainty() -> None:
    bundle = normalize_observations(
        [
            observation(evidence_id="telemetry-host-fresh", state="observed"),
            observation(evidence_id="telemetry-host-stale", state="stale"),
        ],
        evaluation_time=EVALUATION_TIME,
    )

    conflict = [item for item in bundle.uncertainties if item.kind is UncertaintyKind.CONFLICTING_EVIDENCE]
    assert len(conflict) == 1
    assert set(conflict[0].evidence_refs) == {"telemetry-host-fresh", "telemetry-host-stale"}
    assert bundle.status is AdvisorStatus.PARTIAL


def test_collection_limit_is_deterministic_and_explicit() -> None:
    records = [observation(evidence_id=f"telemetry-host-{index}", resource_id=f"host-{index}") for index in range(3)]
    bundle = EvidenceNormalizer(max_items=2).normalize(records, evaluation_time=EVALUATION_TIME)

    assert len(bundle.items) == 2
    assert any(item.kind is UncertaintyKind.SCOPE_LIMITED for item in bundle.uncertainties)
    with pytest.raises(NormalizationError):
        EvidenceNormalizer(max_items=0)


def test_input_permutation_has_identical_order_serialization_and_ids() -> None:
    records = [
        observation(evidence_id="telemetry-z", resource_id="z"),
        observation(evidence_id="telemetry-a", resource_id="a"),
    ]
    first = normalize_observations(records, evaluation_time=EVALUATION_TIME)
    second = normalize_observations(list(reversed(records)), evaluation_time=EVALUATION_TIME)

    assert first.canonical_json() == second.canonical_json()
    assert first.stable_id == second.stable_id
    assert [item.evidence_id for item in first.items] == ["telemetry-a", "telemetry-z"]


def test_string_timestamps_are_utc_normalized_without_clock_reads() -> None:
    bundle = normalize_observations(
        [observation(observed_at="2026-08-23T11:59:00+01:00")],
        evaluation_time=EVALUATION_TIME,
    )

    assert bundle.items[0].observed_at == datetime(2026, 8, 23, 10, 59, tzinfo=timezone.utc)


def test_safe_link_and_unsafe_field_validation_fail_closed() -> None:
    unsafe_link = normalize_observations(
        [observation(safe_links=[{"route": "https://example.com", "label": "external"}])],
        evaluation_time=EVALUATION_TIME,
    )
    assert unsafe_link.items[0].state is EvidenceState.INVALID
    assert any(item.kind is UncertaintyKind.INVALID_EVIDENCE for item in unsafe_link.uncertainties)

    unsafe_field = normalize_observations(
        [observation(fields={"command": "run this command"})],
        evaluation_time=EVALUATION_TIME,
    )
    assert unsafe_field.items[0].state is EvidenceState.INVALID
    assert any(item.kind is UncertaintyKind.INVALID_EVIDENCE for item in unsafe_field.uncertainties)


def test_verified_provenance_is_preserved_as_metadata_only() -> None:
    bundle = normalize_observations(
        [observation(provenance_refs=("prov-1",))],
        evaluation_time=EVALUATION_TIME,
        provenance=[
            {
                "provenance_ref_id": "prov-1",
                "source_id": "telemetry",
                "provenance_id": "provenance-1",
                "key_id": "key-1",
                "signature_verified": True,
                "observed_at": OBSERVED_AT,
            }
        ],
    )

    assert bundle.provenance[0].provenance_ref_id == "prov-1"
    assert bundle.provenance[0].signature_verified is True
    assert bundle.items[0].provenance_refs == ("prov-1",)
    assert bundle.canonical()["provenance"][0]["signature_verified"] is True


def test_unverified_or_missing_provenance_is_explicitly_uncertain() -> None:
    bundle = normalize_observations(
        [observation(provenance_refs=("prov-1",))],
        evaluation_time=EVALUATION_TIME,
        provenance=[
            {
                "provenance_ref_id": "prov-1",
                "source_id": "telemetry",
                "provenance_id": "provenance-1",
                "key_id": "key-1",
                "signature_verified": False,
            }
        ],
    )

    assert bundle.provenance[0].signature_verified is False
    assert any(item.kind is UncertaintyKind.PROVENANCE_UNVERIFIED for item in bundle.uncertainties)


def test_evaluation_time_is_mandatory_timezone_aware_and_no_implicit_now() -> None:
    with pytest.raises(NormalizationError):
        normalize_observations([observation()], evaluation_time=datetime(2026, 8, 23, 12, 0))
    future = normalize_observations(
        [observation(observed_at=EVALUATION_TIME + timedelta(seconds=1))],
        evaluation_time=EVALUATION_TIME,
    )
    assert future.items[0].state is EvidenceState.INVALID


def test_duplicate_ids_and_unsupported_input_are_rejected_or_explicit() -> None:
    with pytest.raises(NormalizationError):
        normalize_observations([observation(), observation()], evaluation_time=EVALUATION_TIME)

    with pytest.raises(NormalizationError):
        normalize_observations({"not": "a sequence"}, evaluation_time=EVALUATION_TIME)  # type: ignore[arg-type]
