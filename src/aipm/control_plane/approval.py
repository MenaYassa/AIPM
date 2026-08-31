"""Canonical owner confirmation service.

This service binds explicit single-owner confirmation to an
``AuthorizationDecision`` that the composition already issued. It never
re-derives plan identifiers, digests, or actor identity from raw request
fields: the canonical action identity arrives inside the decision. A forged or
modified decision cannot be confirmed because only decisions recorded by the
issuing composition are accepted, and bindings are matched by exact equality.

Confirmation semantics are explicit: in OWNER_CONFIRMATION mode the confirmer
must be the authenticated requester itself. DISTINCT_APPROVAL is the reserved
future mode requiring a different subject. Neither mode executes anything.

Binding persistence goes through a ``ConfirmationStore``; the default store is
in-memory (test double), and the durable action repository implements the same
contract. No secret, session identifier, or cookie is ever stored.
"""
from __future__ import annotations


class _FrozenServiceType(type):
    """Reject ordinary class mutation for security-sensitive service behavior."""

    def __setattr__(cls, name, value):
        raise TypeError("Service behavior is immutable")

    def __delattr__(cls, name):
        raise TypeError("Service behavior is immutable")


class InMemoryConfirmationStore:
    """Bounded in-memory confirmation store (test double)."""

    def __init__(self, *, max_bindings: int = 256) -> None:
        if max_bindings < 1 or max_bindings > 256:
            raise ValueError("Invalid confirmation bound")
        self._max_bindings = max_bindings
        self._bindings: dict = {}

    def put(self, binding) -> None:
        self._bindings[binding.confirmation_id] = binding

    def get(self, confirmation_id: str):
        return self._bindings.get(confirmation_id)

    def has_active_for_action(self, action_id: str) -> bool:
        from aipm.control_plane.models import ConfirmationState

        return any(
            binding.action_id == action_id
            and binding.state in {ConfirmationState.CONFIRMATION_REQUESTED, ConfirmationState.CONFIRMED}
            for binding in self._bindings.values()
        )

    def get_active_for_action(self, action_id: str):
        from aipm.control_plane.models import ConfirmationState

        for binding in self._bindings.values():
            if binding.action_id == action_id and binding.state is ConfirmationState.CONFIRMATION_REQUESTED:
                return binding
        return None

    def count(self) -> int:
        return len(self._bindings)

    def as_mapping(self):
        from types import MappingProxyType

        return MappingProxyType(dict(self._bindings))


