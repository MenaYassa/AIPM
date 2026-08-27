from __future__ import annotations


class _FrozenServiceType(type):
    """Reject ordinary class mutation for security-sensitive service behavior."""

    def __setattr__(cls, name, value):
        raise TypeError("Service behavior is immutable")

    def __delattr__(cls, name):
        raise TypeError("Service behavior is immutable")


class ActionAuditRepository(metaclass=_FrozenServiceType):
    """Small in-memory append-only audit boundary for the plan-only milestone."""

    __slots__ = ("_clock", "_target_allow_list", "_max_records", "_records", "_initialized")

    def __init__(self, *, clock=None, target_allow_list: set[str] | frozenset[str] | None = None, max_records: int = 256):
        from datetime import datetime, timezone

        if max_records < 1 or max_records > 256:
            raise ValueError("Invalid audit record bound")
        targets = frozenset(target_allow_list or ())
        if not targets:
            raise ValueError("An explicit target allow-list is required")
        object.__setattr__(self, "_clock", clock or (lambda: datetime.now(timezone.utc)))
        object.__setattr__(self, "_target_allow_list", targets)
        object.__setattr__(self, "_max_records", max_records)
        object.__setattr__(self, "_records", [])
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("ActionAuditRepository configuration is immutable")
        object.__setattr__(self, name, value)

    def append_plan(self, request: ActionRequest, *, actor_id: str = "system", now: datetime | None = None) -> ActionAuditRecord:
        import hashlib
        import json
        from datetime import timedelta, timezone
        from aipm.control_plane.models import ActionAuditRecord, ActionRequest, AuditState, ControlPlaneError, PlanningErrorCode

        if not isinstance(request, ActionRequest):
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid action request")
        if getattr(request.operation, "value", None) != "update_project_plan":
            raise ControlPlaneError(PlanningErrorCode.UNSUPPORTED_OPERATION, "Unsupported operation")
        if request.target_id not in self._target_allow_list:
            raise ControlPlaneError(PlanningErrorCode.UNAVAILABLE_TARGET, "Target is not allow-listed")
        value = now or self._clock()
        created_at = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        expires_at = created_at + timedelta(minutes=15)
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
            "created_at": created_at.isoformat(),
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
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "state": "planned",
        }
        plan_digest = hashlib.sha256(json.dumps(canonical_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()
        return self._append(
            plan_id=plan_id,
            plan_digest=plan_digest,
            request=request,
            actor_id=actor_id,
            state=AuditState.PLANNED,
            risk=risk_value,
            evidence_state="not_observed",
            outcome_code="plan_created",
            now=now,
        )

    def append_approval_requested(self, binding: ApprovalBinding, *, now: datetime | None = None) -> ActionAuditRecord:
        from aipm.control_plane.models import ApprovalBinding, AuditState, ControlPlaneError, PlanningErrorCode

        if not isinstance(binding, ApprovalBinding):
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid approval binding")
        return self._append_binding(binding, state=AuditState.APPROVAL_REQUESTED, outcome_code="approval_intent_recorded", now=now)

    def append_approved(self, binding: ApprovalBinding, *, now: datetime | None = None) -> ActionAuditRecord:
        from aipm.control_plane.models import ApprovalBinding, AuditState, ControlPlaneError, PlanningErrorCode

        if not isinstance(binding, ApprovalBinding):
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid approval binding")
        return self._append_binding(binding, state=AuditState.APPROVED, outcome_code="approval_intent_accepted", now=now)

    def records(self) -> tuple[ActionAuditRecord, ...]:
        return tuple(self._records)

    def safe_records(self) -> tuple[dict[str, str], ...]:
        return tuple(record.safe_dict() for record in self._records)

    def _append_binding(self, binding: ApprovalBinding, *, state: AuditState, outcome_code: str, now: datetime | None) -> ActionAuditRecord:
        from aipm.control_plane.models import ApprovalBinding, ControlPlaneError, PlanningErrorCode

        if not isinstance(binding, ApprovalBinding):
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid approval binding")
        return self._append(
            plan_id=binding.plan_id,
            plan_digest=binding.plan_digest,
            request=binding.request,
            actor_id=binding.actor_id,
            state=state,
            risk="low",
            evidence_state="not_observed",
            outcome_code=outcome_code,
            now=now,
        )

    def _append(self, *, plan_id, plan_digest, request, actor_id, state, risk, evidence_state, outcome_code, now):
        from aipm.control_plane.models import ActionAuditRecord, ControlPlaneError, PlanningErrorCode

        if len(self._records) >= self._max_records:
            raise ValueError("Audit record bound reached")
        timestamp = now or self._clock()
        record = ActionAuditRecord(
            action_id=plan_id,
            plan_id=plan_id,
            plan_digest=plan_digest,
            operation=request.operation,
            target_id=request.target_id,
            actor_id=actor_id,
            timestamp=timestamp,
            state=state,
            risk=risk,
            evidence_state=evidence_state,
            outcome_code=outcome_code,
        )
        if getattr(getattr(record, "evidence_state", None), "value", None) != "not_observed":
            raise ControlPlaneError(PlanningErrorCode.INVALID_PLAN, "Audit evidence must remain not observed")
        self._records.append(record)
        return record


class Stage2AuditRepository:
    """Test/local append-only audit collection; not durable production state."""

    __slots__ = ("_max_records", "_records", "_initialized")

    def __init__(self, *, max_records: int = 256):
        if not isinstance(max_records, int) or max_records < 1 or max_records > 256:
            raise ValueError("Invalid Stage 2 audit record bound")
        object.__setattr__(self, "_max_records", max_records)
        object.__setattr__(self, "_records", [])
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("Stage 2 audit repository is immutable")
        object.__setattr__(self, name, value)

    def append(self, event):
        from aipm.control_plane.models import LifecycleError, Stage2AuditEvent

        if not isinstance(event, Stage2AuditEvent):
            raise LifecycleError("Invalid Stage 2 audit event")
        if len(self._records) >= self._max_records:
            raise LifecycleError("Stage 2 audit record bound reached")
        if any(existing.event_id == event.event_id for existing in self._records):
            raise LifecycleError("Duplicate Stage 2 audit event")
        self._records.append(event)
        return event

    def records(self):
        return tuple(self._records)

    def safe_records(self):
        return tuple(event.safe_dict() for event in self._records)
