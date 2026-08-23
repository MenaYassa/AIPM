from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


MAX_IDENTIFIER = 128
MAX_SAFE_TEXT = 256
MAX_SOURCE_FIELDS = 32
MAX_EVIDENCE_ITEMS = 128
MAX_FINDINGS = 50
MAX_RECOMMENDATIONS = 25
MAX_UNCERTAINTIES = 32
MAX_PROVENANCE = 32
MAX_LINKS = 32
MAX_COVERAGE = 32
MAX_REFS = 32

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ROUTE_RE = re.compile(r"^/[A-Za-z0-9_./?=&%:@+\-]{1,127}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE_TEXT_MARKERS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token=",
    "traceback",
    "exception=",
    "provider",
    "docker.sock",
    "systemctl",
    "subprocess",
    "shell",
    "sql",
    "rm -",
)
_ACTION_VERBS = r"restart|stop|start|enable|disable|execute|run|approve|consume|deploy|rollback|delete|modify|alter"
_ACTION_TARGETS = r"the|this|that|a|an|your|our|service|plan|command|deployment|database|config(?:uration)?|project|container|unit|job|operation|action|rows"
_ACTION_DIRECTIVE_RE = re.compile(
    rf"(?:^|\b)(?:please\s+|you\s+should\s+|must\s+|need\s+to\s+|go\s+ahead\s+and\s+)?(?:{_ACTION_VERBS})\s+(?:{_ACTION_TARGETS})\b",
    re.IGNORECASE,
)
_COMMAND_SYNTAX_RE = re.compile(r"(?:^|[;&|`$])\s*(?:sudo\s+)?(?:systemctl|docker|kubectl|sqlite3|curl|wget|bash|sh|python(?:3)?|git)\b", re.IGNORECASE)
_NEUTRAL_VERB_NOUN_RE = re.compile(
    r"(?<!\w)(?:start|stop|run|deploy|delete|modify)\s+(?:time|duration|count|status|state|change|revision|event|history|evidence|result|outcome|value|timestamp|sample|interval|observed|data)\b",
    re.IGNORECASE,
)


class AdvisorValidationError(ValueError):
    """Raised when an advisor contract violates a bounded safety invariant."""


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        return {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2, Confidence.UNKNOWN: 3}[self]


class EvidenceState(StrEnum):
    OBSERVED = "observed"
    UNAVAILABLE = "unavailable"
    NOT_OBSERVED = "not_observed"
    STALE = "stale"
    INVALID = "invalid"


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {FindingSeverity.CRITICAL: 0, FindingSeverity.WARNING: 1, FindingSeverity.INFO: 2}[self]


class AdvisorStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class RecommendationStatus(StrEnum):
    NEW = "new"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    SUPERSEDED = "superseded"


class UncertaintyKind(StrEnum):
    MISSING_EVIDENCE = "missing_evidence"
    STALE_EVIDENCE = "stale_evidence"
    UNAVAILABLE_SOURCE = "unavailable_source"
    INVALID_EVIDENCE = "invalid_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    SCOPE_LIMITED = "scope_limited"
    PROVENANCE_UNVERIFIED = "provenance_unverified"


class ConfidenceImpact(StrEnum):
    HIGH_TO_MEDIUM = "high_to_medium"
    MEDIUM_TO_LOW = "medium_to_low"
    TO_UNKNOWN = "to_unknown"
    WITHHELD = "withheld"


class AdvisorScope(StrEnum):
    OVERVIEW = "overview"
    HOST = "host"
    SERVICES = "services"
    PROJECTS = "projects"
    TELEMETRY = "telemetry"
    DEPLOYMENT = "deployment"


class AdvisorCategory(StrEnum):
    SERVICE_HEALTH = "service_health"
    RESOURCE_PRESSURE = "resource_pressure"
    TELEMETRY_ANOMALY = "telemetry_anomaly"
    DEPLOYMENT_CHANGE = "deployment_change"
    PROJECT_STATE_CHANGE = "project_state_change"


