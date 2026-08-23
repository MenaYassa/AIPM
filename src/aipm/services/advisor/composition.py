"""Pure MC-6.13 Phase 4A composition boundary.

This module only orchestrates the landed Phase 2 normalizer and Phase 3 rule
engine. It does not collect observations, own domain semantics, or access any
runtime, provider, persistence, authority, or clock boundary.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from aipm.models.advisor import AdvisorResponse, AdvisorScope, AdvisorValidationError, ProvenanceReference
from aipm.services.advisor.normalizer import EvidenceNormalizer
from aipm.services.advisor.rules import AdvisorRuleEngine, ResourceHistoryEnvelope


MAX_COMPOSITION_OBSERVATIONS = 512
MAX_COMPOSITION_HISTORY_ENVELOPES = 128
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")


class CompositionError(ValueError):
    """Raised when the Phase 4A request envelope is invalid."""


def _aware(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CompositionError(f"{name} must be timezone-aware")
    return value


def _freeze(value: Any) -> Any:
    """Snapshot supported mapping/sequence containers without changing values."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


def _frozen_observations(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (tuple, list)):
        raise CompositionError("observations must be a bounded sequence")
    if len(value) > MAX_COMPOSITION_OBSERVATIONS:
        raise CompositionError("observations exceed the Phase 4A bound")
    frozen: list[Mapping[str, Any]] = []
    for observation in value:
        if not isinstance(observation, Mapping):
            raise CompositionError("observations must contain mappings")
        frozen.append(_freeze(observation))
    return tuple(frozen)


def _frozen_expected_sources(value: Any) -> tuple[str, ...] | Mapping[str, int]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        copied: dict[str, int] = {}
        for source, count in value.items():
            if not isinstance(source, str) or not isinstance(count, int) or isinstance(count, bool):
                raise CompositionError("expected_sources contains an invalid entry")
            copied[source] = count
        return MappingProxyType(copied)
    if isinstance(value, (tuple, list)):
        if any(not isinstance(source, str) for source in value):
            raise CompositionError("expected_sources must contain source identifiers")
        return tuple(value)
    raise CompositionError("expected_sources must be a bounded sequence or mapping")


def _frozen_provenance(value: Any) -> tuple[ProvenanceReference | Mapping[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, (tuple, list)):
        raise CompositionError("provenance must be a bounded sequence")
    return tuple(_freeze(item) for item in value)


@dataclass(frozen=True, slots=True)
class AdvisorCompositionRequest:
    """Immutable bounded input for the pure Phase 4A composition seam."""

    request_id: str
    evaluation_time: datetime
    observations: tuple[Mapping[str, Any], ...]
    scope: AdvisorScope = AdvisorScope.OVERVIEW
    expected_sources: tuple[str, ...] | Mapping[str, int] = ()
    provenance: tuple[ProvenanceReference | Mapping[str, Any], ...] = ()
    history_envelopes: tuple[ResourceHistoryEnvelope, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or _REQUEST_ID_RE.fullmatch(self.request_id) is None:
            raise CompositionError("Invalid request_id")
        _aware(self.evaluation_time, "evaluation_time")
        object.__setattr__(self, "observations", _frozen_observations(self.observations))
        try:
            scope = self.scope if isinstance(self.scope, AdvisorScope) else AdvisorScope(self.scope)
        except (TypeError, ValueError) as exc:
            raise CompositionError("Invalid scope") from exc
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "expected_sources", _frozen_expected_sources(self.expected_sources))
        object.__setattr__(self, "provenance", _frozen_provenance(self.provenance))
        if not isinstance(self.history_envelopes, (tuple, list)):
            raise CompositionError("history_envelopes must be a bounded sequence")
        if len(self.history_envelopes) > MAX_COMPOSITION_HISTORY_ENVELOPES:
            raise CompositionError("history_envelopes exceed the Phase 4A bound")
        if any(not isinstance(envelope, ResourceHistoryEnvelope) for envelope in self.history_envelopes):
            raise CompositionError("history_envelopes must contain typed immutable envelopes")
        object.__setattr__(self, "history_envelopes", tuple(self.history_envelopes))


def compose_advisor(request: AdvisorCompositionRequest) -> AdvisorResponse:
    """Normalize the request and evaluate the fixed Phase 3 catalog."""

    if not isinstance(request, AdvisorCompositionRequest):
        raise CompositionError("compose_advisor requires AdvisorCompositionRequest")
    bundle = EvidenceNormalizer().normalize(
        request.observations,
        evaluation_time=request.evaluation_time,
        scope=request.scope,
        expected_sources=request.expected_sources,
        provenance=request.provenance,
    )
    return AdvisorRuleEngine().evaluate(
        bundle,
        request_id=request.request_id,
        evaluation_time=request.evaluation_time,
        history_envelopes=request.history_envelopes,
    )


__all__ = [
    "AdvisorCompositionRequest",
    "CompositionError",
    "MAX_COMPOSITION_HISTORY_ENVELOPES",
    "MAX_COMPOSITION_OBSERVATIONS",
    "compose_advisor",
]
