"""Application-layer composition of the canonical identity and authorization seam.

``OwnerControlPlaneService`` is the one place where owner authentication,
opaque sessions, the single authorization policy, explicit owner confirmation,
the durable action repository, and the canonical audit ledger are composed.
It owns no execution authority: it cannot execute, spawn, or touch anything
outside the control-plane state store.

State + evidence consistency (atomicity model OPTION A): security-sensitive
state transitions carry their audit drafts into the action repository's
transaction, so a state change without durable evidence is impossible and an
evidence failure rolls the state change back. Audit-only events (authentication,
denials, replay/conflict evidence) are appended directly; their failure raises
visibly rather than leaving a silent gap.
"""
from __future__ import annotations

from datetime import datetime, timezone

import json as _json

from aipm.control_plane.action_state import InMemoryActionRepository
from aipm.control_plane.audit.repository import InMemoryAuditLedger


def json_canonical(payload: dict) -> str:
    return _json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _verification_version_value() -> str:
    from aipm.control_plane.verification import VERIFICATION_VERSION

    return VERIFICATION_VERSION
from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.audit import builders as audit_builders
from aipm.control_plane.audit.models import AuditEventDraft
from aipm.control_plane.audit.repository import InMemoryAuditLedger
from aipm.control_plane.contracts import ActionRepository, LifecycleTransition
from aipm.control_plane.identity import OwnerPrincipal
from aipm.control_plane.lifecycle import advance as advance_lifecycle
from aipm.control_plane.models import (
    ActionLifecycle,
    ActionRequest,
    ActionScope,
    ControlPlaneError,
    LifecycleState,
    OperationKind,
    PlanningErrorCode,
    UpdateExecutionBinding,
)
from aipm.control_plane.owner_auth import OwnerAuthenticator
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import AuthorizationDecision, AuthorizationPolicy
from aipm.control_plane.project_plan import Environment, ProjectPlanError
from aipm.control_plane.executor import (
    EXECUTION_CONTRACT_VERSION,
    ExecutionContract,
    Executor,
    ExecutorCapability,
)
from aipm.control_plane.bridge import DryRunMutationRecord, LegacyUpdateIntent, UpdateActionRequestAdapter
from aipm.control_plane.rollback import ROLLBACK_PLAN_VERSION, RollbackPlan, RollbackSafetyCode, plan_rollback
from aipm.control_plane.session import OwnerSession, OwnerSessionStore


class _TerminalExecutionResult:
    """Lightweight result for terminal-state replays (no lease required)."""

    __slots__ = ("action_id", "outcome", "lifecycle_state", "mutated_revision", "verification_success", "verification_id")

    def __init__(self, *, action_id: str, outcome, lifecycle_state, mutated_revision=None, verification_success=None, verification_id=None):
        self.action_id = action_id
        self.outcome = outcome
        self.lifecycle_state = lifecycle_state
        self.mutated_revision = mutated_revision
        self.verification_success = verification_success
        self.verification_id = verification_id
from aipm.control_plane.verification import (
    ExpectedState,
    VerificationResult,
    observed_from_plan,
    verify,
)