_ALLOWED_SOURCES = frozenset({"host", "telemetry", "logs", "events", "incidents", "history", "project", "service_health"})
_ALLOWED_RESOURCE_TYPES = frozenset({"host", "service", "project", "container", "incident", "history", "deployment", "tunnel", "event", "resource"})
_ALLOWED_SCALARS = (str, int, float, bool, type(None))


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _normalize_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER:
        raise AdvisorValidationError(f"Invalid {name}")
    value = unicodedata.normalize("NFC", value)
    if _ID_RE.fullmatch(value) is None:
        raise AdvisorValidationError(f"Invalid {name}")
    return value


def _normalize_key(value: Any, name: str) -> str:
    if not isinstance(value, str) or _KEY_RE.fullmatch(value) is None:
        raise AdvisorValidationError(f"Invalid {name}")
    return value


def _safe_text(value: Any, name: str, *, maximum: int = MAX_SAFE_TEXT, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise AdvisorValidationError(f"Invalid {name}")
    value = unicodedata.normalize("NFC", value)
    if not allow_empty and not value:
        raise AdvisorValidationError(f"Invalid {name}")
    if len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AdvisorValidationError(f"Invalid {name}")
    lowered = value.casefold()
    if any(marker in lowered for marker in _UNSAFE_TEXT_MARKERS):
        raise AdvisorValidationError(f"Unsafe {name}")
    if _COMMAND_SYNTAX_RE.search(value) or (_ACTION_DIRECTIVE_RE.search(value) and not _NEUTRAL_VERB_NOUN_RE.search(value)):
        raise AdvisorValidationError(f"Unsafe {name}")
    return value


def _utc(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AdvisorValidationError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _enum(value: Any, enum_type: type[StrEnum], name: str) -> StrEnum:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        raise AdvisorValidationError(f"Invalid {name}") from exc


def _refs(values: Any, name: str, *, maximum: int = MAX_REFS, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or len(values) > maximum or (not allow_empty and not values):
        raise AdvisorValidationError(f"Invalid {name}")
    normalized = tuple(_normalize_identifier(value, f"{name} reference") for value in values)
    if len(set(normalized)) != len(normalized):
        raise AdvisorValidationError(f"Duplicate {name} reference")
    return tuple(sorted(normalized))


def _links(values: Any, name: str = "safe links") -> tuple[SafeLink, ...]:
    if not isinstance(values, (tuple, list)) or len(values) > MAX_LINKS:
        raise AdvisorValidationError(f"Invalid {name}")
    normalized = tuple(value if isinstance(value, SafeLink) else SafeLink(**value) for value in values)
    identifiers = [link.route for link in normalized]
    if len(set(identifiers)) != len(identifiers):
        raise AdvisorValidationError(f"Duplicate {name}")
    return tuple(sorted(normalized, key=lambda link: (link.route, link.label)))


def _scalar(value: Any, name: str) -> Any:
    if not isinstance(value, _ALLOWED_SCALARS) or (isinstance(value, float) and not math.isfinite(value)):
        raise AdvisorValidationError(f"Invalid {name}")
    if isinstance(value, str):
        return _safe_text(value, name, maximum=128, allow_empty=True)
    return value


@dataclass(frozen=True, slots=True)
class SafeLink:
    route: str
    label: str

    def __post_init__(self) -> None:
        if _ROUTE_RE.fullmatch(self.route) is None or "//" in self.route or ".." in self.route:
            raise AdvisorValidationError("Invalid safe link route")
        object.__setattr__(self, "label", _safe_text(self.label, "safe link label", maximum=96))

    def canonical(self) -> dict[str, str]:
        return {"label": self.label, "route": self.route}


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: str
    source_id: str
    resource_type: str
    resource_id: str
    state: EvidenceState
    observed_at: datetime | None = None
    freshness_deadline: datetime | None = None
    fields: tuple[tuple[str, Any], ...] = ()
    safe_links: tuple[SafeLink, ...] = ()
    source_revision: str | None = None
    provenance_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _normalize_identifier(self.evidence_id, "evidence ID"))
        if self.source_id not in _ALLOWED_SOURCES:
            raise AdvisorValidationError("Invalid evidence source")
        if self.resource_type not in _ALLOWED_RESOURCE_TYPES:
            raise AdvisorValidationError("Invalid evidence resource type")
        object.__setattr__(self, "resource_id", _normalize_identifier(self.resource_id, "resource ID"))
        object.__setattr__(self, "state", _enum(self.state, EvidenceState, "evidence state"))
        observed = _utc(self.observed_at, "observed_at") if self.observed_at is not None else None
        deadline = _utc(self.freshness_deadline, "freshness_deadline") if self.freshness_deadline is not None else None
        if deadline is not None and observed is not None and deadline < observed:
            raise AdvisorValidationError("freshness_deadline cannot precede observed_at")
        if self.state is EvidenceState.OBSERVED and observed is None:
            raise AdvisorValidationError("observed evidence requires observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "freshness_deadline", deadline)
        if not isinstance(self.fields, (tuple, list)) or len(self.fields) > MAX_SOURCE_FIELDS:
            raise AdvisorValidationError("Too many evidence fields")
        normalized_fields: list[tuple[str, Any]] = []
        for key, value in self.fields:
            key = _normalize_key(key, "evidence field key")
            normalized_fields.append((key, _scalar(value, "evidence field value")))
        if len({key for key, _ in normalized_fields}) != len(normalized_fields):
            raise AdvisorValidationError("Duplicate evidence field key")
        object.__setattr__(self, "fields", tuple(sorted(normalized_fields)))
        object.__setattr__(self, "safe_links", _links(self.safe_links))
        if self.source_revision is not None:
            object.__setattr__(self, "source_revision", _normalize_identifier(self.source_revision, "source revision"))
        object.__setattr__(self, "provenance_refs", _refs(self.provenance_refs, "provenance", maximum=MAX_PROVENANCE))

    def canonical(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_id": self.source_id,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "state": self.state.value,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "freshness_deadline": self.freshness_deadline.isoformat() if self.freshness_deadline else None,
            "fields": [[key, value] for key, value in self.fields],
            "safe_links": [link.canonical() for link in self.safe_links],
            "source_revision": self.source_revision,
            "provenance_refs": list(self.provenance_refs),
        }


@dataclass(frozen=True, slots=True)
class Uncertainty:
    uncertainty_id: str
    kind: UncertaintyKind
    summary: str
    evidence_refs: tuple[str, ...] = ()
    confidence_impact: ConfidenceImpact = ConfidenceImpact.TO_UNKNOWN
    resolution_hint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "uncertainty_id", _normalize_identifier(self.uncertainty_id, "uncertainty ID"))
        object.__setattr__(self, "kind", _enum(self.kind, UncertaintyKind, "uncertainty kind"))
        object.__setattr__(self, "summary", _safe_text(self.summary, "uncertainty summary"))
        object.__setattr__(self, "evidence_refs", _refs(self.evidence_refs, "evidence", maximum=MAX_EVIDENCE_ITEMS))
        object.__setattr__(self, "confidence_impact", _enum(self.confidence_impact, ConfidenceImpact, "confidence impact"))
        if self.resolution_hint is not None:
            object.__setattr__(self, "resolution_hint", _safe_text(self.resolution_hint, "resolution hint"))

    def canonical(self) -> dict[str, Any]:
        return {
            "uncertainty_id": self.uncertainty_id,
            "kind": self.kind.value,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "confidence_impact": self.confidence_impact.value,
            "resolution_hint": self.resolution_hint,
        }


@dataclass(frozen=True, slots=True)
class ProvenanceReference:
    provenance_ref_id: str
    source_id: str
    provenance_id: str
    key_id: str
    signature_verified: bool
    plan_id: str | None = None
    plan_digest: str | None = None
    observed_at: datetime | None = None
    freshness_deadline: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provenance_ref_id", _normalize_identifier(self.provenance_ref_id, "provenance reference ID"))
        if self.source_id not in _ALLOWED_SOURCES:
            raise AdvisorValidationError("Invalid provenance source")
        object.__setattr__(self, "provenance_id", _normalize_identifier(self.provenance_id, "provenance ID"))
        object.__setattr__(self, "key_id", _normalize_identifier(self.key_id, "key ID"))
        if not isinstance(self.signature_verified, bool):
            raise AdvisorValidationError("Invalid signature verification flag")
        if self.plan_id is not None:
            object.__setattr__(self, "plan_id", _normalize_identifier(self.plan_id, "plan ID"))
        if self.plan_digest is not None and _HEX64_RE.fullmatch(self.plan_digest) is None:
            raise AdvisorValidationError("Invalid plan digest")
        observed = _utc(self.observed_at, "provenance observed_at") if self.observed_at is not None else None
        deadline = _utc(self.freshness_deadline, "provenance freshness_deadline") if self.freshness_deadline is not None else None
        if deadline is not None and observed is not None and deadline < observed:
            raise AdvisorValidationError("provenance freshness_deadline cannot precede observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "freshness_deadline", deadline)

    def canonical(self) -> dict[str, Any]:
        return {
            "provenance_ref_id": self.provenance_ref_id,
            "source_id": self.source_id,
            "provenance_id": self.provenance_id,
            "key_id": self.key_id,
            "signature_verified": self.signature_verified,
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "freshness_deadline": self.freshness_deadline.isoformat() if self.freshness_deadline else None,
        }


@dataclass(frozen=True, slots=True)
class Coverage:
    source_id: str
    expected: int = 0
    observed: int = 0
    stale: int = 0
    unavailable: int = 0
    invalid: int = 0
    omitted: int = 0

    def __post_init__(self) -> None:
        if self.source_id not in _ALLOWED_SOURCES:
            raise AdvisorValidationError("Invalid coverage source")
        values = (self.expected, self.observed, self.stale, self.unavailable, self.invalid, self.omitted)
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
            raise AdvisorValidationError("Coverage counts must be non-negative integers")
        if sum(values[1:]) > self.expected:
            raise AdvisorValidationError("Coverage counts exceed expected count")

    def canonical(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "expected": self.expected, "observed": self.observed, "stale": self.stale, "unavailable": self.unavailable, "invalid": self.invalid, "omitted": self.omitted}


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    schema_version: str
    bundle_id: str
    evaluation_time: datetime
    generated_at: datetime
    freshness_deadline: datetime | None
    status: AdvisorStatus
    scope: AdvisorScope
    items: tuple[EvidenceItem, ...] = ()
    uncertainties: tuple[Uncertainty, ...] = ()
    provenance: tuple[ProvenanceReference, ...] = ()
    coverage: tuple[Coverage, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _normalize_identifier(self.schema_version, "schema version"))
        object.__setattr__(self, "bundle_id", _normalize_identifier(self.bundle_id, "bundle ID"))
        evaluation_time = _utc(self.evaluation_time, "evaluation_time")
        generated_at = _utc(self.generated_at, "generated_at")
        if generated_at != evaluation_time:
            raise AdvisorValidationError("generated_at must equal evaluation_time for deterministic evaluation")
        object.__setattr__(self, "evaluation_time", evaluation_time)
        object.__setattr__(self, "generated_at", generated_at)
        deadline = _utc(self.freshness_deadline, "bundle freshness_deadline") if self.freshness_deadline is not None else None
        object.__setattr__(self, "freshness_deadline", deadline)
        object.__setattr__(self, "status", _enum(self.status, AdvisorStatus, "advisor status"))
        object.__setattr__(self, "scope", _enum(self.scope, AdvisorScope, "advisor scope"))
        if not isinstance(self.items, (tuple, list)) or len(self.items) > MAX_EVIDENCE_ITEMS:
            raise AdvisorValidationError("Too many evidence items")
        items = tuple(sorted(self.items, key=lambda item: (item.source_id, item.resource_type, item.resource_id, item.evidence_id)))
        if len({item.evidence_id for item in items}) != len(items):
            raise AdvisorValidationError("Duplicate evidence ID")
        object.__setattr__(self, "items", items)
        evidence_ids = {item.evidence_id for item in items}
        uncertainties = tuple(sorted(self.uncertainties, key=lambda item: (item.kind.value, item.uncertainty_id)))
        if len(uncertainties) > MAX_UNCERTAINTIES or len({item.uncertainty_id for item in uncertainties}) != len(uncertainties):
            raise AdvisorValidationError("Invalid uncertainty collection")
        for uncertainty in uncertainties:
            if not set(uncertainty.evidence_refs).issubset(evidence_ids):
                raise AdvisorValidationError("Uncertainty references evidence outside bundle")
        object.__setattr__(self, "uncertainties", uncertainties)
        provenance = tuple(sorted(self.provenance, key=lambda item: (item.source_id, item.provenance_ref_id)))
        if len(provenance) > MAX_PROVENANCE or len({item.provenance_ref_id for item in provenance}) != len(provenance):
            raise AdvisorValidationError("Invalid provenance collection")
        if any(not any(item.source_id == reference.source_id for item in items) for reference in provenance):
            raise AdvisorValidationError("Provenance source is absent from bundle")
        object.__setattr__(self, "provenance", provenance)
        coverage = tuple(sorted(self.coverage, key=lambda item: item.source_id))
        if len(coverage) > MAX_COVERAGE or len({item.source_id for item in coverage}) != len(coverage):
            raise AdvisorValidationError("Invalid coverage collection")
        object.__setattr__(self, "coverage", coverage)

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset(item.evidence_id for item in self.items)

    @property
    def uncertainty_ids(self) -> frozenset[str]:
        return frozenset(item.uncertainty_id for item in self.uncertainties)

    @property
    def provenance_ids(self) -> frozenset[str]:
        return frozenset(item.provenance_ref_id for item in self.provenance)

    def validate_finding(self, finding: Finding) -> None:
        if not set(finding.evidence_refs).issubset(self.evidence_ids):
            raise AdvisorValidationError("Finding references evidence outside bundle")
        if not set(finding.uncertainty_refs).issubset(self.uncertainty_ids):
            raise AdvisorValidationError("Finding references uncertainty outside bundle")
        if not set(finding.provenance_refs).issubset(self.provenance_ids):
            raise AdvisorValidationError("Finding references provenance outside bundle")

    def validate_response(self, response: AdvisorResponse) -> None:
        for finding in response.findings:
            self.validate_finding(finding)
        for recommendation in response.recommendations:
            if not set(recommendation.evidence_refs).issubset(self.evidence_ids):
                raise AdvisorValidationError("Recommendation references evidence outside bundle")
            if not set(recommendation.uncertainty_refs).issubset(self.uncertainty_ids):
                raise AdvisorValidationError("Recommendation references uncertainty outside bundle")
            if not set(recommendation.provenance_refs).issubset(self.provenance_ids):
                raise AdvisorValidationError("Recommendation references provenance outside bundle")

    def canonical(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "evaluation_time": self.evaluation_time.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "freshness_deadline": self.freshness_deadline.isoformat() if self.freshness_deadline else None,
            "status": self.status.value,
            "scope": self.scope.value,
            "items": [item.canonical() for item in self.items],
            "uncertainties": [item.canonical() for item in self.uncertainties],
            "provenance": [item.canonical() for item in self.provenance],
            "coverage": [item.canonical() for item in self.coverage],
        }

    def canonical_json(self) -> str:
        return _canonical(self.canonical())

    @property
    def stable_id(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Finding:
    finding_id: str
    category: str
    severity: FindingSeverity
    confidence: Confidence
    title: str
    condition: str
    rule_id: str
    rule_version: str
    evidence_refs: tuple[str, ...]
    uncertainty_refs: tuple[str, ...] = ()
    provenance_refs: tuple[str, ...] = ()
    safe_links: tuple[SafeLink, ...] = ()

    def __post_init__(self) -> None:
        _normalize_identifier(self.finding_id, "finding ID")
        object.__setattr__(self, "category", _enum(self.category, AdvisorCategory, "finding category"))
        _normalize_identifier(self.rule_id, "rule ID")
        _normalize_identifier(self.rule_version, "rule version")
        object.__setattr__(self, "severity", _enum(self.severity, FindingSeverity, "finding severity"))
        object.__setattr__(self, "confidence", _enum(self.confidence, Confidence, "finding confidence"))
        object.__setattr__(self, "title", _safe_text(self.title, "finding title"))
        object.__setattr__(self, "condition", _safe_text(self.condition, "finding condition"))
        object.__setattr__(self, "evidence_refs", _refs(self.evidence_refs, "finding evidence", allow_empty=False, maximum=MAX_EVIDENCE_ITEMS))
        object.__setattr__(self, "uncertainty_refs", _refs(self.uncertainty_refs, "finding uncertainty"))
        object.__setattr__(self, "provenance_refs", _refs(self.provenance_refs, "finding provenance", maximum=MAX_PROVENANCE))
        object.__setattr__(self, "safe_links", _links(self.safe_links))
        if self.confidence is not Confidence.HIGH and not self.uncertainty_refs:
            raise AdvisorValidationError("Non-high-confidence finding requires uncertainty")

    def canonical(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "category": self.category,
            "severity": self.severity.value,
            "confidence": self.confidence.value,
            "title": self.title,
            "condition": self.condition,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "evidence_refs": list(self.evidence_refs),
            "uncertainty_refs": list(self.uncertainty_refs),
            "provenance_refs": list(self.provenance_refs),
            "safe_links": [link.canonical() for link in self.safe_links],
        }


@dataclass(frozen=True, slots=True)
class Recommendation:
    recommendation_id: str
    category: str
    priority: int
    status: RecommendationStatus
    title: str
    summary: str
    rationale: str
    confidence: Confidence
    finding_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    uncertainty_refs: tuple[str, ...] = ()
    safe_links: tuple[SafeLink, ...] = ()
    provenance_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _normalize_identifier(self.recommendation_id, "recommendation ID")
        object.__setattr__(self, "category", _enum(self.category, AdvisorCategory, "recommendation category"))
        if not isinstance(self.priority, int) or isinstance(self.priority, bool) or not 1 <= self.priority <= 5:
            raise AdvisorValidationError("Recommendation priority must be between 1 and 5")
        object.__setattr__(self, "status", _enum(self.status, RecommendationStatus, "recommendation status"))
        object.__setattr__(self, "title", _safe_text(self.title, "recommendation title"))
        object.__setattr__(self, "summary", _safe_text(self.summary, "recommendation summary"))
        object.__setattr__(self, "rationale", _safe_text(self.rationale, "recommendation rationale"))
        object.__setattr__(self, "confidence", _enum(self.confidence, Confidence, "recommendation confidence"))
        object.__setattr__(self, "finding_refs", _refs(self.finding_refs, "recommendation finding", allow_empty=self.status is not RecommendationStatus.NEW))
        object.__setattr__(self, "evidence_refs", _refs(self.evidence_refs, "recommendation evidence", allow_empty=self.status is not RecommendationStatus.NEW, maximum=MAX_EVIDENCE_ITEMS))
        object.__setattr__(self, "uncertainty_refs", _refs(self.uncertainty_refs, "recommendation uncertainty"))
        object.__setattr__(self, "safe_links", _links(self.safe_links))
        object.__setattr__(self, "provenance_refs", _refs(self.provenance_refs, "recommendation provenance", maximum=MAX_PROVENANCE))
        if self.status is RecommendationStatus.INSUFFICIENT_EVIDENCE and self.confidence is not Confidence.UNKNOWN and not self.uncertainty_refs:
            raise AdvisorValidationError("Insufficient-evidence recommendation requires uncertainty or unknown confidence")
        if self.confidence is not Confidence.HIGH and not self.uncertainty_refs:
            raise AdvisorValidationError("Non-high-confidence recommendation requires uncertainty")

    def validate_against_findings(self, findings: tuple[Finding, ...]) -> None:
        finding_map = {finding.finding_id: finding for finding in findings}
        if not set(self.finding_refs).issubset(finding_map):
            raise AdvisorValidationError("Recommendation references finding outside response")
        allowed_evidence = set().union(*(set(finding_map[ref].evidence_refs) for ref in self.finding_refs)) if self.finding_refs else set()
        if not set(self.evidence_refs).issubset(allowed_evidence):
            raise AdvisorValidationError("Recommendation evidence is not traceable through findings")
        if self.finding_refs and any(finding_map[ref].category != self.category for ref in self.finding_refs):
            raise AdvisorValidationError("Recommendation category differs from finding category")

    def canonical(self) -> dict[str, Any]:
        return {
            "recommendation_id": self.recommendation_id,
            "category": self.category,
            "priority": self.priority,
            "status": self.status.value,
            "title": self.title,
            "summary": self.summary,
            "rationale": self.rationale,
            "confidence": self.confidence.value,
            "finding_refs": list(self.finding_refs),
            "evidence_refs": list(self.evidence_refs),
            "uncertainty_refs": list(self.uncertainty_refs),
            "safe_links": [link.canonical() for link in self.safe_links],
            "provenance_refs": list(self.provenance_refs),
        }


@dataclass(frozen=True, slots=True)
class AdvisorResponse:
    schema_version: str
    request_id: str
    available: bool
    status: AdvisorStatus
    evaluation_time: datetime
    generated_at: datetime
    freshness_deadline: datetime | None
    scope: AdvisorScope
    findings: tuple[Finding, ...] = ()
    recommendations: tuple[Recommendation, ...] = ()
    uncertainties: tuple[Uncertainty, ...] = ()
    provenance: tuple[ProvenanceReference, ...] = ()
    evidence_coverage: tuple[Coverage, ...] = ()
    links: tuple[SafeLink, ...] = ()
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _normalize_identifier(self.schema_version, "schema version"))
        object.__setattr__(self, "request_id", _normalize_identifier(self.request_id, "request ID"))
        if not isinstance(self.available, bool):
            raise AdvisorValidationError("Invalid available flag")
        status = _enum(self.status, AdvisorStatus, "advisor status")
        object.__setattr__(self, "status", status)
        evaluation_time = _utc(self.evaluation_time, "evaluation_time")
        generated_at = _utc(self.generated_at, "generated_at")
        if generated_at != evaluation_time:
            raise AdvisorValidationError("generated_at must equal evaluation_time for deterministic evaluation")
        object.__setattr__(self, "evaluation_time", evaluation_time)
        object.__setattr__(self, "generated_at", generated_at)
        if self.available and status in {AdvisorStatus.UNAVAILABLE, AdvisorStatus.ERROR}:
            raise AdvisorValidationError("Available response cannot have unavailable/error status")
        if not self.available and status in {AdvisorStatus.FRESH, AdvisorStatus.PARTIAL, AdvisorStatus.STALE}:
            raise AdvisorValidationError("Unavailable response requires unavailable/error status")
        object.__setattr__(self, "freshness_deadline", _utc(self.freshness_deadline, "freshness_deadline") if self.freshness_deadline is not None else None)
        object.__setattr__(self, "scope", _enum(self.scope, AdvisorScope, "advisor scope"))
        findings = tuple(sorted(self.findings, key=lambda item: (item.category, item.severity.rank, item.confidence.rank, item.finding_id)))
        if len(findings) > MAX_FINDINGS or len({item.finding_id for item in findings}) != len(findings):
            raise AdvisorValidationError("Invalid findings collection")
        object.__setattr__(self, "findings", findings)
        recommendations = tuple(sorted(self.recommendations, key=lambda item: (item.priority, item.category, item.confidence.rank, item.recommendation_id)))
        if len(recommendations) > MAX_RECOMMENDATIONS or len({item.recommendation_id for item in recommendations}) != len(recommendations):
            raise AdvisorValidationError("Invalid recommendations collection")
        for recommendation in recommendations:
            recommendation.validate_against_findings(findings)
        object.__setattr__(self, "recommendations", recommendations)
        uncertainties = tuple(sorted(self.uncertainties, key=lambda item: (item.kind.value, item.uncertainty_id)))
        if len(uncertainties) > MAX_UNCERTAINTIES or len({item.uncertainty_id for item in uncertainties}) != len(uncertainties):
            raise AdvisorValidationError("Invalid response uncertainty collection")
        object.__setattr__(self, "uncertainties", uncertainties)
        provenance = tuple(sorted(self.provenance, key=lambda item: (item.source_id, item.provenance_ref_id)))
        if len(provenance) > MAX_PROVENANCE or len({item.provenance_ref_id for item in provenance}) != len(provenance):
            raise AdvisorValidationError("Invalid response provenance collection")
        object.__setattr__(self, "provenance", provenance)
        uncertainty_ids = {item.uncertainty_id for item in uncertainties}
        provenance_ids = {item.provenance_ref_id for item in provenance}
        for finding in findings:
            if not set(finding.uncertainty_refs).issubset(uncertainty_ids) or not set(finding.provenance_refs).issubset(provenance_ids):
                raise AdvisorValidationError("Finding references response data outside envelope")
        for recommendation in recommendations:
            if not set(recommendation.uncertainty_refs).issubset(uncertainty_ids) or not set(recommendation.provenance_refs).issubset(provenance_ids):
                raise AdvisorValidationError("Recommendation references response data outside envelope")
        coverage = tuple(sorted(self.evidence_coverage, key=lambda item: item.source_id))
        object.__setattr__(self, "evidence_coverage", coverage)
        object.__setattr__(self, "links", _links(self.links))
        if self.next_cursor is not None:
            object.__setattr__(self, "next_cursor", _normalize_identifier(self.next_cursor, "next cursor"))

    def validate_against_bundle(self, bundle: EvidenceBundle) -> None:
        if bundle.evaluation_time != self.evaluation_time or bundle.scope is not self.scope:
            raise AdvisorValidationError("Response evaluation context differs from evidence bundle")
        bundle.validate_response(self)

    def canonical(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "available": self.available,
            "status": self.status.value,
            "evaluation_time": self.evaluation_time.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "freshness_deadline": self.freshness_deadline.isoformat() if self.freshness_deadline else None,
            "scope": self.scope.value,
            "findings": [item.canonical() for item in self.findings],
            "recommendations": [item.canonical() for item in self.recommendations],
            "uncertainties": [item.canonical() for item in self.uncertainties],
            "provenance": [item.canonical() for item in self.provenance],
            "evidence_coverage": [item.canonical() for item in self.evidence_coverage],
            "links": [item.canonical() for item in self.links],
            "next_cursor": self.next_cursor,
        }

    def canonical_json(self) -> str:
        return _canonical(self.canonical())

    @property
    def stable_id(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


__all__ = [
    "AdvisorCategory",
    "AdvisorResponse",
    "AdvisorScope",
    "AdvisorStatus",
    "AdvisorValidationError",
    "Confidence",
    "ConfidenceImpact",
    "Coverage",
    "EvidenceBundle",
    "EvidenceItem",
    "EvidenceState",
    "Finding",
    "FindingSeverity",
    "ProvenanceReference",
    "Recommendation",
    "RecommendationStatus",
    "SafeLink",
    "Uncertainty",
    "UncertaintyKind",
]

# End of pure advisor domain contract module.
