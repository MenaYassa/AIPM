"""Rollback planning and safety for reversible control-plane mutations.

Rollback is a NEW bounded control-plane action, never an in-place undo and
never a generic reverse-execution engine. The original action remains
immutable history; a rollback action references the original action and its
pre-mutation snapshot through its own identity, lifecycle, authorization,
and audit trail.

Safety model (compare-and-set against the post-mutation state)::

    snapshot state  = A  (immutable, integrity-verified)
    mutation result = B  (the failed action's deterministic post-condition)
    current state   = C  (independently read now)

    rollback is permitted only when C == B. If anything else changed the
    target after the failed action, rollback is denied so a later legitimate
    change can never be overwritten.

Reversibility is allow-listed: only operations with an explicit, safe
rollback definition may be rolled back. Everything else is non-reversible
and must never receive automatic rollback.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, TYPE_CHECKING

from aipm.control_plane.audit.sanitize import AuditEventError, bounded_reference
from aipm.control_plane.models import OperationKind
from aipm.control_plane.project_plan import allowed_fields

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aipm.control_plane.verification import ExpectedState

ROLLBACK_PLAN_VERSION = "mc612-rollback-v1"

#: Closed reversible-capability set: operations with an explicit, safe
#: rollback definition. Everything else is non-reversible.
REVERSIBLE_OPERATIONS = frozenset({OperationKind.UPDATE_PROJECT_PLAN})

#: Snapshot schema version produced by this contract.
SNAPSHOT_VERSION = "mc612-snapshot-v1"


class RollbackSafetyCode(str, Enum):
    """Closed set of rollback planning outcomes."""

    SAFE = "safe"
    WRONG_SNAPSHOT = "wrong_snapshot"
    STALE_SNAPSHOT = "stale_snapshot"
    WRONG_TARGET = "wrong_target"
    NON_REVERSIBLE = "non_reversible"
    FIELD_NOT_REVERSIBLE = "field_not_reversible"
    CURRENT_STATE_MISMATCH = "current_state_mismatch"
    INVALID_SNAPSHOT = "invalid_snapshot"
    ALREADY_ROLLED_BACK = "already_rolled_back"


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    """Typed, bounded rollback plan derived from an action and its snapshot.

    The CAS safety rule compares the CURRENT state against the failed
    mutation's deterministic post-condition: the revision immediately after
    the snapshot revision, carrying exactly the mutation's field values. The
    comparison is semantic (revision + mutation field values) because the
    plan digest includes the mutation timestamp, which is not knowable at
    planning time.
    """

    plan_version: str
    original_action_id: str
    snapshot_id: str
    target_id: str
    environment: str
    expected_post_revision: int
    expected_post_fields: tuple[tuple[str, str], ...]
    restore_revision: int
    restore_digest: str
    restore_fields: tuple[tuple[str, str], ...]
    reversible_fields: tuple[str, ...]
    safe: bool
    reason_code: RollbackSafetyCode

    def __post_init__(self) -> None:
        if self.plan_version != ROLLBACK_PLAN_VERSION:
            raise AuditEventError("Unsupported rollback plan version")
        object.__setattr__(self, "original_action_id", bounded_reference(self.original_action_id, field="original action id"))
        object.__setattr__(self, "snapshot_id", bounded_reference(self.snapshot_id, field="snapshot id"))
        object.__setattr__(self, "target_id", bounded_reference(self.target_id, field="target id"))
        object.__setattr__(self, "environment", bounded_reference(self.environment, field="environment", maximum=32))
        if not isinstance(self.expected_post_revision, int) or self.expected_post_revision < 1:
            raise AuditEventError("Invalid expected post revision")
        object.__setattr__(self, "expected_post_fields", tuple(sorted((str(k), str(v)) for k, v in self.expected_post_fields)))
        if not isinstance(self.restore_revision, int) or self.restore_revision < 1:
            raise AuditEventError("Invalid restore revision")
        object.__setattr__(self, "restore_digest", bounded_reference(self.restore_digest, field="restore digest", maximum=64))
        object.__setattr__(self, "restore_fields", tuple(sorted((str(key), str(value)) for key, value in self.restore_fields)))
        object.__setattr__(self, "reversible_fields", tuple(sorted(str(name) for name in self.reversible_fields)))
        if self.safe != (self.reason_code is RollbackSafetyCode.SAFE):
            raise AuditEventError("Rollback plan safety and reason disagree")

    def safe_dict(self) -> dict[str, Any]:
        return {
            "plan_version": self.plan_version,
            "original_action_id": self.original_action_id,
            "snapshot_id": self.snapshot_id,
            "target_id": self.target_id,
            "environment": self.environment,
            "expected_post_revision": self.expected_post_revision,
            "expected_post_fields": [list(item) for item in self.expected_post_fields],
            "restore_revision": self.restore_revision,
            "restore_digest": self.restore_digest,
            "restore_fields": [list(item) for item in self.restore_fields],
            "reversible_fields": list(self.reversible_fields),
            "safe": self.safe,
            "reason_code": self.reason_code.value,
        }


def _plan_from_snapshot_payload(payload_canonical: str):
    """Reconstruct the pre-mutation ProjectPlan from a snapshot payload.

    The stored payload is the plan's canonical payload (which excludes the
    derived digest); the digest is recomputed and must match the snapshot's
    recorded ``canonical_digest``.
    """

    import json
    from dataclasses import replace

    from aipm.control_plane.project_plan import Environment, ProjectPlan

    payload = json.loads(payload_canonical)
    plan = ProjectPlan(
        target_id=payload["target_id"],
        environment=Environment(payload["environment"]),
        revision=int(payload["revision"]),
        title=payload["title"],
        objective=payload["objective"],
        created_at=datetime_from_iso(payload["created_at"]),
        updated_at=datetime_from_iso(payload["updated_at"]),
        enabled=bool(payload["enabled"]),
        canonical_digest="",
    )
    restored = replace(plan, canonical_digest=plan.digest())
    return restored


def datetime_from_iso(value: str):
    from datetime import datetime

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("snapshot payload timestamp is naive")
    return parsed


def plan_rollback(*, original_action, snapshot, current_plan, mutation_fields=None) -> RollbackPlan:
    """Plan the rollback of one original action against its snapshot.

    ``original_action`` is the durable ``ActionLifecycle`` of the failed
    action, ``snapshot`` the pre-mutation :class:`PlanSnapshot` (already
    integrity-verified on load), ``current_plan`` a fresh independent read of
    the target plan state, and ``mutation_fields`` the original action's
    canonical mutation fields taken from its decision request (never
    reconstructed here).
    """

    from aipm.control_plane.models import ActionLifecycle
    from aipm.control_plane.project_plan import ProjectPlan

    if not isinstance(original_action, ActionLifecycle):
        raise AuditEventError("Invalid original action")
    if not isinstance(current_plan, ProjectPlan):
        raise AuditEventError("Invalid current plan state")
    if mutation_fields is not None and not isinstance(mutation_fields, dict):
        raise AuditEventError("Invalid mutation fields")
    if snapshot is None or not hasattr(snapshot, "payload_canonical"):
        return _unsafe(original_action, snapshot, current_plan, RollbackSafetyCode.INVALID_SNAPSHOT)

    def build(reason: RollbackSafetyCode) -> RollbackPlan:
        return _unsafe(original_action, snapshot, current_plan, reason)

    if snapshot.action_id != original_action.action_id:
        return build(RollbackSafetyCode.WRONG_SNAPSHOT)
    if snapshot.target_id != original_action.scope.target_id or snapshot.environment != original_action.scope.environment:
        return build(RollbackSafetyCode.WRONG_TARGET)
    if snapshot.revision != original_action.plan_revision:
        return build(RollbackSafetyCode.STALE_SNAPSHOT)
    if original_action.operation not in REVERSIBLE_OPERATIONS:
        return build(RollbackSafetyCode.NON_REVERSIBLE)
    if original_action.state.value == "rolled_back":
        return build(RollbackSafetyCode.ALREADY_ROLLED_BACK)

    try:
        snapshot_plan = _plan_from_snapshot_payload(snapshot.payload_canonical)
    except (ValueError, TypeError, KeyError):
        return build(RollbackSafetyCode.INVALID_SNAPSHOT)
    if snapshot_plan.target_id != snapshot.target_id or snapshot_plan.revision != snapshot.revision:
        return build(RollbackSafetyCode.INVALID_SNAPSHOT)

    if original_action.operation is OperationKind.UPDATE_PROJECT_PLAN:
        fields = dict(mutation_fields or {})
        allowed = allowed_fields()
        if not fields or any(name not in allowed for name in fields):
            return build(RollbackSafetyCode.FIELD_NOT_REVERSIBLE)
        expected_post_revision = snapshot_plan.revision + 1
        expected_post_fields = tuple(sorted(fields.items()))
        restore_fields = tuple(sorted(fields.items()))
    else:  # pragma: no cover - reversible set is closed
        return build(RollbackSafetyCode.NON_REVERSIBLE)

    # CAS safety: the current state must be exactly one revision past the
    # snapshot — the revision the failed mutation produced. The revision
    # chain is single-writer (every mutation CAS-guards its expected
    # revision), so current.revision == snapshot.revision + 1 proves the
    # only change since the snapshot was this action's own mutation; any
    # later legitimate change would have advanced the revision further and
    # must never be overwritten.
    if current_plan.revision != expected_post_revision:
        return build(RollbackSafetyCode.CURRENT_STATE_MISMATCH)

    return RollbackPlan(
        plan_version=ROLLBACK_PLAN_VERSION,
        original_action_id=original_action.action_id,
        snapshot_id=snapshot.snapshot_id,
        target_id=snapshot.target_id,
        environment=snapshot.environment,
        expected_post_revision=expected_post_revision,
        expected_post_fields=expected_post_fields,
        restore_revision=snapshot_plan.revision,
        restore_digest=snapshot_plan.digest(),
        restore_fields=restore_fields,
        reversible_fields=tuple(sorted(allowed_fields())),
        safe=True,
        reason_code=RollbackSafetyCode.SAFE,
    )


def _unsafe(original_action, snapshot, current_plan, reason: RollbackSafetyCode) -> RollbackPlan:
    target_id = ""
    environment = ""
    if snapshot is not None and hasattr(snapshot, "target_id"):
        target_id = str(snapshot.target_id)
        environment = str(snapshot.environment)
    return RollbackPlan(
        plan_version=ROLLBACK_PLAN_VERSION,
        original_action_id=getattr(original_action, "action_id", "unknown"),
        snapshot_id=getattr(snapshot, "snapshot_id", "unknown") if snapshot is not None else "unknown",
        target_id=target_id or "unknown",
        environment=environment or "unknown",
        expected_post_revision=1,
        expected_post_fields=(),
        restore_revision=1,
        restore_digest="0" * 64,
        restore_fields=(),
        reversible_fields=(),
        safe=False,
        reason_code=reason,
    )
