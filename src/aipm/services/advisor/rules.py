"""Pure deterministic MC-6.13 Phase 3 advisor rules.

This module consumes only immutable normalized evidence.  It does not collect
evidence or reach any infrastructure, runtime, provider, or authority boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from aipm.models.advisor import (
    AdvisorCategory,
    AdvisorResponse,
    AdvisorStatus,
    AdvisorValidationError,
    Confidence,
    ConfidenceImpact,
    EvidenceBundle,
    EvidenceItem,
    EvidenceState,
    Finding,
    FindingSeverity,
    Recommendation,
    RecommendationStatus,
    SafeLink,
    Uncertainty,
    UncertaintyKind,
)


RULE_SET_VERSION = "mc613-rules-v1"
RULE_VERSION = "1.0.0"
MAX_RULE_WORK_ITEMS = 128
MAX_RULE_FINDINGS = 50
MAX_RULE_RECOMMENDATIONS = 25
MAX_HISTORY_POINTS = 128
MAX_CADENCE_SECONDS = 86_400.0
SUSTAINED_MIN_DURATION_SECONDS = 300.0
SUSTAINED_MIN_POINTS = 3
SUSTAINED_MAX_GAP_MULTIPLIER = 1.5
RESOURCE_THRESHOLDS: Mapping[str, float] = {
    "cpu_percent": 85.0,
    "memory_percent": 85.0,
    "disk_percent": 90.0,
}


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Canonical rule-field contract metadata."""

    name: str
    scalar_type: str
    unit: str | None
    allowed_values: tuple[str, ...]
    meaning: str
    comparison_side: str | None
    required_for: tuple[str, ...]
    participates_in_identity: bool = False


# Canonical names are intentionally singular: rules do not support aliases.
# Missing or malformed fields are withheld by the corresponding predicate.
PHASE3_FIELD_SCHEMA: tuple[FieldSpec, ...] = (
    FieldSpec("service_status", "string", None, ("healthy", "degraded", "critical", "unavailable", "unknown"), "Safe backend service status", None, ("service.health.unavailable",)),
    FieldSpec("metric", "string", None, tuple(RESOURCE_THRESHOLDS), "Resource metric name", None, ("resource.pressure.sustained", "resource.pressure.spike")),
    FieldSpec("value", "number", "percent", (), "Observed resource metric value", None, ("resource.pressure.sustained",)),
    FieldSpec("unit", "string", None, ("percent",), "Unit for a resource metric", None, ("resource.pressure.sustained", "resource.pressure.spike")),
    FieldSpec("comparison_status", "string", None, ("unchanged", "changed", "missing", "unavailable", "indeterminate"), "Bounded comparison result", None, ("resource.pressure.spike", "deployment.revision.changed", "project.state.changed")),
    FieldSpec("baseline_value", "number", "percent", (), "Numeric comparison baseline", "baseline", ("resource.pressure.spike",)),
    FieldSpec("current_value", "number", "percent", (), "Numeric comparison current value", "current", ("resource.pressure.spike",)),
    FieldSpec("cadence_seconds", "number", "seconds", (), "Declared telemetry sampling interval", None, ("telemetry.cadence.gap",)),
    FieldSpec("retention_status", "string", None, ("healthy", "unavailable", "invalid", "failed"), "Explicit normalized retention status", None, ("telemetry.source.degraded",)),
    FieldSpec("baseline_revision", "string", None, (), "Comparison baseline revision identifier", "baseline", ("deployment.revision.changed",)),
    FieldSpec("current_revision", "string", None, (), "Comparison current revision identifier", "current", ("deployment.revision.changed",)),
    FieldSpec("revision", "string", None, (), "Known deployment revision identity", None, ("deployment.posture.unverified",)),
    FieldSpec("runtime_confirmation_status", "string", None, ("observed", "unavailable", "not_observed", "stale", "invalid"), "Explicit runtime confirmation status", None, ("deployment.posture.unverified",)),
    FieldSpec("identity_proven", "boolean", None, (), "Evidence-backed project identity flag", None, ("project.state.changed",), True),
    FieldSpec("changed_field", "string", None, ("revision", "branch", "dirty", "runtime_association", "component_count", "health_state"), "Allow-listed changed project state field", None, ("project.state.changed",)),
    FieldSpec("health_status", "string", None, ("healthy", "degraded", "critical", "unknown"), "Explicit project health projection", None, ("project.health.degraded",)),
    FieldSpec("supporting_evidence_count", "integer", "count", (), "Count of safe supporting health evidence", None, ("project.health.degraded",)),
)

_SCHEMA_BY_NAME = {spec.name: spec for spec in PHASE3_FIELD_SCHEMA}


