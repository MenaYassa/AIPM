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
    CAPABILITY_EXECUTE_SERVICE_UPDATE,
    CAPABILITY_EXECUTE_UPDATE_PLAN,
    PROTOCOL_VERSION,
    ExecutionRequest,
    ExecutionResponse,
    ReceiptQueryRequest,
    ReceiptQueryResponse,
)
from aipm.control_plane.mutation_receipt import (
    MutationReceiptError,
    MutationStatus,
    RECEIPT_EVIDENCE_NOT_FOUND,
    RECEIPT_EVIDENCE_UNAVAILABLE,
)
from aipm.core.exceptions import UpdateError
from aipm.services.compose.execution_adapter import (
    BoundedServiceUpdateIntent,
    ComposeExecutionAdapter,
    ComposeExecutionError,
    ServiceUpdateVerificationCode,
)
from aipm.services.update.engine import UpdateEngine
from aipm.services.update.execution_contract import ExecutionContract

__all__ = [
    "compose_executor_update_handler",
    "compose_ipc_update_runtime",
    "compose_receipt_query_handler",
]


def compose_ipc_update_runtime(client):
    """Return the ``update_runtime`` port backed by the executor IPC channel.

    The returned callable accepts one trusted durable
    :class:`~aipm.control_plane.models.UpdateExecutionBinding` (derived by
    the service layer after VERIFIED_SUCCESS; never client input) and sends
    one bounded execution request carrying the binding's durable evidence.
    """

    def runtime(binding) -> dict:
        service_scope = getattr(binding, "service_scope", None)
        capability_id = (
            CAPABILITY_EXECUTE_SERVICE_UPDATE
            if service_scope is not None
            else CAPABILITY_EXECUTE_UPDATE_PLAN
        )
        request = ExecutionRequest(
            action_id=binding.action_id,
            capability_id=capability_id,
            target_id=binding.project_name,
            contract_digest=binding.contract_digest,
            lease_id=binding.lease_id,
            fencing_token=binding.fencing_token,
            action_protocol=binding.action_protocol,
            plan_digest=binding.plan_digest,
            confirmation_id=binding.confirmation_id,
            service_scope=service_scope,
            registration_id=getattr(binding, "registration_id", None),
            registration_digest=getattr(binding, "registration_digest", None),
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
    engine: UpdateEngine | None = None,
    compose_adapter: ComposeExecutionAdapter | None = None,
    receipts,
    audit_dir: str | None = None,
    now=None,
):
    """Return the executor-side IPC handler for update capabilities."""
    if engine is None and compose_adapter is None:
        raise TypeError("Either engine or compose_adapter must be provided")
    if engine is not None and not isinstance(engine, UpdateEngine):
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
        # 1. Structural capability and binding validation
        if request.capability_id not in (CAPABILITY_EXECUTE_UPDATE_PLAN, CAPABILITY_EXECUTE_SERVICE_UPDATE):
            return ExecutionResponse(outcome="refused", provider_code="unsupported_capability", action_id=request.action_id, evidence_reference="")

        is_service_update = (
            request.capability_id == CAPABILITY_EXECUTE_SERVICE_UPDATE
            or request.service_scope is not None
        )

        plan_digest = request.plan_digest or ""
        confirmation_id = request.confirmation_id or ""
        if len(plan_digest) != 64 or not set(plan_digest) <= _hex64:
            return ExecutionResponse(outcome="refused", provider_code="invalid_plan_digest", action_id=request.action_id, evidence_reference="")
        if len(confirmation_id) != 32 or not set(confirmation_id) <= _hex32:
            return ExecutionResponse(outcome="refused", provider_code="invalid_confirmation_id", action_id=request.action_id, evidence_reference="")

        if is_service_update:
            if request.service_scope is None or not request.service_scope:
                return ExecutionResponse(outcome="refused", provider_code="missing_service_scope", action_id=request.action_id, evidence_reference="")
            if compose_adapter is None:
                return ExecutionResponse(outcome="refused", provider_code="compose_adapter_unavailable", action_id=request.action_id, evidence_reference="")
        else:
            if engine is None:
                return ExecutionResponse(outcome="refused", provider_code="engine_unavailable", action_id=request.action_id, evidence_reference="")

        # 2. Exactly-once durable receipt claim.
        try:
            receipts.claim(
                action_id=request.action_id,
                fencing_token=request.fencing_token,
                capability_id=request.capability_id,
                target_id=request.target_id,
                contract_digest=request.contract_digest,
            )
        except MutationReceiptError:
            existing = None
            try:
                existing = receipts.get(action_id=request.action_id, fencing_token=request.fencing_token)
            except Exception:  # noqa: BLE001 - classification only
                pass
            status = existing.mutation_status.value if existing is not None else "unknown"
            return ExecutionResponse(outcome="refused", provider_code=f"already_claimed:{status}", action_id=request.action_id, evidence_reference="")
        except Exception:  # noqa: BLE001 - receipt store failure: fail closed, no engine call
            return ExecutionResponse(outcome="refused", provider_code="receipt_store_unavailable", action_id=request.action_id, evidence_reference="")

        # 3. Execution dispatch
        if is_service_update:
            intent = BoundedServiceUpdateIntent(
                project_name=request.target_id,
                service_scope=request.service_scope,
                plan_digest=plan_digest,
                confirmation_id=confirmation_id,
                action_id=request.action_id,
                fencing_token=request.fencing_token,
                contract_digest=request.contract_digest,
                lease_id=request.lease_id,
                now=now() if callable(now) else now,
            )
            try:
                result = compose_adapter.execute(intent)
            except ComposeExecutionError as exc:
                try:
                    receipts.complete(
                        action_id=request.action_id,
                        fencing_token=request.fencing_token,
                        status=MutationStatus.MUTATION_FAILED,
                        provider_code=f"executor_error:{exc.code}",
                    )
                except Exception:
                    pass
                return ExecutionResponse(outcome="failed", provider_code=exc.code, action_id=request.action_id, evidence_reference=str(exc)[:128])
            except Exception as exc:
                try:
                    receipts.complete(
                        action_id=request.action_id,
                        fencing_token=request.fencing_token,
                        status=MutationStatus.UNKNOWN_OUTCOME,
                        provider_code="update_interrupted",
                    )
                except Exception:
                    pass
                return ExecutionResponse(outcome="unknown_outcome", provider_code="update_interrupted", action_id=request.action_id, evidence_reference=str(exc)[:128])

            if not result.is_success:
                code_val = result.verification.code.value
                status = (
                    MutationStatus.UNKNOWN_OUTCOME
                    if result.verification.code == ServiceUpdateVerificationCode.RECONCILIATION_REQUIRED
                    else MutationStatus.MUTATION_FAILED
                )
                outcome_str = "unknown_outcome" if status == MutationStatus.UNKNOWN_OUTCOME else "failed"
                try:
                    receipts.complete(
                        action_id=request.action_id,
                        fencing_token=request.fencing_token,
                        status=status,
                        provider_code=f"verification_failure:{code_val}",
                    )
                except Exception:
                    pass
                return ExecutionResponse(
                    outcome=outcome_str,
                    provider_code=f"verification_failure:{code_val}",
                    action_id=request.action_id,
                    evidence_reference=result.verification.details[:128],
                )

            try:
                receipts.complete(
                    action_id=request.action_id,
                    fencing_token=request.fencing_token,
                    status=MutationStatus.MUTATION_SUCCEEDED,
                    provider_code="update_ok",
                )
            except Exception:
                pass
            return ExecutionResponse(
                outcome="succeeded",
                provider_code="update_ok",
                action_id=request.action_id,
                evidence_reference=f"verified-scope:{','.join(result.verification.verified_services)}",
            )

        # 4. Engine execution under the binding contract.
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
            try:
                receipts.complete(
                    action_id=request.action_id,
                    fencing_token=request.fencing_token,
                    status=MutationStatus.MUTATION_FAILED,
                    provider_code="update_failed",
                )
            except Exception:
                pass
            return ExecutionResponse(outcome="failed", provider_code="update_failed", action_id=request.action_id, evidence_reference=str(exc)[:128])
        except Exception:
            try:
                receipts.complete(
                    action_id=request.action_id,
                    fencing_token=request.fencing_token,
                    status=MutationStatus.UNKNOWN_OUTCOME,
                    provider_code="update_interrupted",
                )
            except Exception:
                pass
            return ExecutionResponse(outcome="unknown_outcome", provider_code="update_interrupted", action_id=request.action_id, evidence_reference="")

        outcome_value = str(getattr(audit, "outcome", "")).lower()
        if outcome_value != "success":
            try:
                receipts.complete(
                    action_id=request.action_id,
                    fencing_token=request.fencing_token,
                    status=MutationStatus.MUTATION_FAILED,
                    provider_code=f"engine_outcome:{outcome_value or 'unknown'}",
                )
            except Exception:
                pass
            return ExecutionResponse(outcome="failed", provider_code=f"engine_outcome:{outcome_value or 'unknown'}", action_id=request.action_id, evidence_reference="")
        try:
            receipts.complete(
                action_id=request.action_id,
                fencing_token=request.fencing_token,
                status=MutationStatus.MUTATION_SUCCEEDED,
                provider_code="update_ok",
            )
        except Exception:
            pass
        audit_path = str(getattr(audit, "audit_path", "") or "")
        return ExecutionResponse(outcome="succeeded", provider_code="update_ok", action_id=request.action_id, evidence_reference=f"update-audit:{audit_path}")

    return handler


