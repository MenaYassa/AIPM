"""Bridge between the legacy update plane and the canonical control plane.

Direction is one-way and permanent::

    legacy update intent
      → typed ActionRequest adapter   (this package)
      → ControlPlaneService           (the authority)
      → bounded executor / dry-run sink

The adapter allow-lists operation, target, environment, and fields; it never
invents identity (the service derives the canonical ActionIdentity), never
authorizes, and never executes. Legacy actor strings, booleans, and approval
flags are deliberately NOT accepted as inputs — all authority comes from the
control plane's own owner/principal/policy machinery.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from aipm.control_plane.audit.sanitize import AuditEventError, bounded_reference
from aipm.control_plane.models import ActionRequest, OperationKind
from aipm.control_plane.rollback import REVERSIBLE_OPERATIONS

BRIDGE_VERSION = "mc612-update-bridge-v1"


class BridgeError(ValueError):
    """Raised when a legacy intent cannot be represented safely."""


@dataclass(frozen=True, slots=True)
class LegacyUpdateIntent:
    """Bounded representation of legacy update intent.

    Deliberately minimal: a project name (which must map to a registered
    control-plane plan), a bounded idempotency key, and the environment. The
    adapter rejects everything else — legacy actor strings, approval
    booleans, and provider-selected targets never enter the control plane.
    """

    project: str
    idempotency_key: str
    environment: str = "staging"
    bridge_version: str = BRIDGE_VERSION

    def __post_init__(self) -> None:
        if self.bridge_version != BRIDGE_VERSION:
            raise BridgeError("Unsupported bridge version")
        object.__setattr__(self, "project", bounded_reference(self.project, field="project"))
        object.__setattr__(self, "idempotency_key", bounded_reference(self.idempotency_key, field="idempotency key"))
        if self.environment not in ("staging", "production"):
            raise BridgeError("Intent environment must be a known value")


@dataclass(frozen=True, slots=True)
class DryRunMutationRecord:
    """Bounded record of what a real executor WOULD have mutated.

    Evidence-only: never persisted as control-plane state, never a claim
    that a mutation occurred.
    """

    action_id: str
    target_id: str
    environment: str
    plan_id: str
    pre_mutation_revision: int
    pre_mutation_digest: str
    mutation_fields: tuple[tuple[str, str], ...]
    lease_id: str
    fencing_token: int
    operation: str
    recorded_at: str

    def safe_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "target_id": self.target_id,
            "environment": self.environment,
            "plan_id": self.plan_id,
            "pre_mutation_revision": self.pre_mutation_revision,
            "pre_mutation_digest": self.pre_mutation_digest,
            "mutation_fields": [list(item) for item in self.mutation_fields],
            "lease_id": self.lease_id,
            "fencing_token": self.fencing_token,
            "operation": self.operation,
            "recorded_at": self.recorded_at,
            "dry_run": True,
        }


class PlanMutationStore(Protocol):
    """Narrow view the bridge needs of a plan store."""

    def read(self, target_id: str) -> Any: ...


class UpdateActionRequestAdapter:
    """Deterministic LegacyUpdateIntent → canonical ActionRequest adapter.

    Allow-lists everything: the project must map to a registered staging
    control-plane plan, the operation is the bounded plan update, and the
    mutation fields are exactly the plan's current mutable values (the
    legacy plane supplies intent, not content). Never authorizes, never
    executes, never computes identity.
    """

    def __init__(self, *, plan_store: PlanMutationStore, allowed_projects: frozenset[str] | set[str]) -> None:
        plans = frozenset(allowed_projects)
        if not plans:
            raise BridgeError("An explicit project allow-list is required")
        self._plans = plans
        self._plan_store = plan_store

    def adapt(self, intent: LegacyUpdateIntent) -> ActionRequest:
        if not isinstance(intent, LegacyUpdateIntent):
            raise BridgeError("Invalid legacy update intent")
        if intent.project not in self._plans:
            raise BridgeError("Project is not allow-listed for the bridge")
        if intent.environment != "staging":
            raise BridgeError("Only staging is bridgeable")
        try:
            plan = self._plan_store.read(intent.project)
        except Exception as exc:
            raise BridgeError("Target plan is not registered with the control plane") from exc
        if plan.environment.value != "staging":
            raise BridgeError("Only staging plans are bridgeable")
        if not plan.enabled:
            raise BridgeError("Disabled plans are not bridgeable")
        fields = tuple(sorted((name, getattr(plan, name)) for name in ("title", "objective")))
        if not fields:
            raise BridgeError("Plan exposes no mutable fields")
        return ActionRequest(
            operation=OperationKind.UPDATE_PROJECT_PLAN,
            target_id=intent.project,
            idempotency_key=intent.idempotency_key,
            metadata=fields,
            environment=intent.environment,
        )


class DryRunMutationSink:
    """Records what a real executor WOULD have done; performs nothing.

    Implements the mutation boundary as a recording sink: the canonical
    flow runs fully (authorize → confirm → snapshot → lease → contract) but
    the plan is never mutated and no external boundary is touched.
    """

    def __init__(self) -> None:
        self.records: list[DryRunMutationRecord] = []

    def record(self, *, action_id: str, target_id: str, environment: str, plan_id: str, pre_mutation_revision: int, pre_mutation_digest: str, mutation_fields: Mapping[str, str], lease_id: str, fencing_token: int, operation: str) -> DryRunMutationRecord:
        for name, value in mutation_fields.items():
            bounded_reference(str(value), field=f"field {name}")
        record = DryRunMutationRecord(
            action_id=action_id,
            target_id=target_id,
            environment=environment,
            plan_id=plan_id,
            pre_mutation_revision=pre_mutation_revision,
            pre_mutation_digest=pre_mutation_digest,
            mutation_fields=tuple(sorted(mutation_fields.items())),
            lease_id=lease_id,
            fencing_token=fencing_token,
            operation=operation,
            recorded_at=str(_utc_now_iso()),
        )
        self.records.append(record)
        return record


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
