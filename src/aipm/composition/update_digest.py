"""Composition-root adapter: the authoritative update-plan digest port.

The approval boundary verifies a presented update-plan digest against the
digest of the plan the engine will execute. The canonical identity is:

    UpdatePlanIdentity.from_plan(
        engine.plan_update(target, dry_run=False)
    ).digest()

This module binds that identity to a live update engine WITHOUT placing
engine vocabulary inside ``aipm.control_plane`` (the control plane is
boundary-scanned: no engine type names, no ``aipm.services`` imports —
C4 tests 40/41). It is a composition-root port: the caller injects the
same engine instance that the update runtime drives, so approval
verification, the durable binding, and the engine's recomputed identity
all speak one digest space.

This defines no parallel digest implementation: the derivation is the
canonical :class:`~aipm.services.update.plan_identity.UpdatePlanIdentity`
only. The durable ProjectPlan record keeps its own authoritative identity
(a separate, legitimate digest space); it is never used for update-plan
approval here.
"""
from __future__ import annotations

from typing import Callable

from aipm.control_plane.models import ControlPlaneError, PlanningErrorCode
from aipm.services.update.plan_identity import UpdatePlanIdentity

__all__ = ["update_plan_digest_port"]


def update_plan_digest_port(engine) -> Callable[[str], str]:
    """Bind the ``current_plan_digest`` port to the update engine's plan.

    Read-only adapter: re-plans the target through the engine's canonical
    planner in execution mode (``dry_run=False`` — planning is read-only in
    both modes; the flag only marks the plan) and returns the canonical
    ``UpdatePlanIdentity`` digest. Any planning failure raises a typed
    control-plane error; the approval boundary maps it to canonical
    fail-closed codes (no fabricated digest, no default).

    The injected object must expose the engine planning surface
    (``plan_update(project_name, *, dry_run)``); the composition root
    passes the same engine instance it hands to the update runtime, so
    both ports share one planning semantics.
    """

    def _read_digest(target_id: str) -> str:
        try:
            plan = engine.plan_update(target_id, dry_run=False)
        except ControlPlaneError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed on any planning failure
            raise ControlPlaneError(
                PlanningErrorCode.UNAVAILABLE_EVIDENCE, "Authoritative update plan is unavailable"
            ) from exc
        try:
            digest = UpdatePlanIdentity.from_plan(plan).digest()
        except Exception as exc:  # noqa: BLE001 - fail closed on any identity failure
            raise ControlPlaneError(
                PlanningErrorCode.UNAVAILABLE_EVIDENCE, "Authoritative update plan digest is unavailable"
            ) from exc
        if not isinstance(digest, str) or len(digest) != 64:
            raise ControlPlaneError(
                PlanningErrorCode.UNAVAILABLE_EVIDENCE, "Authoritative update plan digest is malformed"
            )
        return digest

    return _read_digest
