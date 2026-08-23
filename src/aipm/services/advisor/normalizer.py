"""Deterministic normalization of bounded read-only observations.

This module is deliberately an adapter boundary: it accepts only explicit mapping-shaped
observation records and constructs the immutable MC-6.13 Phase 1 contracts.  It does not
collect observations, read a clock, or access any runtime, provider, or persistence system.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from aipm.models.advisor import (
    AdvisorScope,
    AdvisorStatus,
    AdvisorValidationError,
    ConfidenceImpact,
    Coverage,
    EvidenceBundle,
    EvidenceItem,
    EvidenceState,
    ProvenanceReference,
    SafeLink,
    Uncertainty,
    UncertaintyKind,
)


DEFAULT_SCHEMA_VERSION = "1.0"
MAX_NORMALIZABLE_ITEMS = 128
MAX_EXPECTED_SOURCES = 32
_SUPPORTED_STATES = {
    "observed": EvidenceState.OBSERVED,
    "fresh": EvidenceState.OBSERVED,
    "stale": EvidenceState.STALE,
    "unavailable": EvidenceState.UNAVAILABLE,
    "error": EvidenceState.UNAVAILABLE,
    "not_observed": EvidenceState.NOT_OBSERVED,
    "never_sampled": EvidenceState.NOT_OBSERVED,
    "invalid": EvidenceState.INVALID,
}


class NormalizationError(ValueError):
    """Raised when the normalizer contract itself cannot be satisfied."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _aware_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise NormalizationError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any, name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware_datetime(value, name)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise NormalizationError(f"Invalid {name}") from exc
        return _aware_datetime(parsed, name)
    raise NormalizationError(f"Invalid {name}")


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise NormalizationError(f"{name} must be a mapping")
    return value