@dataclass(frozen=True, slots=True)
class ResourceHistoryPoint:
    """One normalized, bounded point in a resource-history envelope."""

    evidence_id: str
    observed_at: datetime
    metric: str
    value: float
    unit: str = "percent"
    state: EvidenceState = EvidenceState.OBSERVED

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_id, str) or not self.evidence_id or len(self.evidence_id) > 128:
            raise AdvisorValidationError("Invalid resource-history evidence ID")
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise AdvisorValidationError("Resource-history observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(timezone.utc))
        if self.metric not in RESOURCE_THRESHOLDS:
            raise AdvisorValidationError("Invalid resource-history metric")
        if self.unit != "percent":
            raise AdvisorValidationError("Resource-history unit must be percent")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)) or not math.isfinite(float(self.value)) or not 0 <= float(self.value) <= 100:
            raise AdvisorValidationError("Invalid resource-history value")
        object.__setattr__(self, "value", float(self.value))
        if not isinstance(self.state, EvidenceState):
            try:
                object.__setattr__(self, "state", EvidenceState(self.state))
            except (TypeError, ValueError) as exc:
                raise AdvisorValidationError("Invalid resource-history state") from exc

    def canonical(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "observed_at": self.observed_at.isoformat(),
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "state": self.state.value,
        }


