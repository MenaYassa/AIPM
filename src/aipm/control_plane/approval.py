from __future__ import annotations


class _FrozenServiceType(type):
    """Reject ordinary class mutation for security-sensitive service behavior."""

    def __setattr__(cls, name, value):
        raise TypeError("Service behavior is immutable")

    def __delattr__(cls, name):
        raise TypeError("Service behavior is immutable")


class ApprovalService(metaclass=_FrozenServiceType):
    """Model local approval intent; planning is service-owned and never executes."""

    __slots__ = ("_clock", "_target_allow_list", "_bindings", "_max_bindings", "_initialized")

    def __init__(self, *, clock=None, target_allow_list: set[str] | frozenset[str] | None = None, max_bindings: int = 256):
        from datetime import datetime, timezone

        if max_bindings < 1 or max_bindings > 256:
            raise ValueError("Invalid approval bound")
        targets = frozenset(target_allow_list or ())
        if not targets:
            raise ValueError("An explicit target allow-list is required")
        object.__setattr__(self, "_clock", clock or (lambda: datetime.now(timezone.utc)))
        object.__setattr__(self, "_target_allow_list", targets)
        object.__setattr__(self, "_bindings", {})
        object.__setattr__(self, "_max_bindings", max_bindings)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("ApprovalService configuration is immutable")
        object.__setattr__(self, name, value)

    @property
    def store(self):
        from types import MappingProxyType

        return MappingProxyType(self._bindings)

    def request(self, request: ActionRequest, *, actor_id: str) -> ApprovalBinding:
        import hashlib
        import json
        from datetime import timezone
        from aipm.control_plane.models import APPROVAL_TTL, PLAN_TTL, ActionRequest, ApprovalBinding, ControlPlaneError, PlanningErrorCode

        if not isinstance(request, ActionRequest):
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid action request")
        if getattr(request.operation, "value", None) != "update_project_plan":
            raise ControlPlaneError(PlanningErrorCode.UNSUPPORTED_OPERATION, "Unsupported operation")
        if request.target_id not in self._target_allow_list:
            raise ControlPlaneError(PlanningErrorCode.UNAVAILABLE_TARGET, "Target is not allow-listed")
        value = self._clock()
        now = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        expires_at = now + PLAN_TTL
        risk_value = "low"
        expected_effect = "No runtime effect; produce a bounded future-operation plan only"
        request_identity = hashlib.sha256(request.canonical().encode("utf-8")).hexdigest()
        plan_payload = {
            "request_identity": request_identity,
            "operation": request.operation.value,
            "target_id": request.target_id,
            "evidence_source": "none",
            "evidence_state": "not_observed",
            "evidence": [],
            "risk": risk_value,
            "expected_effect": expected_effect,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "state": "planned",
        }
        plan_id = hashlib.sha256(json.dumps(plan_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()[:32]
        canonical_payload = {
            "plan_id": plan_id,
            "request": json.loads(request.canonical()),
            "risk": risk_value,
            "evidence_source": "none",
            "evidence_state": "not_observed",
            "evidence": [],
            "expected_effect": expected_effect,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "state": "planned",
        }
        plan_digest = hashlib.sha256(json.dumps(canonical_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()
        approval_now = self._now()
        approval_expires_at = min(expires_at, approval_now + APPROVAL_TTL)
        approval_id = hashlib.sha256(f"{plan_id}:{plan_digest}:{actor_id}:{approval_now.isoformat()}".encode("utf-8")).hexdigest()[:32]
        binding = ApprovalBinding(
            approval_id=approval_id,
            request=request,
            plan_id=plan_id,
            plan_digest=plan_digest,
            actor_id=actor_id,
            created_at=approval_now,
            expires_at=approval_expires_at,
            scope="plan_only_intent",
            state="approval_requested",
        )
        if binding.approval_id in self._bindings:
            raise ControlPlaneError(PlanningErrorCode.APPROVAL_MISMATCH, "Approval request already exists")
        if len(self._bindings) >= self._max_bindings:
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Approval bound reached")
        self._bindings[binding.approval_id] = binding
        return binding

    def approve(self, binding: ApprovalBinding) -> ApprovalBinding:
        from aipm.control_plane.models import ApprovalBinding, ApprovalState, ControlPlaneError, PlanningErrorCode

        if not isinstance(binding, ApprovalBinding):
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid approval binding")
        stored = self._stored(binding)
        self._validate_binding(stored)
        if stored.state is not ApprovalState.APPROVAL_REQUESTED:
            raise ControlPlaneError(PlanningErrorCode.APPROVAL_MISMATCH, "Approval is not requestable")
        approved = ApprovalBinding(
            approval_id=stored.approval_id,
            request=stored.request,
            plan_id=stored.plan_id,
            plan_digest=stored.plan_digest,
            actor_id=stored.actor_id,
            created_at=stored.created_at,
            expires_at=stored.expires_at,
            scope=stored.scope,
            state=ApprovalState.APPROVED,
        )
        self._bindings[approved.approval_id] = approved
        return approved

    def validate(self, binding: ApprovalBinding) -> None:
        from aipm.control_plane.models import ApprovalBinding, ApprovalState, ControlPlaneError, PlanningErrorCode

        if not isinstance(binding, ApprovalBinding):
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid approval binding")
        stored = self._stored(binding)
        self._validate_binding(stored)
        if stored.state is not ApprovalState.APPROVED:
            raise ControlPlaneError(PlanningErrorCode.APPROVAL_MISMATCH, "Approval is not approved")

    def consume(self, binding: ApprovalBinding) -> ApprovalBinding:
        from aipm.control_plane.models import ApprovalBinding, ApprovalState, ControlPlaneError, PlanningErrorCode

        if not isinstance(binding, ApprovalBinding):
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid approval binding")
        stored = self._stored(binding)
        self._validate_binding(stored)
        if stored.state is not ApprovalState.APPROVED:
            raise ControlPlaneError(PlanningErrorCode.APPROVAL_MISMATCH, "Approval is not consumable")
        consumed = ApprovalBinding(
            approval_id=stored.approval_id,
            request=stored.request,
            plan_id=stored.plan_id,
            plan_digest=stored.plan_digest,
            actor_id=stored.actor_id,
            created_at=stored.created_at,
            expires_at=stored.expires_at,
            scope=stored.scope,
            state=ApprovalState.CONSUMED,
        )
        self._bindings[consumed.approval_id] = consumed
        return consumed

    def _stored(self, binding: ApprovalBinding) -> ApprovalBinding:
        from aipm.control_plane.models import ControlPlaneError, PlanningErrorCode

        try:
            stored = self._bindings[binding.approval_id]
        except KeyError as exc:
            raise ControlPlaneError(PlanningErrorCode.APPROVAL_MISMATCH, "Unknown approval") from exc
        if stored != binding:
            raise ControlPlaneError(PlanningErrorCode.APPROVAL_MISMATCH, "Approval binding mismatch")
        return stored

    def _validate_binding(self, binding: ApprovalBinding) -> None:
        from aipm.control_plane.models import ControlPlaneError, PlanningErrorCode

        if binding.is_expired(self._now()):
            raise ControlPlaneError(PlanningErrorCode.EXPIRED_PLAN, "Plan or approval has expired")

    def _now(self) -> datetime:
        from datetime import timezone

        value = self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