class OwnerControlPlaneService:
    """Composition seam for the canonical flow; authority-free by construction."""

    __slots__ = (
        "_authenticator",
        "_sessions",
        "_policy",
        "_confirmations",
        "_plans",
        "_planner",
        "_audit",
        "_actions",
        "_kill_switches",
        "_dry_run_sink",
        "_current_plan_digest",
        "_update_runtime",
        "_execution_mode",
        "_executor_ipc_client",
        "_snapshot_repo",
        "_verification_repo",
        "_clock",
        "_initialized",
    )

    def __init__(
        self,
        *,
        authenticator: OwnerAuthenticator,
        sessions: OwnerSessionStore,
        policy: AuthorizationPolicy,
        confirmations: OwnerConfirmationService,
        plans,
        planner: PlanOnlyPlanner,
        audit=None,
        actions: ActionRepository | None = None,
        kill_switches=None,
        dry_run_sink=None,
        current_plan_digest=None,
        update_runtime=None,
        execution_mode: str = "test",  # production MUST explicitly pass "ipc"
        executor_ipc_client=None,
        clock=None,
    ) -> None:
        if not isinstance(authenticator, OwnerAuthenticator):
            raise TypeError("authenticator must be OwnerAuthenticator")
        if not isinstance(sessions, OwnerSessionStore):
            raise TypeError("sessions must be OwnerSessionStore")
        if not isinstance(policy, AuthorizationPolicy):
            raise TypeError("policy must be the canonical AuthorizationPolicy")
        if not isinstance(confirmations, OwnerConfirmationService):
            raise TypeError("confirmations must be OwnerConfirmationService")
        if not isinstance(planner, PlanOnlyPlanner):
            raise TypeError("planner must be PlanOnlyPlanner")
        if plans is None or not hasattr(plans, "read"):
            raise TypeError("plans must provide a read(target_id) view")
        ledger = audit if audit is not None else InMemoryAuditLedger()
        if not hasattr(ledger, "append") or not hasattr(ledger, "verify_chain"):
            raise TypeError("audit must implement the audit ledger contract")
        repository = actions if actions is not None else InMemoryActionRepository()
        if not isinstance(repository, ActionRepository):
            raise TypeError("actions must implement the ActionRepository contract")
        # One shared confirmation store: the confirmation service and the
        # action repository must never hold divergent confirmation state.
        object.__setattr__(confirmations, "_store", repository)
        # One shared evidence path: the repository commits the service's
        # audit drafts inside its own state transactions.
        if hasattr(repository, "_audit"):
            existing_sink = getattr(repository, "_audit", None)
            if existing_sink is not None and existing_sink is not ledger:
                raise TypeError("action repository must share the service audit ledger")
            object.__setattr__(repository, "_audit", ledger)
        object.__setattr__(self, "_authenticator", authenticator)
        object.__setattr__(self, "_sessions", sessions)
        object.__setattr__(self, "_policy", policy)
        object.__setattr__(self, "_confirmations", confirmations)
        object.__setattr__(self, "_plans", plans)
        object.__setattr__(self, "_planner", planner)
        object.__setattr__(self, "_audit", ledger)
        object.__setattr__(self, "_actions", repository)
        object.__setattr__(self, "_kill_switches", kill_switches)
        object.__setattr__(self, "_dry_run_sink", dry_run_sink)
        object.__setattr__(self, "_current_plan_digest", current_plan_digest)
        object.__setattr__(self, "_update_runtime", update_runtime)
        object.__setattr__(self, "_execution_mode", execution_mode)
        object.__setattr__(self, "_executor_ipc_client", executor_ipc_client)
        object.__setattr__(self, "_snapshot_repo", None)
        object.__setattr__(self, "_verification_repo", None)
        object.__setattr__(self, "_clock", clock or (lambda: datetime.now(timezone.utc)))
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("OwnerControlPlaneService configuration is immutable")
        object.__setattr__(self, name, value)

    # ------------------------------------------------------------------
    # Authentication and sessions
    # ------------------------------------------------------------------

    def login(self, secret, *, now: datetime | None = None) -> OwnerSession:
        """Authenticate the owner and open an opaque session for the principal.

        Evidence is appended before the session exists: an audit failure here
        refuses the login instead of leaving an unaudited session.
        """

        moment = self._now(now)
        result = self._authenticator.authenticate(secret, now=now)
        if not result.accepted or result.principal is None:
            self._audit.append(audit_builders.authentication_failure(reason_code=result.reason.value, occurred_at=moment))
            raise ControlPlaneError(PlanningErrorCode.AUTHENTICATION_REJECTED, "Owner authentication failed")
        principal = result.principal
        self._audit.append(audit_builders.authentication_success(actor_subject=principal.subject, occurred_at=moment))
        self._audit.append(audit_builders.session_created(actor_subject=principal.subject, occurred_at=moment))
        return self._sessions.create(principal=principal, now=now)

    def session(self, session_id: str, *, now: datetime | None = None) -> OwnerSession:
        """Resolve an opaque session identifier to an active session."""

        session = self._sessions.get(session_id, now=now)
        if session is None:
            raise ControlPlaneError(PlanningErrorCode.SESSION_INVALID, "No active owner session")
        return session

    def logout(self, session_id: str, *, now: datetime | None = None) -> None:
        moment = self._now(now)
        session = self._sessions.get(session_id, now=now)
        actor = session.principal.subject if session is not None else None
        if actor is not None:
            self._audit.append(audit_builders.session_revoked(actor_subject=actor, occurred_at=moment))
        self._sessions.revoke(session_id)

    def rotate_credentials(self, *, now: datetime | None = None) -> int:
        """Rotate the authentication epoch; every live session is revoked."""

        epoch = self._authenticator.rotate_auth_epoch()
        self._sessions.rotate_auth_epoch()
        self._audit.append(audit_builders.credential_epoch_rotated(occurred_at=self._now(now), epoch=epoch))
        return epoch

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    def authorize(self, session_id: str, request: ActionRequest, *, now: datetime | None = None, extra_evidence_drafts: tuple = (), lifecycle_refs: dict | None = None) -> AuthorizationDecision:
        """Authorize one bounded request for the session's principal.

        Durable idempotency: an existing action registered under the same
        ``(target_id, operation, idempotency_key)`` returns its stored decision
        when the canonical request matches (recording replay evidence) and
        raises a deterministic conflict otherwise (recording the conflict).
        """

        if not isinstance(request, ActionRequest):
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid action request")
        session = self.session(session_id, now=now)
        principal = session.principal
        moment = self._now(now)
        self._ensure_operation_permitted(request.environment)
        existing = self._actions.find_action_by_idempotency(
            target_id=request.target_id,
            operation=request.operation.value,
            idempotency_key=request.idempotency_key,
        )
        if existing is not None:
            stored_decision = self._actions.get_decision(existing.decision_id) if existing.decision_id else None
            if stored_decision is not None and stored_decision.request is not None:
                if stored_decision.request.canonical() == request.canonical():
                    self._audit.append(
                        audit_builders.action_idempotency_replay(
                            actor_subject=principal.subject,
                            occurred_at=moment,
                            action_id=existing.action_id,
                            decision_id=stored_decision.decision_id,
                        )
                    )
                    return stored_decision
            self._audit.append(
                audit_builders.action_idempotency_conflict(actor_subject=principal.subject, occurred_at=moment)
            )
            raise ControlPlaneError(PlanningErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency key already bound to a different request")
        plan = self._planner.plan(request)
        current_plan = self._read_current_plan(request.target_id)
        decision = self._policy.authorize(
            principal=principal,
            request=request,
            plan=plan,
            current_plan=current_plan,
            now=now,
        )
        if not decision.allowed:
            self._audit.append(audit_builders.authorization_denied(actor_subject=principal.subject, occurred_at=moment, decision=decision))
            return decision
        self._audit.append(audit_builders.authorization_allowed(actor_subject=principal.subject, occurred_at=moment, decision=decision))
        self._register_allowed_action(decision, request, principal, now=now, extra_evidence_drafts=extra_evidence_drafts, lifecycle_refs=lifecycle_refs)
        return decision

    def _execute_via_ipc(self, session_id: str, action_id: str, action, lease, decision, confirmation_id, snapshot, kill_switch_epoch, *, now=None):
        """Route execution through the executor service via Unix-domain IPC.

        This is the PRODUCTION path. There is no in-process fallback.
        If the executor service is unavailable, the execution fails with a
        bounded error and the action remains in its current state.
        """
        if self._executor_ipc_client is None:
            raise ControlPlaneError(PlanningErrorCode.SESSION_INVALID, "Executor IPC client is not configured")
        contract = ExecutionContract(
            contract_version=EXECUTION_CONTRACT_VERSION,
            action_id=action_id,
            action_version=action.version,
            operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
            target_id=action.scope.target_id,
            environment=action.scope.environment,
            plan_id=action.plan_id,
            expected_plan_revision=action.plan_revision,
            expected_plan_digest=decision.action_identity.target_digest,
            mutation_fields=tuple(decision.request.mutation_metadata),
            snapshot_id=snapshot.snapshot_id,
            decision_id=decision.decision_id,
            confirmation_id=confirmation_id,
            policy_version=action.scope.policy_version,
            verification_version=_verification_version_value(),
            kill_switch_epoch=kill_switch_epoch,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            expires_at=action.expires_at,
            capability_version="1",
        )
        from aipm.control_plane.executor_ipc import ExecutionRequest
        request = ExecutionRequest(
            action_id=contract.action_id,
            capability_id=contract.operation.value,
            target_id=contract.target_id,
            contract_digest=contract.digest(),
            lease_id=contract.lease_id,
            fencing_token=contract.fencing_token,
        )
        response = self._executor_ipc_client.send(request)
        return {
            "action_id": response.action_id,
            "outcome": response.outcome,
            "provider_code": response.provider_code,
            "evidence_reference": response.evidence_reference,
        }

    def _read_current_plan(self, target_id: str):
        try:
            return self._plans.read(target_id)
        except ProjectPlanError:
            return None

    def _ensure_safety_repos(self) -> None:
        """Resolve the durable snapshot/verification repositories.

        The durable implementations share the action repository's database
        and audit sink. In-memory test doubles provide equivalent methods.
        """

        if self._snapshot_repo is None:
            from aipm.control_plane.storage.sqlite_store import SQLitePlanSnapshotRepository, SQLiteVerificationRepository

            if isinstance(self._actions, InMemoryActionRepository):
                object.__setattr__(self, "_snapshot_repo", self._actions)
                object.__setattr__(self, "_verification_repo", self._actions)
            else:
                db = self._actions._db
                snapshot_repo = SQLitePlanSnapshotRepository(db)
                verification_repo = SQLiteVerificationRepository(db, audit=self._audit)
                object.__setattr__(self, "_snapshot_repo", snapshot_repo)
                object.__setattr__(self, "_verification_repo", verification_repo)
        return self._snapshot_repo

    # ------------------------------------------------------------------
    # Explicit owner confirmation
    # ------------------------------------------------------------------

    def confirm(self, session_id: str, decision_id: str, *, now: datetime | None = None):
        """Record the explicit owner confirmation for a previously issued decision.

        The confirmed binding and the lifecycle transition — together with
        their audit evidence — are committed by the action repository in one
        durable transaction, so a crash can never leave a confirmed
        authorization without its recorded confirmation and evidence.
        """

        if not isinstance(decision_id, str) or not decision_id:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Invalid decision reference")
        session = self.session(session_id, now=now)
        moment = self._now(now)
        decision = self._actions.get_decision(decision_id)
        if decision is None:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Unknown authorization decision")
        if decision.is_expired(now or self._clock()):
            raise ControlPlaneError(PlanningErrorCode.EXPIRED_PLAN, "Authorization decision has expired")
        pending = self._confirmations.request_confirmation(
            decision,
            requester_subject=decision.principal_subject,
            now=now,
        )
        try:
            confirmed = self._confirmations.build_confirmation(
                pending,
                confirmed_by_subject=session.principal.subject,
                now=now,
            )
        except ControlPlaneError as error:
            self._audit.append(
                audit_builders.owner_confirmation_rejected(
                    actor_subject=session.principal.subject,
                    occurred_at=moment,
                    decision=decision,
                    reason_code=error.code.value,
                )
            )
            raise
        lifecycle = self._actions.get_action(confirmed.action_id)
        if lifecycle is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Confirmed action is missing from the store")
        drafts = (
            audit_builders.owner_confirmation_requested(actor_subject=confirmed.requester_subject, occurred_at=moment, binding=pending),
            audit_builders.owner_confirmed(actor_subject=session.principal.subject, occurred_at=moment, binding=confirmed),
            audit_builders.lifecycle_transition_confirmed(
                actor_subject=session.principal.subject,
                occurred_at=moment,
                binding=confirmed,
                from_state=lifecycle.state.value,
                to_state=LifecycleState.CONFIRMED.value,
            ),
        )
        advanced = self._actions.record_confirmation_with_advance(
            confirmed,
            LifecycleTransition(
                action_id=confirmed.action_id,
                expected_version=lifecycle.version,
                next_state=LifecycleState.CONFIRMED,
                approver_subject=session.principal.subject,
                now=now or self._clock(),
            ),
            audit_drafts=drafts,
        )
        return confirmed

    # ------------------------------------------------------------------
    # Verification, snapshot, and rollback safety layer
    # ------------------------------------------------------------------

    def _ensure_operation_permitted(self, environment: str) -> None:
        """Fail-closed kill-switch gate for mutation-preparing operations.

        Policy: any operation that prepares or performs a mutation requires
        the staging switch to be explicitly disengaged; production is
        permanently denied; an unconfigured kill switch is a test-only
        composition and is reported as such by the executor contract, which
        refuses to run without one. Evidence-only flows (verification
        recording) are never blocked: evidence is safety.
        """

        if self._kill_switches is None:
            return
        if not self._kill_switches.permits(environment):
            raise ControlPlaneError(PlanningErrorCode.UNAVAILABLE_TARGET, "Kill switch is engaged for this environment")

    def capture_snapshot(self, session_id: str, action_id: str, *, now: datetime | None = None):
        """Capture the immutable pre-mutation snapshot for a confirmed action.

        The snapshot is bound to the action's exact bound plan revision; the
        composite commits snapshot + lifecycle advance + audit atomically, so
        the lifecycle can never represent SNAPSHOT_CAPTURED without a durable
        snapshot.
        """

        import secrets as _secrets

        from aipm.control_plane.storage.sqlite_store import PlanSnapshot

        self._ensure_safety_repos()
        session = self.session(session_id, now=now)
        lifecycle = self._actions.get_action(action_id)
        if lifecycle is None:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Unknown action")
        if lifecycle.state is not LifecycleState.CONFIRMED:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Snapshot capture requires a confirmed action")
        self._ensure_operation_permitted(lifecycle.scope.environment)
        decision = self._actions.get_decision(lifecycle.decision_id) if lifecycle.decision_id else None
        if decision is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Confirmed action has no durable decision")
        current_plan = self._read_current_plan(lifecycle.scope.target_id)
        if current_plan is None or current_plan.revision != lifecycle.plan_revision:
            raise ControlPlaneError(PlanningErrorCode.STALE_EVIDENCE, "Stale target: plan no longer matches the action revision")
        from aipm.control_plane.storage.sqlite_store import SNAPSHOT_VERSION_DEFAULT

        snapshot = PlanSnapshot(
            snapshot_id=_secrets.token_hex(16),
            target_id=current_plan.target_id,
            environment=current_plan.environment.value,
            revision=current_plan.revision,
            canonical_digest=current_plan.digest(),
            payload_canonical=json_canonical(current_plan.canonical_payload()),
            action_id=action_id,
            plan_id=lifecycle.plan_id,
            captured_at=self._now(now),
            snapshot_version=SNAPSHOT_VERSION_DEFAULT,
        )
        moment = self._now(now)
        drafts = (
            audit_builders.lifecycle_transition(
                actor_subject=session.principal.subject,
                occurred_at=moment,
                decision=decision,
                from_state=LifecycleState.CONFIRMED.value,
                to_state=LifecycleState.SNAPSHOT_CAPTURED.value,
            ),
        )
        self._actions.capture_snapshot_and_advance(
            snapshot,
            action_id=action_id,
            expected_version=lifecycle.version,
            now=now or self._clock(),
            audit_drafts=drafts,
        )
        return snapshot

    def record_verification(self, session_id: str, action_id: str, expected: ExpectedState, *, now: datetime | None = None) -> VerificationResult:
        """Independently verify one action's expected post-condition.

        The observation is a fresh read of the durable plan state — never the
        mutation response. The result and its audit evidence are committed
        atomically; verification evidence is never blocked by the kill switch.
        """

        self._ensure_safety_repos()
        session = self.session(session_id, now=now)
        lifecycle = self._actions.get_action(action_id)
        if lifecycle is None:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Unknown action")
        if lifecycle.is_expired(now or self._clock()):
            raise ControlPlaneError(PlanningErrorCode.EXPIRED_PLAN, "Action has expired")
        moment = self._now(now)
        started = audit_builders.verification_started(
            actor_subject=session.principal.subject,
            occurred_at=moment,
            action_id=action_id,
            plan_id=lifecycle.plan_id,
            plan_revision=expected.revision,
            plan_digest=expected.canonical_digest,
            target_id=expected.target_id,
            environment=expected.environment,
        )
        self._audit.append(started)
        try:
            current_plan = self._plans.read(lifecycle.scope.target_id)
        except ProjectPlanError:
            from aipm.control_plane.verification import VerificationCode

            result = VerificationResult(
                verification_id=__import__("secrets").token_hex(16),
                action_id=action_id,
                success=False,
                reason_code=VerificationCode.PLAN_MISSING,
                expected_revision=expected.revision,
                observed_revision=None,
                expected_digest=expected.canonical_digest,
                observed_digest=None,
                observed_at=moment,
            )
            self._verification_repo.save(result)
            return result
        result = verify(expected, observed_from_plan(current_plan), action_id=action_id)
        finished = audit_builders.verification_finished(
            actor_subject=session.principal.subject,
            occurred_at=moment,
            action_id=action_id,
            plan_id=lifecycle.plan_id,
            plan_revision=expected.revision,
            plan_digest=expected.canonical_digest,
            target_id=expected.target_id,
            environment=expected.environment,
            verification_id=result.verification_id,
            success=result.success,
            reason_code=result.reason_code.value,
            verification_version=result.verification_version,
        )
        self._verification_repo.save(result, audit_drafts=(finished,))
        return result

    def plan_rollback(self, session_id: str, original_action_id: str, *, now: datetime | None = None) -> RollbackPlan:
        """Plan the rollback of one original action against its snapshot.

        Pure safety planning: snapshot integrity and binding, reversibility,
        and the compare-and-set rule against the failed mutation's
        deterministic post-condition. No state changes.
        """

        self._ensure_safety_repos()
        self.session(session_id, now=now)
        original = self._actions.get_action(original_action_id)
        if original is None:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Unknown action")
        snapshot = self._snapshot_repo.snapshot_for_action(original_action_id)
        decision = self._actions.get_decision(original.decision_id) if original.decision_id else None
        mutation_fields = dict(decision.request.mutation_metadata) if decision is not None and decision.request is not None else None
        current_plan = self._plans.read(original.scope.target_id)
        return plan_rollback(
            original_action=original,
            snapshot=snapshot,
            current_plan=current_plan,
            mutation_fields=mutation_fields,
        )

    def request_rollback(self, session_id: str, original_action_id: str, *, now: datetime | None = None) -> AuthorizationDecision:
        """Request the rollback action for a failed original action.

        The rollback request is a NEW bounded action with its own identity,
        authorization, confirmation semantics, snapshot reference, lifecycle,
        and audit trail. The original action is never mutated. The rollback
        request carries a deterministic idempotency key derived from the
        original action, so one rollback per original is enforceable by the
        database uniqueness constraint.
        """

        session = self.session(session_id, now=now)
        moment = self._now(now)
        plan = self.plan_rollback(session_id, original_action_id, now=now)
        if not plan.safe:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, f"Rollback is not safe: {plan.reason_code.value}")
        snapshot = self._snapshot_repo.snapshot_for_action(original_action_id)
        rollback_request = ActionRequest(
            operation=OperationKind.ROLLBACK_PROJECT_PLAN,
            target_id=plan.target_id,
            idempotency_key=f"rollback-{original_action_id[:64]}",
            metadata=(),
            environment=plan.environment,
        )
        extra = (
            audit_builders.rollback_requested(
                actor_subject=session.principal.subject,
                occurred_at=moment,
                action_id=original_action_id,
                snapshot_id=plan.snapshot_id,
                plan_id=snapshot.plan_id if snapshot else None,
                plan_revision=snapshot.revision if snapshot else None,
                plan_digest=snapshot.canonical_digest if snapshot else None,
                target_id=plan.target_id,
                environment=plan.environment,
                decision_id=None,
                policy_version=self._policy.policy_version,
            ),
        )
        decision = self.authorize(
            session.session_id,
            rollback_request,
            now=now,
            extra_evidence_drafts=extra,
            lifecycle_refs={"rollback_of_action_id": original_action_id, "snapshot_id": plan.snapshot_id},
        )
        if decision.allowed:
            from aipm.control_plane.models import LifecycleState as _LifecycleState

            original = self._actions.get_action(original_action_id)
            if original is not None and original.state in {_LifecycleState.VERIFICATION_FAILED, _LifecycleState.EXECUTION_FAILED}:
                self._actions.advance_rollback_state(
                    original_action_id,
                    expected_version=original.version,
                    from_states={_LifecycleState.VERIFICATION_FAILED, _LifecycleState.EXECUTION_FAILED},
                    to_state=_LifecycleState.ROLLBACK_REQUESTED,
                    now=now or self._clock(),
                )
        return decision

    # ------------------------------------------------------------------
    # Bounded execution (the only mutation path; transport arrives later)
    # ------------------------------------------------------------------

    def _executor(self) -> Executor:
        return Executor(
            plans=self._plans,
            actions=self._actions,
            confirmations=self._confirmations,
            kill_switches=self._kill_switches,
            audit=self._audit,
            snapshots=self._snapshot_repo,
        )

    def execute_action(self, session_id: str, action_id: str, *, now: datetime | None = None):
        """Run the bounded execution vertical slice for one confirmed action.

        Orchestration only: the lease grantor, the contract builder, and the
        tiny executor do the work. The kill switch is re-checked inside the
        executor at the mutation boundary; the confirmation is consumed
        exactly once; the mutation is a single atomic transaction with its
        lifecycle and evidence; verification is an independent read-back.
        """

        self._ensure_safety_repos()
        session = self.session(session_id, now=now)
        moment = self._now(now)
        action = self._actions.get_action(action_id)
        if action is None:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Unknown action")
        if action.is_expired(moment):
            raise ControlPlaneError(PlanningErrorCode.EXPIRED_PLAN, "Action has expired")
        if action.state in {LifecycleState.VERIFIED_SUCCESS, LifecycleState.ROLLED_BACK, LifecycleState.EXECUTION_FAILED, LifecycleState.ROLLBACK_FAILED}:
            # Terminal: return the durable state without requiring a lease.
            from aipm.control_plane.verification import ExecutionOutcome

            outcome_value = self._actions.outcome_for_action(action_id)
            outcome = ExecutionOutcome(outcome_value) if outcome_value else ExecutionOutcome.MUTATION_NOT_STARTED
            return _TerminalExecutionResult(action_id=action_id, outcome=outcome, lifecycle_state=action.state)
        if action.rollback_of_action_id is not None:
            # The service routes rollback actions to the rollback executor;
            # the transport never chooses execution semantics.
            return self.execute_rollback(
                session_id,
                original_action_id=action.rollback_of_action_id,
                rollback_action_id=action_id,
                now=now,
            )
        decision = self._actions.get_decision(action.decision_id) if action.decision_id else None
        if decision is None or decision.request is None or decision.action_identity is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Action has no durable decision binding")
        confirmation_id = None
        for binding in self._confirmations.store.values():
            if binding.action_id == action_id and binding.state.value in {"confirmed", "consumed"}:
                confirmation_id = binding.confirmation_id
                break
        if confirmation_id is None:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Action has no confirmation binding")
        snapshot = self._snapshot_repo.snapshot_for_action(action_id)
        if snapshot is None:
            raise ControlPlaneError(PlanningErrorCode.UNAVAILABLE_EVIDENCE, "Action has no pre-mutation snapshot")

        if action.state is LifecycleState.SNAPSHOT_CAPTURED:
            lease, advanced = self._actions.acquire_lease(action_id, expected_version=action.version, now=moment)
            action = advanced
        lease = self._actions.active_lease(action_id, now=moment)
        if lease is None:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Action has no active lease")
        kill_switch_epoch = self._kill_switches.switch(action.scope.environment).epoch if self._kill_switches is not None else 1
        if self._execution_mode == "ipc":
            return self._execute_via_ipc(session_id, action_id, action, lease, decision, confirmation_id, snapshot, kill_switch_epoch, now=now)
        contract = ExecutionContract(
            contract_version=EXECUTION_CONTRACT_VERSION,
            action_id=action.action_id,
            action_version=action.version,
            operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
            target_id=action.scope.target_id,
            environment=action.scope.environment,
            plan_id=action.plan_id,
            expected_plan_revision=action.plan_revision,
            expected_plan_digest=decision.action_identity.target_digest,
            mutation_fields=tuple(decision.request.mutation_metadata),
            snapshot_id=snapshot.snapshot_id,
            decision_id=decision.decision_id,
            confirmation_id=confirmation_id,
            policy_version=action.scope.policy_version,
            verification_version=_verification_version_value(),
            kill_switch_epoch=kill_switch_epoch,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            expires_at=action.expires_at,
        )
        if action.rollback_of_action_id is not None:
            # The service routes rollback actions to the rollback executor;
            # the transport never chooses execution semantics.
            return self.execute_rollback(
                session_id,
                original_action_id=action.rollback_of_action_id,
                rollback_action_id=action_id,
                now=now,
            )
        executor = self._executor()
        return executor.execute(contract, now=self._now(now))

    def execute_rollback(self, session_id: str, original_action_id: str, rollback_action_id: str, *, now: datetime | None = None):
        """Run the bounded rollback vertical slice for one failed action."""

        self._ensure_safety_repos()
        self.session(session_id, now=now)
        executor = self._executor()
        return executor.execute_rollback(
            rollback_action_id=rollback_action_id,
            original_action_id=original_action_id,
            now=self._now(now),
        )

    def reconcile_action(self, session_id: str, action_id: str, *, now: datetime | None = None):
        """Reconcile an UNKNOWN_OUTCOME action by independent observation."""

        self._ensure_safety_repos()
        self.session(session_id, now=now)
        action = self._actions.get_action(action_id)
        if action is None:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Unknown action")
        moment = self._now(now)
        decision = self._actions.get_decision(action.decision_id) if action.decision_id else None
        if decision is None or decision.request is None or decision.action_identity is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Action has no durable decision binding")
        lease = self._actions.active_lease(action_id, now=moment) or self._last_lease(action_id)
        if lease is None:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Action has no lease to reconcile against")
        kill_switch_epoch = self._kill_switches.switch(action.scope.environment).epoch if self._kill_switches is not None else 1
        contract = ExecutionContract(
            contract_version=EXECUTION_CONTRACT_VERSION,
            action_id=action.action_id,
            action_version=action.version,
            operation=ExecutorCapability.UPDATE_PROJECT_PLAN,
            target_id=action.scope.target_id,
            environment=action.scope.environment,
            plan_id=action.plan_id,
            expected_plan_revision=action.plan_revision,
            expected_plan_digest=decision.action_identity.target_digest,
            mutation_fields=tuple(decision.request.mutation_metadata),
            snapshot_id=action.snapshot_id or "unknown",
            decision_id=decision.decision_id,
            confirmation_id=self._confirmation_id_for(action_id) or "unknown",
            policy_version=action.scope.policy_version,
            verification_version=_verification_version_value(),
            kill_switch_epoch=kill_switch_epoch,
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            expires_at=action.expires_at,
        )
        return self._executor().reconcile(contract, now=self._now(now))

    def dry_run_update_intent(self, session_id: str, intent: LegacyUpdateIntent, *, now: datetime | None = None):
        """Run a legacy update intent through the canonical flow WITHOUT mutating.

        Stage-C bridge operation: intent → adapter → authorize → confirm →
        snapshot → lease → contract → dry-run record. The plan is never
        mutated, the confirmation is consumed, and the action rests at
        LEASED ready for a real (separately authorized) execution. The
        dry-run record is evidence, not control-plane state.
        """

        if self._dry_run_sink is None:
            raise ControlPlaneError(PlanningErrorCode.SESSION_INVALID, "Dry-run sink is not configured")
        allowed_targets = frozenset(target for target, _environment in self._policy.allowed_scopes)
        adapter = UpdateActionRequestAdapter(plan_store=self._plans, allowed_projects=allowed_targets)
        session = self.session(session_id, now=now)
        try:
            request = adapter.adapt(intent)
        except Exception as exc:
            # Adapter refusals are bounded denials, never exceptions to the caller.
            return {"allowed": False, "code": "intent_refused", "reason": str(exc)[:128], "dry_run": True}
        decision = self.authorize(session.session_id, request, now=now)
        if not decision.allowed:
            return {"allowed": False, "code": decision.code.value, "dry_run": True}
        identity = decision.action_identity
        if identity is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Allowed decision carries no identity")
        binding = self.confirm(session.session_id, decision.decision_id, now=now)
        snapshot = self.capture_snapshot(session.session_id, identity.action_id, now=now)
        action = self._actions.get_action(identity.action_id)
        if action is None or action.state is not LifecycleState.SNAPSHOT_CAPTURED:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Dry-run requires a snapshot-captured action")
        lease, _advanced = self._actions.acquire_lease(identity.action_id, expected_version=action.version, now=now)
        current_plan = self._plans.read(identity.target_id)
        record = self._dry_run_sink.record(
            action_id=identity.action_id,
            target_id=identity.target_id,
            environment=identity.environment,
            plan_id=identity.plan_id,
            pre_mutation_revision=current_plan.revision,
            pre_mutation_digest=current_plan.digest(),
            mutation_fields=dict(request.metadata),
            lease_id=lease.lease_id,
            fencing_token=lease.fencing_token,
            operation=request.operation.value,
        )
        return {
            "allowed": True,
            "dry_run": True,
            "action_id": identity.action_id,
            "decision_id": decision.decision_id,
            "confirmation_id": binding.confirmation_id,
            "snapshot_id": snapshot.snapshot_id,
            "lease_id": lease.lease_id,
            "fencing_token": lease.fencing_token,
            "record": record.safe_dict(),
        }

    def _confirmation_id_for(self, action_id: str):
        for binding in self._confirmations.store.values():
            if binding.action_id == action_id:
                return binding.confirmation_id
        return None

    def _mutation_pairs_for_target(self, target_id: str) -> tuple[tuple[str, str], ...]:
        """Current title/objective of the authoritative plan (bridge precedent).

        The revision-bump mutation fields for a composed update approval:
        the digest binding is what pins the approval; the mutation fields
        keep the canonical contract non-empty without inventing new plan
        content through the approval channel.
        """

        try:
            plan = self._plans.read(target_id)
        except ProjectPlanError as exc:
            raise ControlPlaneError(PlanningErrorCode.UNAVAILABLE_EVIDENCE, "Current plan is unavailable") from exc
        return (("objective", plan.objective), ("title", plan.title))

    def _update_binding_for(self, action: ActionLifecycle) -> "UpdateExecutionBinding":
        """Derive the update execution binding from trusted durable state.

        Composed ONLY here, after the canonical gated execution reached
        VERIFIED_SUCCESS: the plan digest is the presented update-plan
        digest recovered from the durable decision metadata pair (the
        ``UpdatePlanIdentity`` digest space — never the control-plane
        action digest, a different space), the confirmation reference is
        the durable binding, and the project identity is the action's
        bound target. Transport, dashboard, and the approval surface never
        construct this binding. The composition root adapts it to the
        engine-side execution contract; this layer never names engine
        types.
        """

        decision = self._actions.get_decision(action.decision_id) if action.decision_id else None
        if decision is None or decision.request is None or decision.action_identity is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Action has no durable decision binding")
        pairs = dict(decision.request.metadata)
        plan_digest = pairs.get("update_plan_digest")
        if not isinstance(plan_digest, str) or not plan_digest:
            raise ControlPlaneError(PlanningErrorCode.STALE_EVIDENCE, "Authorized decision binds no update plan digest")
        confirmation_id = self._confirmation_id_for(action.action_id)
        if confirmation_id is None:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Action has no confirmation binding")
        if self._update_runtime is None:
            raise ControlPlaneError(PlanningErrorCode.UNAVAILABLE_EVIDENCE, "Update runtime is not composed")
        return UpdateExecutionBinding(
            project_name=action.scope.target_id,
            plan_digest=plan_digest,
            confirmation_id=confirmation_id,
        )

    def _assert_binding_digest(self, decision, presented_digest: str) -> None:
        """Fail closed unless the durable decision binds the presented digest.

        The presented update-plan digest is carried inside the canonical
        request metadata (the binding channel); the decision's embedded
        request is the durable copy. NOTE: ``identity.plan_digest`` is the
        control-plane ActionPlan digest — a DIFFERENT digest space from the
        update-plan identity — so the comparison is against the durable
        metadata pair, never against ``identity.plan_digest``.
        """

        request = decision.request
        pairs = dict(request.metadata) if request is not None else {}
        if pairs.get("update_plan_digest") != presented_digest:
            raise ControlPlaneError(PlanningErrorCode.STALE_EVIDENCE, "Authorized identity does not bind the presented digest")

    # ------------------------------------------------------------------
    # C4: canonical update-plane composition (approve + run)
    # ------------------------------------------------------------------

    def approve_update_plan(
        self,
        session_id: str,
        *,
        target_id: str,
        environment: str,
        presented_digest: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> dict:
        """Compose the canonical update approval: digest binding → authorize → confirm.

        The presented ``update_plan_digest`` is verified at this boundary
        against the authoritative plan identity through the injected
        ``current_plan_digest`` port. On success the digest travels as
        binding metadata inside a canonical ``ActionRequest`` whose mutation
        fields are the plan's CURRENT title/objective (revision-bump
        mutation, bridge precedent), so the durable identity, confirmation,
        and contracts all bind to the exact plan content the operator
        approved. Transport never performs any of this itself.
        """

        if not isinstance(target_id, str) or not target_id:
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid target reference")
        if not isinstance(environment, str) or not environment:
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Invalid environment")
        if not isinstance(presented_digest, str) or len(presented_digest) != 64 or any(character not in "0123456789abcdef" for character in presented_digest):
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "A 64-hex update plan digest is required")
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 128:
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "A bounded idempotency key is required")
        if self._current_plan_digest is None:
            raise ControlPlaneError(PlanningErrorCode.UNAVAILABLE_EVIDENCE, "Plan digest verification is not composed")
        authoritative_digest = self._current_plan_digest(target_id)
        if not isinstance(authoritative_digest, str) or len(authoritative_digest) != 64 or any(character not in "0123456789abcdef" for character in authoritative_digest):
            raise ControlPlaneError(PlanningErrorCode.UNAVAILABLE_EVIDENCE, "Authoritative plan digest is unavailable")
        if authoritative_digest != presented_digest:
            # Fail closed: the plan changed after the operator reviewed it.
            raise ControlPlaneError(PlanningErrorCode.STALE_EVIDENCE, "Presented plan digest does not match the authoritative plan")
        mutation_pairs = self._mutation_pairs_for_target(target_id)
        if not mutation_pairs:
            raise ControlPlaneError(PlanningErrorCode.UNAVAILABLE_EVIDENCE, "Current plan mutation fields are unavailable")
        request = ActionRequest(
            operation=OperationKind.UPDATE_PROJECT_PLAN,
            target_id=target_id,
            idempotency_key=idempotency_key,
            metadata=mutation_pairs + (("update_plan_digest", presented_digest),),
            environment=environment,
        )
        session = self.session(session_id, now=now)
        decision = self.authorize(session.session_id, request, now=now)
        if not decision.allowed:
            return {
                "allowed": False,
                "code": decision.code.value,
                "decision_id": decision.decision_id,
                "action_id": None,
                "confirmation_id": None,
            }
        identity = decision.action_identity
        if identity is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Allowed decision carries no identity")
        self._assert_binding_digest(decision, presented_digest)
        existing_confirmation_id = self._confirmation_id_for(identity.action_id)
        if existing_confirmation_id is not None:
            # Idempotent replay of an already-confirmed approval: the
            # durable binding stands, no second confirmation is recorded.
            return {
                "allowed": True,
                "code": decision.code.value,
                "decision_id": decision.decision_id,
                "action_id": identity.action_id,
                "confirmation_id": existing_confirmation_id,
                "plan_digest": presented_digest,
            }
        binding = self.confirm(session.session_id, decision.decision_id, now=now)
        return {
            "allowed": True,
            "code": decision.code.value,
            "decision_id": decision.decision_id,
            "action_id": identity.action_id,
            "confirmation_id": binding.confirmation_id,
            "plan_digest": presented_digest,
        }

    def run_approved_update(self, session_id: str, *, action_id: str, now: datetime | None = None):
        """Run one approved update action through the canonical gated flow.

        Delegates to the canonical execution path (snapshot capture,
        confirmations consumed exactly once, lease + fencing, gate checks,
        bounded mutation, independent verification). On a verified outcome,
        the update execution binding is derived from trusted durable state
        and handed to the injected runtime port (the composition root binds
        it to the update engine); a runtime failure is fail-closed with the
        action's terminal state preserved. No retry, no fallback path.
        """

        if not isinstance(action_id, str) or not action_id:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Invalid action reference")
        action = self._actions.get_action(action_id)
        if action is None:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Unknown action")
        was_terminal = action.state in {
            LifecycleState.VERIFIED_SUCCESS,
            LifecycleState.ROLLED_BACK,
            LifecycleState.EXECUTION_FAILED,
            LifecycleState.ROLLBACK_FAILED,
        }
        if action.state is LifecycleState.CONFIRMED:
            # Fresh approval: the pre-mutation snapshot is part of the
            # canonical execution preparation (capture → lease → mutate).
            self.capture_snapshot(session_id, action_id, now=now)
        result = self.execute_action(session_id, action_id, now=now)
        action = self._actions.get_action(action_id)
        if action is None:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Executed action is missing from the store")
        if action.state is not LifecycleState.VERIFIED_SUCCESS or was_terminal:
            # Terminal replay: the confirmation was already consumed and
            # the durable outcome stands; the runtime never re-runs. A
            # re-application needs a fresh authorize → confirm chain.
            return {
                "executed": False,
                "action_id": action_id,
                "outcome": result.outcome.value if hasattr(result.outcome, "value") else str(result.outcome),
                "lifecycle_state": action.state.value,
            }
        binding = self._update_binding_for(action)
        self._update_runtime(binding)
        return {
            "executed": True,
            "action_id": action_id,
            "outcome": result.outcome.value if hasattr(result.outcome, "value") else str(result.outcome),
            "lifecycle_state": action.state.value,
        }

    def _last_lease(self, action_id: str):
        getter = getattr(self._actions, "last_lease", None)
        return getter(action_id) if getter else None

    # ------------------------------------------------------------------
    # Kill switch operator verbs (staging only; production is permanent)
    # ------------------------------------------------------------------

    def kill_switch_status(self) -> dict:
        """Bounded posture of every persisted kill switch."""

        if self._kill_switches is None:
            raise ControlPlaneError(PlanningErrorCode.SESSION_INVALID, "Kill switch is not configured for this composition")
        rows = []
        for switch in self._kill_switches._store.records():
            rows.append({
                "environment": switch.environment.value,
                "state": switch.state.value,
                "epoch": switch.epoch,
                "permits_operations": switch.permits_operations(),
            })
        return {"switches": rows}

    def engage_kill_switch(self, actor_subject: str, *, reason: str, now: datetime | None = None) -> dict:
        if self._kill_switches is None:
            raise ControlPlaneError(PlanningErrorCode.SESSION_INVALID, "Kill switch is not configured for this composition")
        switch = self._kill_switches.engage(Environment.STAGING, reason=reason, now=now or self._clock(), actor_subject=actor_subject)
        return {"environment": switch.environment.value, "state": switch.state.value, "epoch": switch.epoch}

    def disengage_kill_switch(self, actor_subject: str, *, reason: str, now: datetime | None = None) -> dict:
        if self._kill_switches is None:
            raise ControlPlaneError(PlanningErrorCode.SESSION_INVALID, "Kill switch is not configured for this composition")
        switch = self._kill_switches.disengage(Environment.STAGING, reason=reason, now=now or self._clock(), actor_subject=actor_subject)
        return {"environment": switch.environment.value, "state": switch.state.value, "epoch": switch.epoch}

    def confirm_action(self, session_id: str, action_id: str, *, now: datetime | None = None):
        """Confirm by ACTION identity: resolves the durable decision binding.

        The transport knows actions, not decision ids; this resolver keeps
        the lookup inside the service boundary.
        """

        if not isinstance(action_id, str) or not action_id:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Invalid action reference")
        action = self._actions.get_action(action_id)
        if action is None or not action.decision_id:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Unknown action")
        return self.confirm(session_id, action.decision_id, now=now)

    # ------------------------------------------------------------------
    # Introspection (bounded, test/operator views)
    # ------------------------------------------------------------------

    def decision(self, decision_id: str) -> AuthorizationDecision | None:
        return self._actions.get_decision(decision_id)

    def lifecycle(self, action_id: str) -> ActionLifecycle | None:
        return self._actions.get_action(action_id)

    def confirmations(self):
        return self._confirmations.store

    def plan_view(self, target_id: str) -> dict | None:
        """Bounded read view of one ProjectPlan; None when unregistered."""

        try:
            plan = self._plans.read(target_id)
        except ProjectPlanError:
            return None
        return plan.safe_dict()

    def action_view(self, action_id: str) -> dict | None:
        """Bounded read view of one action; no decision payloads."""

        action = self._actions.get_action(action_id)
        if action is None:
            return None
        return {
            "action_id": action.action_id,
            "state": action.state.value,
            "operation": action.operation.value,
            "target_id": action.scope.target_id,
            "environment": action.scope.environment,
            "plan_id": action.plan_id,
            "plan_revision": action.plan_revision,
            "requester_subject": action.requester_subject,
            "idempotency_key": action.idempotency_key,
            "rollback_of_action_id": action.rollback_of_action_id,
            "snapshot_id": action.snapshot_id,
            "version": action.version,
            "outcome": self._actions.outcome_for_action(action_id),
            "expires_at": action.expires_at.isoformat(),
        }

    def audit_for_action(self, action_id: str, *, limit: int = 100) -> tuple:
        """Bounded audit events referencing one action, oldest last."""

        return tuple(event for event in self._audit.events(limit=4096) if event.draft.action_id == action_id)[-limit:]

    def audit_events(self, limit: int = 256):
        return self._audit.events(limit)

    def verify_audit_chain(self):
        return self._audit.verify_chain()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _register_allowed_action(self, decision: AuthorizationDecision, request: ActionRequest, principal: OwnerPrincipal, *, now, extra_evidence_drafts: tuple = (), lifecycle_refs: dict | None = None) -> None:
        identity = decision.action_identity
        if identity is None:
            raise ControlPlaneError(PlanningErrorCode.INVALID_REQUEST, "Allowed decision carries no identity")
        moment = self._now(now)
        lifecycle = ActionLifecycle(
            action_id=identity.action_id,
            decision_id=decision.decision_id,
            plan_id=identity.plan_id,
            plan_digest=identity.plan_digest,
            plan_revision=identity.target_revision,
            operation=request.operation,
            scope=ActionScope(target_id=identity.target_id, environment=identity.environment, policy_version=identity.policy_version),
            state=LifecycleState.REQUESTED,
            requester_subject=identity.requester_subject,
            idempotency_key=request.idempotency_key,
            created_at=decision.decided_at,
            expires_at=decision.expires_at,
            rollback_of_action_id=(lifecycle_refs or {}).get("rollback_of_action_id"),
            snapshot_id=(lifecycle_refs or {}).get("snapshot_id"),
        )
        planned = advance_lifecycle(lifecycle, LifecycleState.PLANNED, now=decision.decided_at)
        confirmation_required = advance_lifecycle(planned, LifecycleState.CONFIRMATION_REQUIRED, now=decision.decided_at)
        drafts = (
            audit_builders.action_created(
                actor_subject=principal.subject,
                occurred_at=moment,
                decision=decision,
                lifecycle_from=LifecycleState.REQUESTED.value,
                lifecycle_to=LifecycleState.CONFIRMATION_REQUIRED.value,
            ),
            audit_builders.lifecycle_transition(
                actor_subject=principal.subject,
                occurred_at=moment,
                decision=decision,
                from_state=LifecycleState.REQUESTED.value,
                to_state=LifecycleState.PLANNED.value,
            ),
            audit_builders.lifecycle_transition(
                actor_subject=principal.subject,
                occurred_at=moment,
                decision=decision,
                from_state=LifecycleState.PLANNED.value,
                to_state=LifecycleState.CONFIRMATION_REQUIRED.value,
            ),
        )
        self._actions.register_action(decision, confirmation_required, audit_drafts=drafts + tuple(extra_evidence_drafts))

    def _now(self, now: datetime | None = None) -> datetime:
        value = now or self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