def compose_receipt_query_handler(*, receipts):
    """Return the executor-side read-only receipt evidence handler.

    The handler is SELECT-only observation: it never claims, completes, or
    mutates a receipt, never touches the engine, confirmations, leases, or
    any other store, and never raises past the IPC accept loop. For one
    structurally valid :class:`ReceiptQueryRequest` it returns the receipt's
    ``safe_dict()`` evidence (bounded scalar map) plus the protocol version
    and an empty ``evidence_reference``; a missing receipt yields
    ``not_found``; any store failure yields ``evidence_unavailable``.
    """

    if receipts is None or not hasattr(receipts, "get"):
        raise TypeError("receipts must provide the MutationReceiptStore contract")

    def handler(request: ReceiptQueryRequest) -> ReceiptQueryResponse:
        try:
            receipt = receipts.get(action_id=request.action_id, fencing_token=request.fencing_token)
        except Exception:  # noqa: BLE001 - fail closed as unavailable, never raise
            return ReceiptQueryResponse(
                mutation_status=RECEIPT_EVIDENCE_UNAVAILABLE,
                provider_code="receipt_store_failure",
                evidence={},
            )
        if receipt is None:
            return ReceiptQueryResponse(
                mutation_status=RECEIPT_EVIDENCE_NOT_FOUND,
                provider_code="",
                evidence={},
            )
        return ReceiptQueryResponse(
            mutation_status=str(receipt.safe_dict()["mutation_status"]),
            provider_code=str(receipt.safe_dict().get("provider_code", "")),
            evidence={
                **receipt.safe_dict(),
                "protocol_version": PROTOCOL_VERSION,
                "evidence_reference": "",
            },
        )

    return handler
