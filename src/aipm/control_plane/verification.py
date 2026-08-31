"""Independent verification contract for the control plane.

Verification is an independent comparison between a bounded expected
post-condition and an independently observed state. It never trusts an
executor return value, exit code, provider success flag, mutation response,
or caller assertion — those are execution claims, not evidence.

The predicate set is closed: there is no scripting, no shell, no expressions,
and no URLs. For the ProjectPlan slice the control plane itself can
deterministically inspect every predicate from a fresh read of the durable
plan state. A future executor reuses this exact contract with observations
from read-only providers.

Contract version: ``mc612-verification-v1``. A result records the version it
was produced under; consumers must refuse results under unknown versions
rather than reinterpret old evidence with new semantics.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TYPE_CHECKING

from aipm.control_plane.audit.sanitize import AuditEventError, bounded_code, bounded_reference

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aipm.control_plane.project_plan import ProjectPlan

VERIFICATION_VERSION = "mc612-verification-v1"

#: Bounded verifier identity for the control plane's own read-back verifier.
PLAN_READBACK_VERIFIER = "control-plane-plan-readback"

_MAX_EVIDENCE_REFERENCES = 8
_HEX64 = "^[0-9a-f]{64}$"


class VerificationCode(str, Enum):
    """Closed set of verification outcome reasons."""

    PASSED = "passed"
    PLAN_MISSING = "plan_missing"
    TARGET_MISMATCH = "target_mismatch"
    ENVIRONMENT_MISMATCH = "environment_mismatch"
    REVISION_MISMATCH = "revision_mismatch"
    DIGEST_MISMATCH = "digest_mismatch"
    ENABLED_MISMATCH = "enabled_mismatch"
    FIELD_MISMATCH = "field_mismatch"
    INVALID_EXPECTATION = "invalid_expectation"


class ExecutionOutcome(str, Enum):
    """Typed classification of where an execution reached.

    ``UNKNOWN_OUTCOME`` is the safety-critical state: when it cannot be
    established whether the external mutation happened, blind retry of the
    mutation is forbidden and reconciliation must determine the actual state.
    """

    MUTATION_NOT_STARTED = "mutation_not_started"
    MUTATION_STARTED = "mutation_started"
    MUTATION_SUCCEEDED = "mutation_succeeded"
    MUTATION_FAILED = "mutation_failed"
    VERIFICATION_SUCCEEDED = "verification_succeeded"
    VERIFICATION_FAILED = "verification_failed"
    ROLLBACK_NOT_REQUIRED = "rollback_not_required"
    ROLLBACK_REQUESTED = "rollback_requested"
    ROLLBACK_SUCCEEDED = "rollback_succeeded"
    ROLLBACK_FAILED = "rollback_failed"
    UNKNOWN_OUTCOME = "unknown_outcome"


def retry_permitted(outcome: ExecutionOutcome | str | None) -> bool:
    """Whether a mutation retry may be attempted for the given outcome.

    Only a provably not-started mutation may be retried. ``MUTATION_STARTED``
    and ``UNKNOWN_OUTCOME`` forbid retry: the executor could not establish
    whether the external effect occurred. ``MUTATION_FAILED`` may only be
    retried once the executor has established that no effect occurred; in this
    contract that is represented by returning to ``MUTATION_NOT_STARTED``
    after reconciliation, never by direct retry.
    """

    if outcome is None:
        return True
    normalized = outcome if isinstance(outcome, ExecutionOutcome) else ExecutionOutcome(outcome)
    return normalized is ExecutionOutcome.MUTATION_NOT_STARTED


@dataclass(frozen=True, slots=True)
class ExpectedState:
    """Bounded, typed expected post-condition for one action.

    Built by the control plane from the action's mutation contract; never
    from caller-supplied scripts or expressions. Every predicate is checked
    against the closed set implicitly represented by the fields present.
    """

    target_id: str
    environment: str
    revision: int
    canonical_digest: str | None
    enabled: bool
    fields: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", bounded_reference(self.target_id, field="target id"))
        object.__setattr__(self, "environment", bounded_reference(self.environment, field="environment", maximum=32))
        if not isinstance(self.revision, int) or self.revision < 1:
            raise AuditEventError("Invalid expected revision")
        if self.canonical_digest is not None:
            object.__setattr__(self, "canonical_digest", bounded_reference(self.canonical_digest, field="expected digest", maximum=64))
        if not isinstance(self.enabled, bool):
            raise AuditEventError("Invalid expected enabled state")
        normalized = tuple(sorted((str(key), str(value)) for key, value in self.fields))
        object.__setattr__(self, "fields", normalized)

    def canonical_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "enabled": self.enabled,
            "environment": self.environment,
            "revision": self.revision,
            "target_id": self.target_id,
        }
        if self.canonical_digest is not None:
            payload["canonical_digest"] = self.canonical_digest
        if self.fields:
            payload["fields"] = [[key, value] for key, value in self.fields]
        return payload


@dataclass(frozen=True, slots=True)
class ObservedState:
    """Bounded observation of the resulting state, taken independently."""

    target_id: str
    environment: str
    revision: int
    canonical_digest: str
    enabled: bool
    fields: tuple[tuple[str, str], ...] = ()
    observed_at: datetime = datetime.min.replace(tzinfo=timezone.utc)

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", bounded_reference(self.target_id, field="target id"))
        object.__setattr__(self, "environment", bounded_reference(self.environment, field="environment", maximum=32))
        if not isinstance(self.revision, int) or self.revision < 1:
            raise AuditEventError("Invalid observed revision")
        object.__setattr__(self, "canonical_digest", bounded_reference(self.canonical_digest, field="observed digest", maximum=64))
        if not isinstance(self.enabled, bool):
            raise AuditEventError("Invalid observed enabled state")
        normalized = tuple(sorted((str(key), str(value)) for key, value in self.fields))
        object.__setattr__(self, "fields", normalized)
        if not isinstance(self.observed_at, datetime):
            raise AuditEventError("Invalid observation timestamp")
        object.__setattr__(self, "observed_at", self.observed_at if self.observed_at.tzinfo is not None else self.observed_at.replace(tzinfo=timezone.utc))


def observed_from_plan(plan: "ProjectPlan") -> ObservedState:
    """Build the observation from a fresh, independent plan read."""

    return ObservedState(
        target_id=plan.target_id,
        environment=plan.environment.value,
        revision=plan.revision,
        canonical_digest=plan.digest(),
        enabled=plan.enabled,
        fields=(("objective", plan.objective), ("title", plan.title)),
        observed_at=datetime.now(timezone.utc),
    )


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Immutable typed verification result."""

    verification_id: str
    action_id: str
    success: bool
    reason_code: VerificationCode
    expected_revision: int
    observed_revision: int | None
    expected_digest: str | None
    observed_digest: str | None
    observed_at: datetime
    verifier: str = PLAN_READBACK_VERIFIER
    verification_version: str = VERIFICATION_VERSION
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "verification_id", bounded_reference(self.verification_id, field="verification id"))
        object.__setattr__(self, "action_id", bounded_reference(self.action_id, field="action id"))
        if self.success != (self.reason_code is VerificationCode.PASSED):
            raise AuditEventError("Verification outcome and reason disagree")
        bounded_code(self.reason_code.value)
        if self.expected_digest is not None:
            object.__setattr__(self, "expected_digest", bounded_reference(self.expected_digest, field="expected digest", maximum=64))
        if self.observed_digest is not None:
            object.__setattr__(self, "observed_digest", bounded_reference(self.observed_digest, field="observed digest", maximum=64))
        if len(self.evidence_references) > _MAX_EVIDENCE_REFERENCES:
            raise AuditEventError("Too many evidence references")
        object.__setattr__(
            self,
            "evidence_references",
            tuple(bounded_reference(reference, field="evidence reference") for reference in self.evidence_references),
        )
        if not isinstance(self.observed_at, datetime):
            raise AuditEventError("Invalid verification timestamp")
        object.__setattr__(self, "observed_at", self.observed_at if self.observed_at.tzinfo is not None else self.observed_at.replace(tzinfo=timezone.utc))
        if self.verification_version != VERIFICATION_VERSION:
            raise AuditEventError("Unsupported verification version")

    def safe_dict(self) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            "action_id": self.action_id,
            "success": self.success,
            "reason_code": self.reason_code.value,
            "expected_revision": self.expected_revision,
            "observed_revision": self.observed_revision,
            "expected_digest": self.expected_digest,
            "observed_digest": self.observed_digest,
            "observed_at": self.observed_at.isoformat(),
            "verifier": self.verifier,
            "verification_version": self.verification_version,
            "evidence_references": list(self.evidence_references),
        }