@dataclass(frozen=True, slots=True)
class ResourceHistoryEnvelope:
    """Bounded immutable continuity contract for sustained-resource rules.

    ``complete`` is producer-supplied completeness metadata.  Continuity is
    then determined mechanically from ordered points and the approved inclusive
    maximum gap of ``1.5 * cadence_seconds``; no arbitrary timestamp span is
    treated as sufficient by itself.
    """

    resource_id: str
    metric: str
    unit: str
    cadence_seconds: float
    window_start: datetime
    window_end: datetime
    complete: bool
    points: tuple[ResourceHistoryPoint, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.resource_id, str) or not self.resource_id or len(self.resource_id) > 128:
            raise AdvisorValidationError("Invalid resource-history resource ID")
        if self.metric not in RESOURCE_THRESHOLDS:
            raise AdvisorValidationError("Invalid resource-history metric")
        if self.unit != "percent":
            raise AdvisorValidationError("Resource-history unit must be percent")
        if isinstance(self.cadence_seconds, bool) or not isinstance(self.cadence_seconds, (int, float)) or not math.isfinite(float(self.cadence_seconds)) or not 0 < float(self.cadence_seconds) <= MAX_CADENCE_SECONDS:
            raise AdvisorValidationError("Invalid resource-history cadence_seconds")
        object.__setattr__(self, "cadence_seconds", float(self.cadence_seconds))
        for name in ("window_start", "window_end"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise AdvisorValidationError(f"{name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        if self.window_start >= self.window_end:
            raise AdvisorValidationError("Resource-history window must be ordered")
        if not isinstance(self.complete, bool):
            raise AdvisorValidationError("Resource-history completeness must be boolean")
        if not isinstance(self.points, (tuple, list)) or len(self.points) > MAX_HISTORY_POINTS:
            raise AdvisorValidationError("Resource-history points exceed the bound")
        points = tuple(self.points)
        if any(not isinstance(point, ResourceHistoryPoint) for point in points):
            raise AdvisorValidationError("Resource-history points must be typed immutable points")
        if any(point.metric != self.metric or point.unit != self.unit for point in points):
            raise AdvisorValidationError("Resource-history point schema differs from envelope")
        if any(point.observed_at < self.window_start or point.observed_at > self.window_end for point in points):
            raise AdvisorValidationError("Resource-history point falls outside the bounded window")
        points = tuple(sorted(points, key=lambda point: (point.observed_at, point.evidence_id)))
        if len({point.evidence_id for point in points}) != len(points):
            raise AdvisorValidationError("Duplicate resource-history evidence ID")
        if len({point.observed_at for point in points}) != len(points):
            raise AdvisorValidationError("Duplicate resource-history observed_at timestamp")
        object.__setattr__(self, "points", points)

    @property
    def maximum_allowable_gap_seconds(self) -> float:
        return self.cadence_seconds * SUSTAINED_MAX_GAP_MULTIPLIER

    @property
    def coverage_seconds(self) -> float:
        if len(self.points) < 2:
            return 0.0
        return (self.points[-1].observed_at - self.points[0].observed_at).total_seconds()

    def canonical(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "metric": self.metric,
            "unit": self.unit,
            "cadence_seconds": self.cadence_seconds,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "complete": self.complete,
            "points": [point.canonical() for point in self.points],
        }

    @property
    def stable_id(self) -> str:
        return f"history-{_digest(self.canonical())[:32]}"


@dataclass(frozen=True, slots=True)
class RuleDefinition:
    rule_id: str
    category: AdvisorCategory
    priority: int
    evaluator_name: str


@dataclass(frozen=True, slots=True)
class RuleOutput:
    findings: tuple[Finding, ...] = ()
    recommendations: tuple[Recommendation, ...] = ()
    uncertainties: tuple[Uncertainty, ...] = ()


RULE_CATALOG: tuple[RuleDefinition, ...] = (
    RuleDefinition("service.health.unavailable", AdvisorCategory.SERVICE_HEALTH, 1, "service_health_unavailable"),
    RuleDefinition("service.health.stale", AdvisorCategory.SERVICE_HEALTH, 2, "service_health_stale"),
    RuleDefinition("resource.pressure.sustained", AdvisorCategory.RESOURCE_PRESSURE, 1, "resource_pressure_sustained"),
    RuleDefinition("resource.pressure.spike", AdvisorCategory.RESOURCE_PRESSURE, 3, "resource_pressure_spike"),
    RuleDefinition("telemetry.cadence.gap", AdvisorCategory.TELEMETRY_ANOMALY, 2, "telemetry_cadence_gap"),
    RuleDefinition("telemetry.source.degraded", AdvisorCategory.TELEMETRY_ANOMALY, 1, "telemetry_source_degraded"),
    RuleDefinition("deployment.revision.changed", AdvisorCategory.DEPLOYMENT_CHANGE, 3, "deployment_revision_changed"),
    RuleDefinition("deployment.posture.unverified", AdvisorCategory.DEPLOYMENT_CHANGE, 2, "deployment_posture_unverified"),
    RuleDefinition("project.state.changed", AdvisorCategory.PROJECT_STATE_CHANGE, 3, "project_state_changed"),
    RuleDefinition("project.health.degraded", AdvisorCategory.PROJECT_STATE_CHANGE, 2, "project_health_degraded"),
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AdvisorValidationError("evaluation_time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _field(item: EvidenceItem, name: str) -> Any:
    if name not in _SCHEMA_BY_NAME:
        raise KeyError(name)
    return dict(item.fields).get(name)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return float(value)


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _valid_field(item: EvidenceItem, name: str) -> bool:
    value = _field(item, name)
    spec = _SCHEMA_BY_NAME[name]
    if spec.scalar_type == "number":
        valid = _number(value) is not None
    elif spec.scalar_type == "integer":
        valid = _integer(value) is not None
    elif spec.scalar_type == "boolean":
        valid = isinstance(value, bool)
    else:
        valid = _text(value) is not None
    return valid and (not spec.allowed_values or value in spec.allowed_values)


def _items(bundle: EvidenceBundle, *, resource_types: Iterable[str] = ()) -> tuple[EvidenceItem, ...]:
    allowed = frozenset(resource_types)
    values = tuple(item for item in bundle.items if not allowed or item.resource_type in allowed)
    return values[:MAX_RULE_WORK_ITEMS]


def _uncertainty(
    *,
    kind: UncertaintyKind,
    summary: str,
    evidence_refs: tuple[str, ...] = (),
    impact: ConfidenceImpact = ConfidenceImpact.TO_UNKNOWN,
) -> Uncertainty:
    return Uncertainty(
        uncertainty_id=f"uncertainty-{_digest({'kind': kind.value, 'summary': summary, 'evidence_refs': evidence_refs})[:32]}",
        kind=kind,
        summary=summary,
        evidence_refs=evidence_refs,
        confidence_impact=impact,
    )


def _uncertainty_refs(bundle: EvidenceBundle, refs: Iterable[str]) -> tuple[str, ...]:
    ref_set = frozenset(refs)
    return tuple(sorted({uncertainty.uncertainty_id for uncertainty in bundle.uncertainties if ref_set.intersection(uncertainty.evidence_refs)}))


def _provenance_refs(items: Sequence[EvidenceItem]) -> tuple[str, ...]:
    return tuple(sorted({reference for item in items for reference in item.provenance_refs}))


def _links(items: Sequence[EvidenceItem]) -> tuple[SafeLink, ...]:
    links: dict[str, SafeLink] = {}
    for item in items:
        for link in item.safe_links:
            links[link.route] = link
    return tuple(sorted(links.values(), key=lambda link: (link.route, link.label)))


def _finding(
    definition: RuleDefinition,
    *,
    items: Sequence[EvidenceItem],
    title: str,
    condition: str,
    severity: FindingSeverity,
    confidence: Confidence,
    uncertainty_refs: tuple[str, ...],
) -> Finding:
    evidence_refs = tuple(sorted(item.evidence_id for item in items))
    payload = {
        "rule_id": definition.rule_id,
        "rule_version": RULE_VERSION,
        "category": definition.category.value,
        "resource_ids": sorted(item.resource_id for item in items),
        "evidence_refs": evidence_refs,
        "condition": condition,
    }
    return Finding(
        finding_id=f"finding-{_digest(payload)[:32]}",
        category=definition.category,
        severity=severity,
        confidence=confidence,
        title=title,
        condition=condition,
        rule_id=definition.rule_id,
        rule_version=RULE_VERSION,
        evidence_refs=evidence_refs,
        uncertainty_refs=uncertainty_refs,
        provenance_refs=_provenance_refs(items),
        safe_links=_links(items),
    )


def _recommendation(definition: RuleDefinition, finding: Finding, *, summary: str, rationale: str) -> Recommendation:
    payload = {"rule_id": definition.rule_id, "finding_id": finding.finding_id, "category": definition.category.value, "evidence_refs": finding.evidence_refs}
    status = RecommendationStatus.INSUFFICIENT_EVIDENCE if finding.confidence is Confidence.UNKNOWN else RecommendationStatus.NEW
    return Recommendation(
        recommendation_id=f"recommendation-{_digest(payload)[:32]}",
        category=definition.category,
        priority=definition.priority,
        status=status,
        title="Review advisor evidence",
        summary=summary,
        rationale=rationale,
        confidence=finding.confidence,
        finding_refs=(finding.finding_id,),
        evidence_refs=finding.evidence_refs,
        uncertainty_refs=finding.uncertainty_refs,
        safe_links=finding.safe_links,
        provenance_refs=finding.provenance_refs,
    )


def _output(definition: RuleDefinition, finding: Finding, *, summary: str, rationale: str) -> RuleOutput:
    return RuleOutput((finding,), (_recommendation(definition, finding, summary=summary, rationale=rationale),))


def _group(items: Sequence[EvidenceItem], key: Callable[[EvidenceItem], tuple[str, ...]]) -> tuple[tuple[EvidenceItem, ...], ...]:
    groups: dict[tuple[str, ...], list[EvidenceItem]] = defaultdict(list)
    for item in items:
        groups[key(item)].append(item)
    return tuple(tuple(sorted(values, key=lambda value: (value.observed_at or datetime.min.replace(tzinfo=timezone.utc), value.evidence_id))) for _, values in sorted(groups.items()))


def _has_conflict(bundle: EvidenceBundle, refs: Iterable[str]) -> bool:
    ref_set = frozenset(refs)
    return any(uncertainty.kind is UncertaintyKind.CONFLICTING_EVIDENCE and ref_set.intersection(uncertainty.evidence_refs) for uncertainty in bundle.uncertainties)


def _missing(definition: RuleDefinition, item: EvidenceItem, fields: Sequence[str]) -> RuleOutput:
    missing = tuple(name for name in fields if not _valid_field(item, name))
    if not missing:
        return RuleOutput()
    return RuleOutput(
        uncertainties=(
            _uncertainty(
                kind=UncertaintyKind.MISSING_EVIDENCE,
                summary=f"Required {definition.category.value} fields were absent or malformed: {', '.join(missing)}",
                evidence_refs=(item.evidence_id,),
                impact=ConfidenceImpact.WITHHELD,
            ),
        ),
    )


def _service_health_unavailable(bundle: EvidenceBundle, definition: RuleDefinition) -> RuleOutput:
    for item in _items(bundle, resource_types=("service",)):
        if item.source_id not in {"service_health", "telemetry"} or item.state is not EvidenceState.UNAVAILABLE:
            continue
        missing = _missing(definition, item, ("service_status",))
        if missing.uncertainties:
            return missing
        peers = tuple(peer for peer in bundle.items if peer.resource_type == "service" and peer.resource_id == item.resource_id)
        if _has_conflict(bundle, (peer.evidence_id for peer in peers)) or _field(item, "service_status") != "unavailable":
            continue
        severity = FindingSeverity.CRITICAL if item.resource_id in {"aipm-telemetry", "aipm-events", "mc3", "telemetry"} else FindingSeverity.WARNING
        finding = _finding(definition, items=(item,), title="Service health is unavailable", condition=f"Service health evidence for {item.resource_id} is unavailable", severity=severity, confidence=Confidence.UNKNOWN, uncertainty_refs=_uncertainty_refs(bundle, (item.evidence_id,)))
        return _output(definition, finding, summary="Review service-health and bounded logs evidence for the affected service", rationale="The normalized service-health source explicitly reports unavailable evidence")
    return RuleOutput()


def _service_health_stale(bundle: EvidenceBundle, definition: RuleDefinition) -> RuleOutput:
    for item in _items(bundle, resource_types=("service",)):
        if item.source_id not in {"service_health", "telemetry"} or item.state is not EvidenceState.STALE:
            continue
        peers = tuple(peer for peer in bundle.items if peer.resource_type == "service" and peer.resource_id == item.resource_id)
        if _has_conflict(bundle, (peer.evidence_id for peer in peers)):
            continue
        confidence = Confidence.HIGH if item.freshness_deadline is not None else Confidence.LOW
        uncertainty_refs = _uncertainty_refs(bundle, (item.evidence_id,))
        if confidence is not Confidence.HIGH and not uncertainty_refs:
            uncertainty = _uncertainty(kind=UncertaintyKind.STALE_EVIDENCE, summary="Service-health evidence is explicitly stale", evidence_refs=(item.evidence_id,), impact=ConfidenceImpact.HIGH_TO_MEDIUM)
            return RuleOutput(uncertainties=(uncertainty,))
        finding = _finding(definition, items=(item,), title="Service health evidence is stale", condition=f"Service health evidence for {item.resource_id} exceeded its freshness boundary", severity=FindingSeverity.WARNING, confidence=confidence, uncertainty_refs=uncertainty_refs)
        return _output(definition, finding, summary="Review freshness and recent bounded logs for the affected service", rationale="The normalized service-health observation is explicitly stale")
    return RuleOutput()


def _resource_pressure_sustained(bundle: EvidenceBundle, definition: RuleDefinition, histories: Sequence[ResourceHistoryEnvelope]) -> RuleOutput:
    if not histories:
        return RuleOutput(uncertainties=(_uncertainty(kind=UncertaintyKind.MISSING_EVIDENCE, summary="No bounded resource-history envelope was supplied for sustained-pressure evaluation", impact=ConfidenceImpact.WITHHELD),))
    evidence_map = {item.evidence_id: item for item in bundle.items}
    findings: list[Finding] = []
    uncertainties: list[Uncertainty] = []
    for envelope in sorted(histories[:MAX_RULE_WORK_ITEMS], key=lambda value: (value.resource_id, value.metric, value.window_start, value.stable_id)):
        refs = tuple(point.evidence_id for point in envelope.points)
        if not envelope.complete:
            uncertainties.append(_uncertainty(kind=UncertaintyKind.MISSING_EVIDENCE, summary=f"Resource-history window for {envelope.resource_id} is incomplete", evidence_refs=refs, impact=ConfidenceImpact.WITHHELD))
            continue
        if envelope.window_end - envelope.window_start < timedelta(seconds=SUSTAINED_MIN_DURATION_SECONDS) or envelope.coverage_seconds < SUSTAINED_MIN_DURATION_SECONDS:
            uncertainties.append(_uncertainty(kind=UncertaintyKind.MISSING_EVIDENCE, summary=f"Resource-history window for {envelope.resource_id} does not cover the minimum sustained duration", evidence_refs=refs, impact=ConfidenceImpact.WITHHELD))
            continue
        if len(envelope.points) < SUSTAINED_MIN_POINTS:
            uncertainties.append(_uncertainty(kind=UncertaintyKind.MISSING_EVIDENCE, summary=f"Resource-history for {envelope.resource_id} contains fewer than three valid consecutive points", evidence_refs=refs, impact=ConfidenceImpact.WITHHELD))
            continue
        invalid_refs: list[str] = []
        degraded_kind: UncertaintyKind | None = None
        for point in envelope.points:
            item = evidence_map.get(point.evidence_id)
            item_observed_at = item.observed_at.astimezone(timezone.utc) if item is not None and item.observed_at is not None else None
            binding_matches = (
                item is not None
                and item.resource_id == envelope.resource_id
                and item.state is point.state
                and _field(item, "metric") == point.metric
                and _field(item, "value") == point.value
                and _field(item, "unit") == point.unit
                and item_observed_at == point.observed_at
            )
            if not binding_matches:
                invalid_refs.append(point.evidence_id)
                degraded_kind = UncertaintyKind.INVALID_EVIDENCE
            elif point.state is not EvidenceState.OBSERVED:
                invalid_refs.append(point.evidence_id)
                degraded_kind = {
                    EvidenceState.STALE: UncertaintyKind.STALE_EVIDENCE,
                    EvidenceState.UNAVAILABLE: UncertaintyKind.UNAVAILABLE_SOURCE,
                    EvidenceState.NOT_OBSERVED: UncertaintyKind.MISSING_EVIDENCE,
                    EvidenceState.INVALID: UncertaintyKind.INVALID_EVIDENCE,
                }.get(point.state, UncertaintyKind.INVALID_EVIDENCE)
        if invalid_refs:
            uncertainties.append(_uncertainty(kind=degraded_kind or UncertaintyKind.INVALID_EVIDENCE, summary=f"Resource-history for {envelope.resource_id} contains a non-observed or mismatched point", evidence_refs=tuple(sorted(set(invalid_refs))), impact=ConfidenceImpact.WITHHELD))
            continue
        if _has_conflict(bundle, refs):
            uncertainties.append(_uncertainty(kind=UncertaintyKind.CONFLICTING_EVIDENCE, summary=f"Resource-history for {envelope.resource_id} contains conflicting evidence", evidence_refs=refs, impact=ConfidenceImpact.TO_UNKNOWN))
            continue
        gaps = tuple((current.observed_at - previous.observed_at).total_seconds() for previous, current in zip(envelope.points, envelope.points[1:]))
        if any(gap <= 0 or gap > envelope.maximum_allowable_gap_seconds for gap in gaps):
            uncertainties.append(_uncertainty(kind=UncertaintyKind.MISSING_EVIDENCE, summary=f"Resource-history for {envelope.resource_id} is discontinuous under the approved maximum-gap policy", evidence_refs=refs, impact=ConfidenceImpact.WITHHELD))
            continue
        threshold = RESOURCE_THRESHOLDS[envelope.metric]
        if any(point.value < threshold for point in envelope.points):
            continue
        items = tuple(evidence_map[point.evidence_id] for point in envelope.points)
        condition = f"{envelope.metric} remained at or above {threshold:g}% across {len(envelope.points)} consecutive points for {envelope.resource_id}"
        findings.append(_finding(definition, items=items, title="Sustained resource pressure observed", condition=condition, severity=FindingSeverity.CRITICAL, confidence=Confidence.HIGH, uncertainty_refs=_uncertainty_refs(bundle, refs)))
    if findings:
        finding = sorted(findings, key=lambda value: value.finding_id)[0]
        return RuleOutput((finding,), (_recommendation(definition, finding, summary=f"Review the bounded {finding.condition.split(' remained', 1)[0]} history and related service or project evidence", rationale="The complete normalized history meets the approved threshold, duration, sample-count, and continuity policy"),), tuple(uncertainties))
    return RuleOutput(uncertainties=tuple(uncertainties))


def _resource_pressure_spike(bundle: EvidenceBundle, definition: RuleDefinition) -> RuleOutput:
    for item in _items(bundle, resource_types=("history", "resource")):
        if item.state is not EvidenceState.OBSERVED:
            continue
        required = ("comparison_status", "metric", "unit", "baseline_value", "current_value")
        if not all(_valid_field(item, name) for name in required):
            return _missing(definition, item, required)
        if _field(item, "comparison_status") != "changed" or _field(item, "unit") != "percent":
            continue
        baseline = _number(_field(item, "baseline_value"))
        current = _number(_field(item, "current_value"))
        if baseline is None or current is None or current < 75 or current - baseline < 20:
            continue
        condition = f"{_field(item, 'metric')} increased from {baseline:g} to {current:g} for {item.resource_id}"
        finding = _finding(definition, items=(item,), title="Resource pressure spike observed", condition=condition, severity=FindingSeverity.WARNING, confidence=Confidence.HIGH, uncertainty_refs=_uncertainty_refs(bundle, (item.evidence_id,)))
        return _output(definition, finding, summary="Compare the resource change with recent service, deployment, and project evidence", rationale="A bounded comparison reports a material increase without establishing causality")
    return RuleOutput()


def _telemetry_cadence_gap(bundle: EvidenceBundle, definition: RuleDefinition) -> RuleOutput:
    candidates = tuple(item for item in _items(bundle, resource_types=("history", "resource", "host")) if item.source_id in {"telemetry", "history"} and item.state is EvidenceState.OBSERVED)
    for group in _group(candidates, lambda item: (item.source_id, item.resource_id)):
        if len(group) < 2 or any(not _valid_field(item, "cadence_seconds") for item in group):
            continue
        cadences = {_number(_field(item, "cadence_seconds")) for item in group}
        if len(cadences) != 1:
            continue
        cadence = next(iter(cadences))
        timestamps = tuple(item for item in group if item.observed_at is not None)
        if cadence is None or cadence <= 0 or len(timestamps) < 2:
            continue
        gaps = tuple((current.observed_at - previous.observed_at).total_seconds() for previous, current in zip(timestamps, timestamps[1:]))
        if any(gap <= 0 for gap in gaps):
            continue
        largest_index = max(range(len(gaps)), key=lambda index: (gaps[index], timestamps[index].evidence_id, timestamps[index + 1].evidence_id))
        largest = gaps[largest_index]
        if largest <= 3 * cadence:
            continue
        refs = (timestamps[largest_index], timestamps[largest_index + 1])
        condition = f"Telemetry sampling gap of {largest:g} seconds exceeded three expected intervals for {group[0].resource_id}"
        finding = _finding(definition, items=refs, title="Telemetry cadence gap observed", condition=condition, severity=FindingSeverity.WARNING, confidence=Confidence.HIGH, uncertainty_refs=_uncertainty_refs(bundle, [item.evidence_id for item in refs]))
        return _output(definition, finding, summary="Review telemetry service health, recent bounded logs, and history for the sampling gap", rationale="The normalized timestamps show a gap beyond the declared cadence boundary")
    return RuleOutput()


def _telemetry_source_degraded(bundle: EvidenceBundle, definition: RuleDefinition) -> RuleOutput:
    for item in _items(bundle):
        explicit = item.source_id == "telemetry" and item.state in {EvidenceState.UNAVAILABLE, EvidenceState.INVALID, EvidenceState.NOT_OBSERVED}
        retention = item.source_id == "telemetry" and _field(item, "retention_status") in {"unavailable", "invalid", "failed"}
        if not explicit and not retention:
            continue
        uncertainty_refs = _uncertainty_refs(bundle, (item.evidence_id,))
        if item.state is EvidenceState.INVALID and not uncertainty_refs:
            return RuleOutput(uncertainties=(_uncertainty(kind=UncertaintyKind.INVALID_EVIDENCE, summary="Invalid telemetry evidence was withheld", evidence_refs=(item.evidence_id,)),))
        confidence = Confidence.UNKNOWN if item.state in {EvidenceState.UNAVAILABLE, EvidenceState.INVALID, EvidenceState.NOT_OBSERVED} else Confidence.HIGH
        if confidence is not Confidence.HIGH and not uncertainty_refs:
            return RuleOutput(uncertainties=(_uncertainty(kind=UncertaintyKind.UNAVAILABLE_SOURCE, summary="Telemetry source degradation lacks corroborating uncertainty", evidence_refs=(item.evidence_id,)),))
        severity = FindingSeverity.CRITICAL if item.resource_id in {"telemetry", "aipm-telemetry"} else FindingSeverity.WARNING
        condition = f"Telemetry source evidence for {item.resource_id} reports {item.state.value}"
        finding = _finding(definition, items=(item,), title="Telemetry source is degraded", condition=condition, severity=severity, confidence=confidence, uncertainty_refs=uncertainty_refs)
        return _output(definition, finding, summary="Review telemetry service-health, bounded logs, and history evidence for the degraded source", rationale="The normalized source status explicitly reports degraded telemetry evidence")
    return RuleOutput()


def _deployment_revision_changed(bundle: EvidenceBundle, definition: RuleDefinition) -> RuleOutput:
    for item in _items(bundle, resource_types=("deployment", "project")):
        if item.state is not EvidenceState.OBSERVED:
            continue
        required = ("comparison_status", "baseline_revision", "current_revision")
        if not all(_valid_field(item, name) for name in required):
            return _missing(definition, item, required)
        if _field(item, "comparison_status") != "changed":
            continue
        before = _field(item, "baseline_revision")
        after = _field(item, "current_revision")
        if before == after:
            continue
        finding = _finding(definition, items=(item,), title="Deployment revision changed", condition=f"Observed revision changed from {before} to {after} for {item.resource_id}", severity=FindingSeverity.INFO, confidence=Confidence.HIGH, uncertainty_refs=_uncertainty_refs(bundle, (item.evidence_id,)))
        return _output(definition, finding, summary="Review the revision change alongside project health, service health, and bounded history", rationale="A bounded deployment observation reports different baseline and current revisions")
    return RuleOutput()


def _deployment_posture_unverified(bundle: EvidenceBundle, definition: RuleDefinition) -> RuleOutput:
    for item in _items(bundle, resource_types=("deployment",)):
        if item.state is not EvidenceState.OBSERVED:
            continue
        required = ("revision", "runtime_confirmation_status")
        if not all(_valid_field(item, name) for name in required):
            return _missing(definition, item, required)
        if _field(item, "runtime_confirmation_status") not in {"unavailable", "not_observed", "stale", "invalid"}:
            continue
        finding = _finding(definition, items=(item,), title="Deployment posture is unverified", condition=f"Runtime confirmation is {_field(item, 'runtime_confirmation_status')} for observed revision {_field(item, 'revision')} of {item.resource_id}", severity=FindingSeverity.WARNING, confidence=Confidence.HIGH, uncertainty_refs=_uncertainty_refs(bundle, (item.evidence_id,)))
        return _output(definition, finding, summary="Review service health and project/runtime evidence for the identified revision", rationale="Repository identity is present but fresh runtime confirmation is not observed")
    return RuleOutput()


def _project_state_changed(bundle: EvidenceBundle, definition: RuleDefinition) -> RuleOutput:
    for item in _items(bundle, resource_types=("project",)):
        if item.state is not EvidenceState.OBSERVED:
            continue
        required = ("comparison_status", "identity_proven", "changed_field")
        if not all(_valid_field(item, name) for name in required):
            return _missing(definition, item, required)
        if _field(item, "comparison_status") != "changed" or _field(item, "identity_proven") is not True:
            continue
        changed_field = _field(item, "changed_field")
        severity = FindingSeverity.WARNING if changed_field in {"dirty", "runtime_association", "health_state"} else FindingSeverity.INFO
        finding = _finding(definition, items=(item,), title="Project state changed", condition=f"Project {item.resource_id} has an observed change in {changed_field}", severity=severity, confidence=Confidence.HIGH, uncertainty_refs=_uncertainty_refs(bundle, (item.evidence_id,)))
        return _output(definition, finding, summary="Review the project detail and bounded comparison evidence for the changed state", rationale="A proven project observation explicitly reports a changed state field")
    return RuleOutput()


def _project_health_degraded(bundle: EvidenceBundle, definition: RuleDefinition) -> RuleOutput:
    for item in _items(bundle, resource_types=("project",)):
        if item.state is not EvidenceState.OBSERVED:
            continue
        required = ("health_status", "supporting_evidence_count")
        if not all(_valid_field(item, name) for name in required):
            return _missing(definition, item, required)
        if _field(item, "health_status") != "degraded":
            continue
        finding = _finding(definition, items=(item,), title="Project health is degraded", condition=f"Project health evidence for {item.resource_id} explicitly reports degraded state", severity=FindingSeverity.WARNING, confidence=Confidence.HIGH, uncertainty_refs=_uncertainty_refs(bundle, (item.evidence_id,)))
        return _output(definition, finding, summary="Review project health evidence, related service state, and bounded recent history", rationale="The normalized project-health projection explicitly reports degraded state with supporting evidence")
    return RuleOutput()


_EVALUATORS: dict[str, Callable[..., RuleOutput]] = {
    "service_health_unavailable": _service_health_unavailable,
    "service_health_stale": _service_health_stale,
    "resource_pressure_sustained": _resource_pressure_sustained,
    "resource_pressure_spike": _resource_pressure_spike,
    "telemetry_cadence_gap": _telemetry_cadence_gap,
    "telemetry_source_degraded": _telemetry_source_degraded,
    "deployment_revision_changed": _deployment_revision_changed,
    "deployment_posture_unverified": _deployment_posture_unverified,
    "project_state_changed": _project_state_changed,
    "project_health_degraded": _project_health_degraded,
}


class AdvisorRuleEngine:
    """Evaluate the fixed MC-6.13 catalog against immutable evidence."""

    def __init__(self, *, catalog: Sequence[RuleDefinition] = RULE_CATALOG) -> None:
        catalog = tuple(catalog)
        if len(catalog) != len(RULE_CATALOG) or tuple(item.rule_id for item in catalog) != tuple(item.rule_id for item in RULE_CATALOG):
            raise ValueError("Rule catalog order and membership are immutable")
        self.catalog = catalog

    def evaluate(
        self,
        bundle: EvidenceBundle,
        *,
        request_id: str,
        evaluation_time: datetime,
        history_envelopes: Sequence[ResourceHistoryEnvelope] = (),
    ) -> AdvisorResponse:
        evaluation_time = _utc(evaluation_time)
        if evaluation_time != bundle.evaluation_time:
            raise AdvisorValidationError("Rule evaluation_time differs from evidence bundle")
        if not isinstance(history_envelopes, (tuple, list)) or len(history_envelopes) > MAX_RULE_WORK_ITEMS:
            raise AdvisorValidationError("Too many resource-history envelopes")
        if any(not isinstance(envelope, ResourceHistoryEnvelope) for envelope in history_envelopes):
            raise AdvisorValidationError("Resource-history envelopes must be typed immutable values")
        findings: list[Finding] = []
        recommendations: list[Recommendation] = []
        uncertainties: list[Uncertainty] = list(bundle.uncertainties)
        for definition in self.catalog:
            if definition.evaluator_name == "resource_pressure_sustained":
                result = _EVALUATORS[definition.evaluator_name](bundle, definition, tuple(history_envelopes))
            else:
                result = _EVALUATORS[definition.evaluator_name](bundle, definition)
            findings.extend(result.findings)
            recommendations.extend(result.recommendations)
            uncertainties.extend(result.uncertainties)
        findings = list({finding.finding_id: finding for finding in findings}.values())[:MAX_RULE_FINDINGS]
        finding_ids = {finding.finding_id for finding in findings}
        recommendations = [recommendation for recommendation in {item.recommendation_id: item for item in recommendations}.values() if set(recommendation.finding_refs).issubset(finding_ids)][:MAX_RULE_RECOMMENDATIONS]
        uncertainty_map = {item.uncertainty_id: item for item in uncertainties}
        response = AdvisorResponse(
            schema_version=bundle.schema_version,
            request_id=request_id,
            available=bundle.status not in {AdvisorStatus.UNAVAILABLE, AdvisorStatus.ERROR},
            status=bundle.status,
            evaluation_time=evaluation_time,
            generated_at=evaluation_time,
            freshness_deadline=bundle.freshness_deadline,
            scope=bundle.scope,
            findings=tuple(findings),
            recommendations=tuple(recommendations),
            uncertainties=tuple(uncertainty_map.values()),
            provenance=bundle.provenance,
            evidence_coverage=bundle.coverage,
            links=_links(bundle.items),
        )
        response.validate_against_bundle(bundle)
        return response


__all__ = [
    "AdvisorRuleEngine",
    "FieldSpec",
    "PHASE3_FIELD_SCHEMA",
    "RESOURCE_THRESHOLDS",
    "ResourceHistoryEnvelope",
    "ResourceHistoryPoint",
    "RULE_CATALOG",
    "RULE_SET_VERSION",
    "RULE_VERSION",
    "SUSTAINED_MAX_GAP_MULTIPLIER",
    "SUSTAINED_MIN_DURATION_SECONDS",
    "SUSTAINED_MIN_POINTS",
    "RuleDefinition",
]
