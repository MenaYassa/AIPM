"""Composition-root adapters for the C6.4 executor update capability.

Two seams, one per trust side, composed here (outside
``aipm.control_plane``, which is boundary-scanned against engine
vocabulary, and outside the executor process itself):

* ``compose_ipc_update_runtime`` binds the control plane's trusted
  :class:`~aipm.control_plane.models.UpdateExecutionBinding` to the
  executor over the existing Unix-domain IPC channel. The control plane
  keeps the canonical gated execution authority: the runtime port is
  invoked ONLY after canonical gated execution reached VERIFIED_SUCCESS
  (confirmed → consumed, snapshot → lease → gate → bounded plan mutation
  → independent read-back verification), so the engine execution is
  downstream of every authorization boundary, not a parallel one.
* ``compose_executor_update_handler`` binds the executor process's IPC
  handler to the real update engine. The executor performs structural
  validation and exactly-once receipt keying only — never business
  authorization.

No new approval mechanism, store, digest space, schema, HTTP route, or
parallel execution authority is created. The wire contract adds only the
two engine binding fields (``plan_digest``, ``confirmation_id``),
required for ``execute_update_plan`` and absent-or-ignored for the
legacy systemd-restart capability.
"""
from __future__ import annotations

from aipm.control_plane.executor_ipc import (
    CAPABILITY_EXECUTE_UPDATE_PLAN,
    ExecutionRequest,
    ExecutionResponse,
)
from aipm.control_plane.mutation_receipt import MutationReceiptError, MutationStatus
from aipm.core.exceptions import UpdateError
from aipm.services.update.engine import UpdateEngine
from aipm.services.update.execution_contract import ExecutionContract

__all__ = [
    "compose_executor_update_handler",
    "compose_ipc_update_runtime",
]


def compose_ipc_update_runtime(client):
    """Return the ``update_runtime`` port backed by the executor IPC channel.

    The returned callable accepts one trusted durable
    :class:`~aipm.control_plane.models.UpdateExecutionBinding` (derived by
    the service layer after VERIFIED_SUCCESS; never client input) and sends
    one bounded ``execute_update_plan`` request carrying the binding's
    durable evidence. The response outcome is returned as a bounded dict;
    ``unknown_outcome`` is preserved as UNKNOWN — never mapped to success
    or failure — and any exception is swallowed into UNKNOWN with a
    transport code: the request may have crossed the executor's receipt
    claim boundary, so this process cannot know the outcome. The control
    plane's action state stays terminal (VERIFIED_SUCCESS); ambiguity is
    resolved by an operator against the executor's receipt database and
    the engine audit files.
    """

    def runtime(binding) -> dict:
        request = ExecutionRequest(
            action_id=binding.action_id,
            capability_id=CAPABILITY_EXECUTE_UPDATE_PLAN,
            target_id=binding.project_name,
            contract_digest=binding.contract_digest,
            lease_id=binding.lease_id,
            fencing_token=binding.fencing_token,
            plan_digest=binding.plan_digest,
            confirmation_id=binding.confirmation_id,
        )
        try:
            response = client.send(request)
        except Exception:  # noqa: BLE001 - never propagate past the terminal boundary
            return {
                "outcome": "unknown_outcome",
                "provider_code": "transport_failure",
                "action_id": binding.action_id,
                "evidence_reference": "",
            }
        return {
            "outcome": response.outcome,
            "provider_code": response.provider_code,
            "action_id": response.action_id,
            "evidence_reference": response.evidence_reference,
        }

    return runtime