def verify(expected: ExpectedState, observed: ObservedState, *, action_id: str) -> VerificationResult:
    """Independently compare the expected post-condition to the observation.

    Deterministic: identical inputs produce identical success/reason values
    (the per-occurrence verification_id is the only varying output). The first
    failing predicate in the closed order wins, so denial evidence is stable.
    """

    if expected.target_id != observed.target_id:
        code = VerificationCode.TARGET_MISMATCH
    elif expected.environment != observed.environment:
        code = VerificationCode.ENVIRONMENT_MISMATCH
    elif expected.revision != observed.revision:
        code = VerificationCode.REVISION_MISMATCH
    elif expected.canonical_digest is not None and expected.canonical_digest != observed.canonical_digest:
        code = VerificationCode.DIGEST_MISMATCH
    elif expected.enabled != observed.enabled:
        code = VerificationCode.ENABLED_MISMATCH
    else:
        expected_fields = dict(expected.fields)
        observed_fields = dict(observed.fields)
        code = VerificationCode.PASSED
        for key, value in expected_fields.items():
            if observed_fields.get(key) != value:
                code = VerificationCode.FIELD_MISMATCH
                break
    return VerificationResult(
        verification_id=secrets.token_hex(16),
        action_id=action_id,
        success=code is VerificationCode.PASSED,
        reason_code=code,
        expected_revision=expected.revision,
        observed_revision=observed.revision,
        expected_digest=expected.canonical_digest,
        observed_digest=observed.canonical_digest,
        observed_at=observed.observed_at,
    )


def expected_from_plan(plan: "ProjectPlan", *, fields: tuple[tuple[str, str], ...] = ()) -> ExpectedState:
    """Build the expected post-condition from a projected plan value."""

    return ExpectedState(
        target_id=plan.target_id,
        environment=plan.environment.value,
        revision=plan.revision,
        canonical_digest=plan.digest(),
        enabled=plan.enabled,
        fields=tuple(sorted((str(key), str(value)) for key, value in fields)),
    )
