from __future__ import annotations

from datetime import datetime, timezone

from aipm.control_plane.identity import EXPECTED_EFFECT, plan_id as canonical_plan_id
from aipm.control_plane.models import (
    ActionPlan,
    ActionRequest,
    ControlPlaneError,
    EvidenceSource,
    EvidenceState,
    EvidenceSummary,
    PLAN_TTL,
    PlanState,
    PlanningErrorCode,
)
from aipm.control_plane.policy import risk_for, validate_operation


class _FrozenPlannerType(type):
    """Prevent ordinary callers from replacing planner class behavior."""

    def __new__(mcls, name, bases, namespace):
        cls = super().__new__(mcls, name, bases, namespace)
        type.__setattr__(cls, "_behavior_frozen", True)
        return cls

    def __setattr__(cls, name, value):
        if getattr(cls, "_behavior_frozen", False):
            raise TypeError("PlanOnlyPlanner behavior is immutable")
        super().__setattr__(name, value)

    def __delattr__(cls, name):
        if getattr(cls, "_behavior_frozen", False):
            raise TypeError("PlanOnlyPlanner behavior is immutable")
        super().__delattr__(name)


def _build_evidence_neutral_plan(
    request: ActionRequest,
    *,
    clock,
    target_allow_list: frozenset[str],
) -> ActionPlan:
    if not isinstance(request, ActionRequest):
        raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid action request")
    try:
        validate_operation(request.operation)
    except ValueError as exc:
        raise ControlPlaneError(PlanningErrorCode.UNSUPPORTED_OPERATION, "Unsupported operation") from exc
    if request.target_id not in target_allow_list:
        raise ControlPlaneError(PlanningErrorCode.UNAVAILABLE_TARGET, "Target is not allow-listed")
    value = clock()
    now = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    evidence = EvidenceSummary(EvidenceState.NOT_OBSERVED, ())
    expires_at = now + PLAN_TTL
    risk = risk_for(request.operation)
    expected_effect = EXPECTED_EFFECT
    plan_id = canonical_plan_id(
        request=request,
        evidence=evidence,
        evidence_source=EvidenceSource.NONE,
        risk=risk,
        expected_effect=expected_effect,
        created_at=now,
        expires_at=expires_at,
        state=PlanState.PLANNED,
    )
    draft = ActionPlan(
        plan_id=plan_id,
        request=request,
        risk=risk,
        evidence=evidence,
        evidence_source=EvidenceSource.NONE,
        expected_effect=expected_effect,
        expires_at=expires_at,
        created_at=now,
        state=PlanState.PLANNED,
    )
    return ActionPlan(
        plan_id=draft.plan_id,
        request=draft.request,
        risk=draft.risk,
        evidence=draft.evidence,
        evidence_source=draft.evidence_source,
        expected_effect=draft.expected_effect,
        expires_at=draft.expires_at,
        created_at=draft.created_at,
        state=draft.state,
        digest=draft.computed_digest(),
    )


class PlanOnlyPlanner(metaclass=_FrozenPlannerType):
    """Create immutable plans without invoking providers or mutation services.

    MC-6.12A has no authoritative observation producer yet; absent an approved
    future composition seam, plans deliberately carry NOT_OBSERVED evidence.
    Caller-supplied evidence is not accepted as a planning input.
    """

    __slots__ = ("_clock", "_target_allow_list")

    def __init__(self, *, clock=None, target_allow_list: set[str] | frozenset[str] | None = None):
        object.__setattr__(self, "_clock", clock or (lambda: datetime.now(timezone.utc)))
        targets = frozenset(target_allow_list or ())
        if not targets:
            raise ValueError("An explicit target allow-list is required")
        object.__setattr__(self, "_target_allow_list", targets)

    def __setattr__(self, name, value):
        raise TypeError("PlanOnlyPlanner configuration is immutable")

    @property
    def target_allow_list(self) -> frozenset[str]:
        return self._target_allow_list

    def plan(self, request: ActionRequest) -> ActionPlan:
        return _build_evidence_neutral_plan(
            request,
            clock=self._clock,
            target_allow_list=self._target_allow_list,
        )
