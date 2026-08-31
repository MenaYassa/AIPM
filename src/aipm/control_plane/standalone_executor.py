"""Standalone executor for external mutations.

This executor does NOT require access to the control-plane database.
It receives an immutable ExecutionEnvelope from the control plane via
Unix-domain IPC. The envelope carries all information necessary to
perform the exact authorized mutation: action identity, capability,
target binding, contract digest, fencing token, and expiry.

Trust model:
- The control plane is the authorization authority (it built the
  envelope after FinalExecutionGate approval).
- The executor validates the envelope structurally (schema, bounded
  fields, expiry) and performs structural replay prevention via the
  mutation receipt store.
- The executor does NOT perform business authorization.
- The executor does NOT read the control-plane database.

The executor's own mutation receipt store is its only durable state.

Guarantee hierarchy (precise terms):
- Exactly-once durable claim: for a given (action_id, fencing_token),
  at most one receipt can ever exist (UNIQUE constraint + BEGIN IMMEDIATE
  claim transaction). Proven under concurrency.
- Single provider invocation under concurrent duplicate requests: the
  claim gates the provider, so concurrent duplicate envelopes yield at
  most one provider call; every loser raises MutationReceiptError.
- NOT exactly-once external side effect: if the process dies between
  claim and the provider's own durability boundary, the external effect
  may or may not have occurred. RECEIPT_CREATED then means
  attempted/outcome-unknown; it is never auto-retried, and pre-provider
  executor failures are recorded as MUTATION_FAILED
  (provider_code="executor_error:<stage>") so a lingering RECEIPT_CREATED
  can only be a true mid-flight crash window.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from aipm.control_plane.audit.sanitize import AuditEventError, bounded_reference
from aipm.control_plane.mutation_receipt import MutationReceiptStore, MutationStatus
from aipm.control_plane.systemd_provider import (
    SystemdRestartError,
    SystemdRestartPolicy,
    SystemdRestartProvider,
    SystemdRestartResult,
    SystemdUnitSnapshot,
)

ENVELOPE_VERSION = "mc612-execution-envelope-v1"
EXECUTOR_VERSION = "mc612-standalone-executor-v1"


class EnvelopeError(ValueError):
    """Raised when an execution envelope fails structural validation."""


@dataclass(frozen=True, slots=True)
class ExecutionEnvelope:
    """Immutable execution envelope carrying all executor-needed information.

    Built by the control plane after FinalExecutionGate approval. The
    executor trusts this envelope because:
    1. The caller is authenticated via SO_PEERCRED (only the control-plane UID).
    2. The envelope was created by the control plane after the gate passed.
    3. The contract digest binds it to the authorized action.
    4. The mutation receipt prevents replay.

    The executor does NOT need access to the control-plane database.
    """

    protocol_version: str
    action_id: str
    action_version: int
    capability_id: str
    capability_version: str
    target_id: str
    environment: str
    unit_name: str
    contract_digest: str
    fencing_token: int
    lease_id: str
    issued_at: str
    expires_at: str

    def __post_init__(self) -> None:
        if self.protocol_version != ENVELOPE_VERSION:
            raise EnvelopeError(f"Unsupported envelope version: {self.protocol_version}")
        for name, value in (
            ("action_id", self.action_id),
            ("capability_id", self.capability_id),
            ("target_id", self.target_id),
            ("contract_digest", self.contract_digest),
            ("lease_id", self.lease_id),
        ):
            object.__setattr__(self, name, bounded_reference(value, field=name))
        object.__setattr__(self, "unit_name", bounded_reference(self.unit_name, field="unit name"))
        if not self.unit_name.endswith(".service"):
            raise EnvelopeError("Unit name must end in .service")
        if "/" in self.unit_name or "\\" in self.unit_name:
            raise EnvelopeError("Unit name must not contain path separators")
        if not isinstance(self.action_version, int) or self.action_version < 1:
            raise EnvelopeError("Invalid action version")
        if not isinstance(self.fencing_token, int) or self.fencing_token < 1:
            raise EnvelopeError("Invalid fencing token")
        # Validate timestamps
        for ts_field in ("issued_at", "expires_at"):
            ts = getattr(self, ts_field)
            if not isinstance(ts, str):
                raise EnvelopeError(f"Invalid {ts_field}")
            try:
                datetime.fromisoformat(ts)
            except ValueError as exc:
                raise EnvelopeError(f"Invalid {ts_field}") from exc

    def is_expired(self, now: datetime) -> bool:
        expires = datetime.fromisoformat(self.expires_at)
        return now >= expires

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "action_id": self.action_id,
            "action_version": self.action_version,
            "capability_id": self.capability_id,
            "capability_version": self.capability_version,
            "target_id": self.target_id,
            "environment": self.environment,
            "unit_name": self.unit_name,
            "contract_digest": self.contract_digest,
            "fencing_token": self.fencing_token,
            "lease_id": self.lease_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def digest(self) -> str:
        payload = self.canonical_payload()
        canonical = __import__("json").dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return __import__("hashlib").sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StandaloneRestartResult:
    """Bounded result from the standalone executor."""

    action_id: str
    outcome: str
    provider_code: str
    evidence_reference: str
    executor_version: str = EXECUTOR_VERSION


class StandaloneSystemdExecutor:
    """Executes a systemd restart from an execution envelope without CP DB access.

    The executor:
    1. Validates the envelope structurally
    2. Claims a mutation receipt (prevents replay)
    3. Observes the unit pre-mutation
    4. Invokes the provider (systemctl restart)
    5. Observes the unit post-mutation (independent verification)
    6. Classifies the outcome
    7. Returns a bounded result
    """

    __slots__ = ("_provider", "_policy", "_receipts", "_runner", "_initialized")

    def __init__(
        self,
        *,
        provider: SystemdRestartProvider,
        policy: SystemdRestartPolicy,
        receipts: MutationReceiptStore,
        runner: Callable | None = None,
    ) -> None:
        if not isinstance(provider, SystemdRestartProvider):
            raise TypeError("provider must be SystemdRestartProvider")
        if not isinstance(policy, SystemdRestartPolicy):
            raise TypeError("policy must be SystemdRestartPolicy")
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_policy", policy)
        object.__setattr__(self, "_receipts", receipts)
        object.__setattr__(self, "_runner", runner)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("StandaloneSystemdExecutor configuration is immutable")
        object.__setattr__(self, name, value)

    def execute_restart(self, envelope: ExecutionEnvelope, *, now: datetime | None = None) -> StandaloneRestartResult:
        """Execute one bounded systemd restart from the execution envelope.

        No control-plane DB access. The envelope carries all necessary info.
        The mutation receipt prevents duplicate external mutation.
        """
        moment = now or datetime.now(timezone.utc)

        # 1. Structural envelope validation
        self._validate_envelope(envelope, now=moment)

        # 2. Claim the mutation receipt (replay prevention; raises if already claimed)
        receipt = self._receipts.claim(
            action_id=envelope.action_id,
            fencing_token=envelope.fencing_token,
            capability_id=envelope.capability_id,
            target_id=envelope.target_id,
            contract_digest=envelope.contract_digest,
        )

        # Steps 3-5 (resolve/observe/restart) are wrapped so that a failure
        # BEFORE the provider invocation is durably recorded as
        # MUTATION_FAILED (provider_code="executor_error:<stage>") instead
        # of leaving a perpetual RECEIPT_CREATED. RECEIPT_CREATED after this
        # point therefore means only one thing: the provider was invoked and
        # the process died mid-flight (crash window), i.e. outcome UNKNOWN.
        try:
            # 3. Resolve the unit from the allow-list
            policy = self._provider.resolve_unit(
                self._policy.unit_id,
                environment=envelope.environment,
            )

            # 4. Pre-mutation observation
            pre_snapshot = self._provider.observe_unit(policy, now=moment)

            # 5. Invoke the provider
            provider_result = self._provider.restart(policy, now=moment)
        except SystemdRestartError as exc:
            self._fail_receipt(envelope, stage="pre_provider")
            raise

        # 6. Classify the outcome
        if provider_result.timed_out:
            outcome = "unknown_outcome"
            provider_code = "timeout"
        elif provider_result.returncode == 0:
            outcome = "succeeded"
            provider_code = "restart_ok"
        else:
            outcome = "failed"
            provider_code = f"exit_{provider_result.returncode}"

        # 7. Record the outcome in the receipt
        status = {
            "succeeded": MutationStatus.MUTATION_SUCCEEDED,
            "failed": MutationStatus.MUTATION_FAILED,
            "unknown_outcome": MutationStatus.UNKNOWN_OUTCOME,
        }.get(outcome, MutationStatus.UNKNOWN_OUTCOME)
        self._receipts.complete(
            action_id=envelope.action_id,
            fencing_token=envelope.fencing_token,
            status=status,
            provider_code=provider_code,
        )

        # 8. Independent verification (fresh observation, not the exit code)
        verification_success = None
        if outcome == "succeeded":
            try:
                post_snapshot = self._provider.observe_unit(policy, now=moment)
                verification_success = (
                    post_snapshot.load_state == "loaded"
                    and post_snapshot.active_state == "active"
                    and post_snapshot.unit_id == pre_snapshot.unit_id
                )
            except SystemdRestartError:
                verification_success = False

        evidence_ref = f"systemd-restart:{envelope.action_id[:16]}:{outcome}"
        return StandaloneRestartResult(
            action_id=envelope.action_id,
            outcome=outcome,
            provider_code=provider_code,
            evidence_reference=evidence_ref,
        )

    def _fail_receipt(self, envelope: ExecutionEnvelope, *, stage: str) -> None:
        """Record a definitive pre-provider executor failure on the receipt.

        Never raises: if the receipt cannot be updated (e.g. contention),
        the original error must propagate; the receipt stays RECEIPT_CREATED
        and the mutation is treated as attempted/unknown by the CP.
        """
        try:
            self._receipts.complete(
                action_id=envelope.action_id,
                fencing_token=envelope.fencing_token,
                status=MutationStatus.MUTATION_FAILED,
                provider_code=f"executor_error:{stage}",
            )
        except Exception:
            pass

    def _validate_envelope(self, envelope: ExecutionEnvelope, *, now: datetime) -> None:
        """Structural validation of the envelope; no business authorization."""
        if envelope.is_expired(now):
            raise EnvelopeError("Execution envelope has expired")
        policy = self._provider.resolve_unit(self._policy.unit_id, environment=envelope.environment)
        if policy.target_id != envelope.target_id:
            raise EnvelopeError("Envelope target does not match the registered policy")
        if policy.environment != envelope.environment:
            raise EnvelopeError("Envelope environment does not match the registered policy")
