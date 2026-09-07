"""Composition-root adapter: canonical binding → engine execution contract.

This module is the smallest safe seam connecting the composed operator
transport's canonical execution flow to the existing final-execution
architecture. It converts the control-plane-owned
:class:`~aipm.control_plane.models.UpdateExecutionBinding` — derived from
trusted durable state after canonical gated execution reached a verified
outcome — into the engine-side
:class:`~aipm.services.update.execution_contract.ExecutionContract` and
drives the real update engine through its established contract path.

This is composition, not new authority: no new approval mechanism, store,
digest space, schema, HTTP route, or execution path is created. The
adapter lives OUTSIDE ``aipm.control_plane`` because the control plane is
boundary-scanned (no engine vocabulary, no engine imports), and the engine
side stays authorization-free (the engine performs an integrity/binding
check only). Authorization semantics stay entirely in the control plane;
the engine-side contract check is integrity/binding only.
"""
from __future__ import annotations

from aipm.services.update.execution_contract import ExecutionContract

__all__ = ["compose_update_runtime"]


def compose_update_runtime(engine):
    """Return a runtime port that drives the real update engine.

    The returned callable satisfies the service's ``update_runtime`` port
    contract: it accepts one
    :class:`~aipm.control_plane.models.UpdateExecutionBinding` (trusted
    durable state, never client input) and returns a bounded result dict
    (no sensitive values, no engine types, no callables). It adapts the
    binding to the engine-side execution contract; the conversion lives at
    the composition root because the control plane is boundary-scanned.
    """

    def runtime(binding) -> dict:
        contract = ExecutionContract(
            project_name=binding.project_name,
            plan_digest=binding.plan_digest,
            confirmation_id=binding.confirmation_id,
        )
        audit = engine.execute_update(
            binding.project_name,
            execution_contract=contract,
            approve=True,
        )
        return {
            "project_name": audit.project,
            "outcome": audit.outcome,
            "mode": audit.mode,
            "risk": audit.risk,
            "audit_path": str(audit.audit_path) if audit.audit_path else "",
        }

    return runtime