class OwnerConfirmationService(metaclass=_FrozenServiceType):
    """Single-use, TTL-bounded confirmation bound to a canonical decision."""

    __slots__ = ("_clock", "_store", "_max_bindings", "_initialized")

    def __init__(self, *, clock=None, max_bindings: int = 256, store=None):
        from datetime import datetime, timezone

        from aipm.control_plane.contracts import ConfirmationStore

        if max_bindings < 1 or max_bindings > 256:
            raise ValueError("Invalid confirmation bound")
        if store is not None and not isinstance(store, ConfirmationStore):
            raise TypeError("store must implement the ConfirmationStore contract")
        object.__setattr__(self, "_clock", clock or (lambda: datetime.now(timezone.utc)))
        object.__setattr__(self, "_store", store if store is not None else InMemoryConfirmationStore(max_bindings=max_bindings))
        object.__setattr__(self, "_max_bindings", max_bindings)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("OwnerConfirmationService configuration is immutable")
        object.__setattr__(self, name, value)

    @property
    def store(self):
        return self._store.as_mapping()

    def request_confirmation(self, decision, *, requester_subject: str, now=None):
        """Record a pending confirmation for an allowed, unexpired decision."""

        from datetime import timezone

        from aipm.control_plane.identity import ActionIdentity
        from aipm.control_plane.models import (
            CONFIRMATION_TTL,
            ConfirmationBinding,
            ConfirmationKind,
            ConfirmationState,
            ControlPlaneError,
            PlanningErrorCode,
        )
        from aipm.control_plane.policy import AuthorizationDecision

        if not isinstance(decision, AuthorizationDecision):
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Unknown authorization decision")
        if not isinstance(requester_subject, str) or not requester_subject:
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid requester subject")
        if not decision.allowed or not decision.confirmation_required:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Decision is not confirmable")
        if decision.action_identity is None or not isinstance(decision.action_identity, ActionIdentity):
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Decision carries no action identity")
        if requester_subject != decision.principal_subject:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Requester does not match the decision subject")
        now = self._now() if now is None else (now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc))
        if decision.is_expired(now):
            raise ControlPlaneError(PlanningErrorCode.EXPIRED_PLAN, "Authorization decision has expired")
        identity = decision.action_identity
        existing_pending = self._store.get_active_for_action(identity.action_id)
        if existing_pending is not None:
            if existing_pending.decision_id == decision.decision_id and existing_pending.state is ConfirmationState.CONFIRMATION_REQUESTED:
                # Idempotent re-request: a crash between the pending write and
                # the atomic confirm composite can be retried safely.
                return existing_pending
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Confirmation already exists for this action")
        if self._store.has_active_for_action(identity.action_id):
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Confirmation already exists for this action")
        if self._store.count() >= self._max_bindings:
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Confirmation bound reached")
        import hashlib

        confirmation_id = hashlib.sha256(
            f"{identity.action_id}:{decision.decision_id}:{requester_subject}:{now.isoformat()}".encode("utf-8")
        ).hexdigest()[:32]
        binding = ConfirmationBinding(
            confirmation_id=confirmation_id,
            decision_id=decision.decision_id,
            action_id=identity.action_id,
            plan_id=identity.plan_id,
            plan_digest=identity.plan_digest,
            target_revision=identity.target_revision,
            target_digest=identity.target_digest,
            policy_version=identity.policy_version,
            requester_subject=requester_subject,
            confirmation_kind=ConfirmationKind(decision.confirmation_kind.value),
            request=decision_request(decision),
            created_at=now,
            expires_at=min(decision.expires_at, now + CONFIRMATION_TTL),
            state=ConfirmationState.CONFIRMATION_REQUESTED,
        )
        if self._store.get(binding.confirmation_id) is not None:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Confirmation already exists")
        self._store.put(binding)
        return binding

    def build_confirmation(self, binding, *, confirmed_by_subject: str, now=None):
        """Validate the confirmation act and return the confirmed value.

        Pure domain step: no persistence happens here. The composition uses
        this so the durable binding write and the lifecycle transition can be
        committed atomically by the action repository.
        """

        from datetime import timezone

        from aipm.control_plane.models import (
            ConfirmationBinding,
            ConfirmationKind,
            ConfirmationState,
            ControlPlaneError,
            PlanningErrorCode,
        )

        if not isinstance(binding, ConfirmationBinding):
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid confirmation binding")
        if not isinstance(confirmed_by_subject, str) or not confirmed_by_subject:
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid confirmer subject")
        stored = self._stored(binding)
        self._validate_binding(stored, now=now)
        if stored.state is not ConfirmationState.CONFIRMATION_REQUESTED:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Confirmation is not requestable")
        if stored.confirmation_kind is ConfirmationKind.OWNER_CONFIRMATION:
            if confirmed_by_subject != stored.requester_subject:
                raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Only the authenticated owner may confirm this action")
        else:
            if confirmed_by_subject == stored.requester_subject:
                raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Distinct confirmation requires a different subject")
        from dataclasses import replace

        return replace(stored, confirmed_by_subject=confirmed_by_subject, state=ConfirmationState.CONFIRMED)

    def confirm(self, binding, *, confirmed_by_subject: str, now=None):
        """Record the explicit confirmation act against the stored binding."""

        confirmed = self.build_confirmation(binding, confirmed_by_subject=confirmed_by_subject, now=now)
        self._store.put(confirmed)
        return confirmed

    def validate(self, binding, *, now=None) -> None:
        from aipm.control_plane.models import ConfirmationBinding, ConfirmationState, ControlPlaneError, PlanningErrorCode

        if not isinstance(binding, ConfirmationBinding):
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid confirmation binding")
        stored = self._stored(binding)
        self._validate_binding(stored, now=now)
        if stored.state is not ConfirmationState.CONFIRMED:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Confirmation is not confirmed")

    def consume(self, binding, *, now=None):
        """Consume the confirmation exactly once; consumption is not execution."""

        from dataclasses import replace

        from aipm.control_plane.models import ConfirmationBinding, ConfirmationState, ControlPlaneError, PlanningErrorCode

        if not isinstance(binding, ConfirmationBinding):
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid confirmation binding")
        stored = self._stored(binding)
        self._validate_binding(stored, now=now)
        if stored.state is not ConfirmationState.CONFIRMED:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Confirmation is not consumable")
        consumed = replace(stored, state=ConfirmationState.CONSUMED)
        self._store.put(consumed)
        return consumed

    def _stored(self, binding):
        from aipm.control_plane.models import ControlPlaneError, PlanningErrorCode

        try:
            stored = self._store.get(binding.confirmation_id)
        except (KeyError, AttributeError) as exc:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Unknown confirmation") from exc
        if stored is None:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Unknown confirmation")
        if stored != binding:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Confirmation binding mismatch")
        return stored

    def _validate_binding(self, binding, *, now=None) -> None:
        from datetime import timezone

        from aipm.control_plane.models import ControlPlaneError, PlanningErrorCode

        moment = self._now() if now is None else (now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc))
        if binding.is_expired(moment):
            raise ControlPlaneError(PlanningErrorCode.EXPIRED_PLAN, "Plan or confirmation has expired")

    def _now(self):
        from datetime import timezone

        value = self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def decision_request(decision):
    """Return the exact request carried by a decision; no reconstruction happens."""

    from aipm.control_plane.models import ActionRequest
    from aipm.control_plane.policy import AuthorizationDecision

    if not isinstance(decision, AuthorizationDecision) or not isinstance(decision.request, ActionRequest):
        raise ValueError("decision carries no request binding")
    return decision.request