def compose_executor_update_handler(
    *,
    engine: UpdateEngine,
    receipts,
    audit_dir: str | None = None,
    now=None,
):
    """Return the executor-side IPC handler for ``execute_update_plan``.

    The handler is the ONLY engine send point on the executor side. For one
    structurally valid request it must: claim the durable mutation receipt
    (exactly-once per ``(action_id, fencing_token)``), build the engine-side
    execution contract from the wire binding material, drive the real update
    engine, classify the outcome into the bounded set, and complete the
    receipt. Every failure is classified — the handler never raises past
    the IPC accept loop (an uncaught exception would crash the single
    accept loop) and never reports a pre-provider failure as success.

    Classification contract (bounded):
    - ``succeeded``        — the engine returned a verified success audit.
    - ``failed``           — the engine raised before the mutation boundary
                             or reported a definitive failure (rollback
                             completed on the engine side; receipt records
                             the bounded reason).
    - ``unknown_outcome``  — the engine raised after the mutation could have
                             started (mid-flight crash analogue): the
                             receipt stays/becomes UNKNOWN, never retried.
    - ``refused``          — structural refusal (duplicate claim, missing
                             engine, malformed binding); no engine call.
    """

    if engine is None or not isinstance(engine, UpdateEngine):
        raise TypeError("engine must be the canonical UpdateEngine")
    if receipts is None or not hasattr(receipts, "claim") or not hasattr(receipts, "complete"):
        raise TypeError("receipts must provide the MutationReceiptStore contract")

    _hex64 = set("0123456789abcdef")
    _hex32 = set("0123456789abcdef")

    def _fail(action_id: str, fencing_token: int, code: str) -> ExecutionResponse:
        try:
            receipts.complete(
                action_id=action_id,
                fencing_token=fencing_token,
                status=MutationStatus.MUTATION_FAILED,
                provider_code=f"executor_error:{code}",
            )
        except Exception:  # noqa: BLE001 - receipt completion is best-effort
            pass
        return ExecutionResponse(outcome="failed", provider_code=code, action_id=action_id, evidence_reference="")

    def handler(request: ExecutionRequest) -> ExecutionResponse:
        # 1. Structural binding validation (NOT business authorization).
        if request.capability_id != CAPABILITY_EXECUTE_UPDATE_PLAN:
            return ExecutionResponse(outcome="refused", provider_code="unsupported_capability", action_id=request.action_id, evidence_reference="")
        plan_digest = request.plan_digest or ""
        confirmation_id = request.confirmation_id or ""
        if len(plan_digest) != 64 or not set(plan_digest) <= _hex64:
            return ExecutionResponse(outcome="refused", provider_code="invalid_plan_digest", action_id=request.action_id, evidence_reference="")
        if len(confirmation_id) != 32 or not set(confirmation_id) <= _hex32:
            return ExecutionResponse(outcome="refused", provider_code="invalid_confirmation_id", action_id=request.action_id, evidence_reference="")

        # 2. Exactly-once durable receipt claim. A duplicate claim means the
        #    mutation was already attempted for this (action, fence): refuse
        #    without touching the engine (no retry, no second send).
        try:
            receipts.claim(
                action_id=request.action_id,
                fencing_token=request.fencing_token,
                capability_id=request.capability_id,
                target_id=request.target_id,
                contract_digest=request.contract_digest,
            )
        except MutationReceiptError as exc:
            existing = None
            try:
                existing = receipts.get(action_id=request.action_id, fencing_token=request.fencing_token)
            except Exception:  # noqa: BLE001 - classification only
                pass
            status = existing.mutation_status.value if existing is not None else "unknown"
            return ExecutionResponse(outcome="refused", provider_code=f"already_claimed:{status}", action_id=request.action_id, evidence_reference="")
        except Exception:  # noqa: BLE001 - receipt store failure: fail closed, no engine call
            return ExecutionResponse(outcome="refused", provider_code="receipt_store_unavailable", action_id=request.action_id, evidence_reference="")

        # 3. Engine execution under the binding contract. The engine
        #    re-plans the project, recomputes the canonical UpdatePlanIdentity
        #    digest, and fails closed on any mismatch BEFORE mutating.
        contract = ExecutionContract(
            project_name=request.target_id,
            plan_digest=plan_digest,
            confirmation_id=confirmation_id,
        )
        try:
            audit = engine.execute_update(
                request.target_id,
                approve=True,
                execution_contract=contract,
            )
        except UpdateError as exc:
            # Definitive pre-mutation or rolled-back failure (the engine
            # restores and audits internally before raising).
            try:
                receipts.complete(
                    action_id=request.action_id,
                    fencing_token=request.fencing_token,
                    status=MutationStatus.MUTATION_FAILED,
                    provider_code="update_failed",
                )
            except Exception:  # noqa: BLE001
                pass
            return ExecutionResponse(outcome="failed", provider_code="update_failed", action_id=request.action_id, evidence_reference=str(exc)[:128])
        except Exception:  # noqa: BLE001 - ambiguity: the mutation may have started
            try:
                receipts.complete(
                    action_id=request.action_id,
                    fencing_token=request.fencing_token,
                    status=MutationStatus.UNKNOWN_OUTCOME,
                    provider_code="update_interrupted",
                )
            except Exception:  # noqa: BLE001
                pass
            return ExecutionResponse(outcome="unknown_outcome", provider_code="update_interrupted", action_id=request.action_id, evidence_reference="")

        # 4. Bounded outcome classification from the engine audit.
        outcome_value = str(getattr(audit, "outcome", "")).lower()
        if outcome_value != "success":
            try:
                receipts.complete(
                    action_id=request.action_id,
                    fencing_token=request.fencing_token,
                    status=MutationStatus.MUTATION_FAILED,
                    provider_code=f"engine_outcome:{outcome_value or 'unknown'}",
                )
            except Exception:  # noqa: BLE001
                pass
            return ExecutionResponse(outcome="failed", provider_code=f"engine_outcome:{outcome_value or 'unknown'}", action_id=request.action_id, evidence_reference="")
        try:
            receipts.complete(
                action_id=request.action_id,
                fencing_token=request.fencing_token,
                status=MutationStatus.MUTATION_SUCCEEDED,
                provider_code="update_ok",
            )
        except Exception:  # noqa: BLE001 - receipt completion failure must not
            pass           # misreport a verified success as failed
        audit_path = str(getattr(audit, "audit_path", "") or "")
        return ExecutionResponse(outcome="succeeded", provider_code="update_ok", action_id=request.action_id, evidence_reference=f"update-audit:{audit_path}")

    return handler