def _text_key(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _source_order_key(value: Any) -> tuple[str, str, str, str, str]:
    if not isinstance(value, Mapping):
        return ("", "", "", "", "")
    observed_at = value.get("observed_at")
    if isinstance(observed_at, datetime):
        timestamp = observed_at.isoformat()
    else:
        timestamp = observed_at if isinstance(observed_at, str) else ""
    return (
        _text_key(value.get("source_id")),
        _text_key(value.get("resource_type")),
        _text_key(value.get("resource_id")),
        _text_key(value.get("evidence_id")),
        timestamp,
    )


def _safe_expected_sources(value: Iterable[str] | Mapping[str, int] | None) -> dict[str, int]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        pairs = list(value.items())
    else:
        pairs = [(source, 1) for source in value]
    if len(pairs) > MAX_EXPECTED_SOURCES:
        raise NormalizationError("Too many expected sources")
    result: dict[str, int] = {}
    for source, count in pairs:
        if not isinstance(source, str) or not source:
            raise NormalizationError("Invalid expected source")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise NormalizationError("Expected source count must be positive")
        if source in result:
            raise NormalizationError("Duplicate expected source")
        result[source] = count
    return result


def _state(value: Any) -> EvidenceState:
    raw = value.value if isinstance(value, EvidenceState) else value
    if not isinstance(raw, str) or raw not in _SUPPORTED_STATES:
        raise NormalizationError("Invalid observation state")
    return _SUPPORTED_STATES[raw]


def _fields(record: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    raw = record.get("fields", record.get("data", {}))
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise NormalizationError("Observation fields must be a string-keyed mapping")
    # EvidenceItem performs the scalar, text, and key safety validation. Sorting here
    # ensures source mapping order is never an implicit determinism dependency.
    return tuple((key, raw[key]) for key in sorted(raw))


def _links(record: Mapping[str, Any]) -> tuple[SafeLink, ...]:
    raw = record.get("safe_links", ())
    if not isinstance(raw, (tuple, list)):
        raise NormalizationError("safe_links must be a bounded sequence")
    links: list[SafeLink] = []
    for value in raw:
        if isinstance(value, SafeLink):
            links.append(value)
        elif isinstance(value, Mapping):
            links.append(SafeLink(route=value.get("route"), label=value.get("label")))
        else:
            raise NormalizationError("Invalid safe link")
    return tuple(links)


def _provenance(records: Any) -> tuple[ProvenanceReference, ...]:
    if records is None:
        return ()
    if not isinstance(records, (tuple, list)):
        raise NormalizationError("provenance must be a bounded sequence")
    result: list[ProvenanceReference] = []
    for value in records:
        if isinstance(value, ProvenanceReference):
            result.append(value)
            continue
        record = _mapping(value, "provenance reference")
        result.append(ProvenanceReference(**dict(record)))
    return tuple(result)


def _uncertainty(
    kind: UncertaintyKind,
    *,
    evidence_refs: Sequence[str] = (),
    source_id: str | None = None,
    summary: str,
    impact: ConfidenceImpact,
) -> Uncertainty:
    identity = {
        "kind": kind.value,
        "evidence_refs": sorted(evidence_refs),
        "source_id": source_id,
    }
    return Uncertainty(
        uncertainty_id=f"uncertainty-{_digest(identity)[:32]}",
        kind=kind,
        summary=summary,
        evidence_refs=tuple(evidence_refs),
        confidence_impact=impact,
    )


class EvidenceNormalizer:
    """Normalize only bounded, already-collected observation records.

    ``evaluation_time`` is always supplied by the caller.  The normalizer never reads
    wall-clock or process state and never calls an observation source.
    """

    def __init__(
        self,
        *,
        schema_version: str = DEFAULT_SCHEMA_VERSION,
        max_items: int = MAX_NORMALIZABLE_ITEMS,
    ) -> None:
        if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= MAX_NORMALIZABLE_ITEMS:
            raise NormalizationError("max_items is outside the supported bound")
        self.schema_version = schema_version
        self.max_items = max_items

    def normalize(
        self,
        observations: Sequence[Mapping[str, Any]],
        *,
        evaluation_time: datetime,
        scope: AdvisorScope = AdvisorScope.OVERVIEW,
        expected_sources: Iterable[str] | Mapping[str, int] | None = None,
        provenance: Iterable[Mapping[str, Any] | ProvenanceReference] | None = None,
    ) -> EvidenceBundle:
        evaluation_time = _aware_datetime(evaluation_time, "evaluation_time")
        if not isinstance(observations, (tuple, list)):
            raise NormalizationError("observations must be a bounded sequence")
        expected = _safe_expected_sources(expected_sources)
        if len(observations) > MAX_NORMALIZABLE_ITEMS * 4:
            raise NormalizationError("Input observations exceed the hard bound")

        ordered = sorted(enumerate(observations), key=lambda pair: (_source_order_key(pair[1]), pair[0]))
        items: list[EvidenceItem] = []
        uncertainties: list[Uncertainty] = []
        source_counts: dict[str, dict[EvidenceState, int]] = {}
        conflict_groups: dict[tuple[str, str, str], list[EvidenceItem]] = {}

        for _, raw in ordered[: self.max_items]:
            item, item_uncertainties = self._normalize_one(raw, evaluation_time)
            uncertainties.extend(item_uncertainties)
            if item is None:
                continue
            items.append(item)
            source_counts.setdefault(item.source_id, {}).setdefault(item.state, 0)
            source_counts[item.source_id][item.state] += 1
            conflict_groups.setdefault((item.source_id, item.resource_type, item.resource_id), []).append(item)

        omitted = max(0, len(ordered) - self.max_items)
        if omitted:
            uncertainties.append(
                _uncertainty(
                    UncertaintyKind.SCOPE_LIMITED,
                    summary="Some source observations were omitted because the bounded evidence limit was reached",
                    impact=ConfidenceImpact.WITHHELD,
                )
            )

        for group in conflict_groups.values():
            states = {item.state for item in group}
            if len(group) > 1 and len(states) > 1:
                refs = tuple(item.evidence_id for item in group)
                uncertainties.append(
                    _uncertainty(
                        UncertaintyKind.CONFLICTING_EVIDENCE,
                        evidence_refs=refs,
                        source_id=group[0].source_id,
                        summary="Multiple source states conflict for the same normalized resource",
                        impact=ConfidenceImpact.TO_UNKNOWN,
                    )
                )

        item_sources = {item.source_id for item in items}
        all_sources = sorted(item_sources | set(expected))
        coverage: list[Coverage] = []
        for source_id in all_sources:
            counts = source_counts.get(source_id, {})
            actual = sum(counts.values())
            expected_count = max(expected.get(source_id, actual), actual)
            observed = counts.get(EvidenceState.OBSERVED, 0)
            stale = counts.get(EvidenceState.STALE, 0)
            unavailable = counts.get(EvidenceState.UNAVAILABLE, 0) + counts.get(EvidenceState.NOT_OBSERVED, 0)
            invalid = counts.get(EvidenceState.INVALID, 0)
            omitted_count = max(0, expected_count - observed - stale - unavailable - invalid)
            coverage.append(
                Coverage(
                    source_id=source_id,
                    expected=expected_count,
                    observed=observed,
                    stale=stale,
                    unavailable=unavailable,
                    invalid=invalid,
                    omitted=omitted_count,
                )
            )
            if omitted_count:
                uncertainties.append(
                    _uncertainty(
                        UncertaintyKind.MISSING_EVIDENCE,
                        source_id=source_id,
                        summary="Expected source evidence was not supplied to the normalizer",
                        impact=ConfidenceImpact.WITHHELD,
                    )
                )

        # Deduplicate only by deterministic identity.  References and summaries are
        # immutable, so collapsing repeated causes cannot hide a distinct evidence ID.
        provenance_values = _provenance(provenance)
        item_sources = {item.source_id for item in items}
        provenance_values = tuple(reference for reference in provenance_values if reference.source_id in item_sources)
        provenance_by_id = {reference.provenance_ref_id: reference for reference in provenance_values}
        for item in items:
            for reference_id in item.provenance_refs:
                reference = provenance_by_id.get(reference_id)
                if reference is None or not reference.signature_verified:
                    uncertainties.append(
                        _uncertainty(
                            UncertaintyKind.PROVENANCE_UNVERIFIED,
                            evidence_refs=(item.evidence_id,),
                            source_id=item.source_id,
                            summary="The source provenance metadata is absent or not verified",
                            impact=ConfidenceImpact.WITHHELD,
                        )
                    )

        uncertainty_map = {item.uncertainty_id: item for item in uncertainties}
        uncertainties = [uncertainty_map[key] for key in sorted(uncertainty_map)]

        deadline_values = [item.freshness_deadline for item in items if item.freshness_deadline is not None]
        bundle_deadline = min(deadline_values) if deadline_values else None
        status = self._status(items, uncertainties, expected)
        identity = {
            "schema_version": self.schema_version,
            "evaluation_time": evaluation_time.isoformat(),
            "scope": scope.value if isinstance(scope, AdvisorScope) else scope,
            "items": [item.canonical() for item in items],
            "uncertainties": [item.canonical() for item in uncertainties],
            "provenance": [item.canonical() for item in provenance_values],
            "coverage": [item.canonical() for item in coverage],
        }
        bundle_id = f"bundle-{_digest(identity)[:32]}"
        try:
            return EvidenceBundle(
                schema_version=self.schema_version,
                bundle_id=bundle_id,
                evaluation_time=evaluation_time,
                generated_at=evaluation_time,
                freshness_deadline=bundle_deadline,
                status=status,
                scope=scope,
                items=tuple(items),
                uncertainties=tuple(uncertainties),
                provenance=provenance_values,
                coverage=tuple(coverage),
            )
        except AdvisorValidationError as exc:
            raise NormalizationError("Normalized evidence violates the Phase 1 contract") from exc

    def _normalize_one(
        self,
        raw: Mapping[str, Any],
        evaluation_time: datetime,
    ) -> tuple[EvidenceItem | None, list[Uncertainty]]:
        if not isinstance(raw, Mapping):
            return None, [
                _uncertainty(
                    UncertaintyKind.INVALID_EVIDENCE,
                    summary="A source observation was not a supported mapping-shaped record",
                    impact=ConfidenceImpact.TO_UNKNOWN,
                )
            ]
        source_id = raw.get("source_id")
        resource_type = raw.get("resource_type")
        resource_id = raw.get("resource_id")
        evidence_id = raw.get("evidence_id")
        base_identity = {
            "source_id": source_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "evidence_id": evidence_id,
        }
        try:
            observed_at = _parse_datetime(raw.get("observed_at"), "observed_at")
            deadline = _parse_datetime(raw.get("freshness_deadline"), "freshness_deadline")
            max_age = raw.get("max_age_seconds")
            if max_age is not None:
                if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age < 0:
                    raise NormalizationError("Invalid max_age_seconds")
                if observed_at is not None:
                    derived_deadline = observed_at + timedelta(seconds=max_age)
                    if deadline is not None and deadline != derived_deadline:
                        raise NormalizationError("Conflicting freshness deadlines")
                    deadline = derived_deadline
            state = _state(raw.get("state", "observed"))
            if state is EvidenceState.OBSERVED:
                if observed_at is None:
                    raise NormalizationError("Observed evidence requires observed_at")
                if observed_at > evaluation_time:
                    raise NormalizationError("Observed evidence cannot be from the future")
                if deadline is not None and evaluation_time > deadline:
                    state = EvidenceState.STALE
            elif state is EvidenceState.STALE and observed_at is not None and observed_at > evaluation_time:
                raise NormalizationError("Stale evidence cannot be from the future")
            if evidence_id is None:
                evidence_id = f"evidence-{_digest({**base_identity, 'observed_at': observed_at.isoformat() if observed_at else None, 'state': state.value, 'fields': raw.get('fields', raw.get('data', {}))})[:32]}"
            # Preserve safe, bounded source fields for valid degraded states so a
            # stale or unavailable value remains distinguishable from absent data.
            # Explicitly invalid evidence never copies source fields.
            fields = () if state is EvidenceState.INVALID else _fields(raw)
            safe_links = _links(raw)
            item = EvidenceItem(
                evidence_id=evidence_id,
                source_id=source_id,
                resource_type=resource_type,
                resource_id=resource_id,
                state=state,
                observed_at=observed_at,
                freshness_deadline=deadline,
                fields=fields,
                safe_links=safe_links,
                source_revision=raw.get("source_revision"),
                provenance_refs=tuple(raw.get("provenance_refs", ())),
            )
            item_uncertainties: list[Uncertainty] = []
            if state is EvidenceState.STALE:
                item_uncertainties.append(
                    _uncertainty(
                        UncertaintyKind.STALE_EVIDENCE,
                        evidence_refs=(item.evidence_id,),
                        source_id=item.source_id,
                        summary="The source observation exceeded its declared freshness boundary",
                        impact=ConfidenceImpact.HIGH_TO_MEDIUM,
                    )
                )
            elif state is EvidenceState.UNAVAILABLE:
                item_uncertainties.append(
                    _uncertainty(
                        UncertaintyKind.UNAVAILABLE_SOURCE,
                        evidence_refs=(item.evidence_id,),
                        source_id=item.source_id,
                        summary="The source reported that this observation is unavailable",
                        impact=ConfidenceImpact.TO_UNKNOWN,
                    )
                )
            elif state is EvidenceState.NOT_OBSERVED:
                item_uncertainties.append(
                    _uncertainty(
                        UncertaintyKind.MISSING_EVIDENCE,
                        evidence_refs=(item.evidence_id,),
                        source_id=item.source_id,
                        summary="The source has not supplied an observation for this resource",
                        impact=ConfidenceImpact.WITHHELD,
                    )
                )
            return item, item_uncertainties
        except (AdvisorValidationError, NormalizationError, TypeError, ValueError):
            # Preserve valid backend identity while making invalid source content
            # explicit.  If identity itself is invalid, no unsafe pseudo-item is made.
            try:
                invalid_id = evidence_id or f"evidence-invalid-{_digest(base_identity)[:32]}"
                invalid_item = EvidenceItem(
                    evidence_id=invalid_id,
                    source_id=source_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    state=EvidenceState.INVALID,
                )
            except (AdvisorValidationError, TypeError, ValueError):
                invalid_item = None
            refs = (invalid_item.evidence_id,) if invalid_item is not None else ()
            uncertainty = _uncertainty(
                UncertaintyKind.INVALID_EVIDENCE,
                evidence_refs=refs,
                source_id=source_id if isinstance(source_id, str) else None,
                summary="A source observation failed bounded normalization and was not treated as factual evidence",
                impact=ConfidenceImpact.TO_UNKNOWN,
            )
            return invalid_item, [uncertainty]

    @staticmethod
    def _status(
        items: Sequence[EvidenceItem],
        uncertainties: Sequence[Uncertainty],
        expected: Mapping[str, int],
    ) -> AdvisorStatus:
        states = {item.state for item in items}
        if not items:
            return AdvisorStatus.UNAVAILABLE if expected or uncertainties else AdvisorStatus.FRESH
        if states <= {EvidenceState.OBSERVED} and not uncertainties:
            return AdvisorStatus.FRESH
        if states <= {EvidenceState.STALE}:
            return AdvisorStatus.STALE
        if EvidenceState.OBSERVED in states:
            return AdvisorStatus.PARTIAL
        if EvidenceState.INVALID in states:
            return AdvisorStatus.ERROR
        return AdvisorStatus.UNAVAILABLE


def normalize_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    evaluation_time: datetime,
    scope: AdvisorScope = AdvisorScope.OVERVIEW,
    expected_sources: Iterable[str] | Mapping[str, int] | None = None,
    provenance: Iterable[Mapping[str, Any] | ProvenanceReference] | None = None,
    max_items: int = MAX_NORMALIZABLE_ITEMS,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> EvidenceBundle:
    """Functional convenience wrapper around :class:`EvidenceNormalizer`."""

    return EvidenceNormalizer(schema_version=schema_version, max_items=max_items).normalize(
        observations,
        evaluation_time=evaluation_time,
        scope=scope,
        expected_sources=expected_sources,
        provenance=provenance,
    )


__all__ = ["DEFAULT_SCHEMA_VERSION", "EvidenceNormalizer", "NormalizationError", "normalize_observations"]
