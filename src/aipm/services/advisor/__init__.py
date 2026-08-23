"""Pure, non-authoritative MC-6.13 advisor service package boundary."""

from aipm.services.advisor.normalizer import EvidenceNormalizer, NormalizationError, normalize_observations
from aipm.services.advisor.rules import (
    AdvisorRuleEngine,
    PHASE3_FIELD_SCHEMA,
    RESOURCE_THRESHOLDS,
    RULE_CATALOG,
    RULE_SET_VERSION,
    RULE_VERSION,
    ResourceHistoryEnvelope,
    ResourceHistoryPoint,
    RuleDefinition,
    SUSTAINED_MAX_GAP_MULTIPLIER,
    SUSTAINED_MIN_DURATION_SECONDS,
    SUSTAINED_MIN_POINTS,
)

__all__ = (
    "AdvisorRuleEngine",
    "EvidenceNormalizer",
    "NormalizationError",
    "PHASE3_FIELD_SCHEMA",
    "RESOURCE_THRESHOLDS",
    "RULE_CATALOG",
    "RULE_SET_VERSION",
    "RULE_VERSION",
    "ResourceHistoryEnvelope",
    "ResourceHistoryPoint",
    "RuleDefinition",
    "SUSTAINED_MAX_GAP_MULTIPLIER",
    "SUSTAINED_MIN_DURATION_SECONDS",
    "SUSTAINED_MIN_POINTS",
    "normalize_observations",
)
