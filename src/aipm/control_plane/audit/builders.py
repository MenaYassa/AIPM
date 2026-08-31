"""Typed builders for canonical audit events.

Every control-plane producer constructs its events through these factories so
actor semantics, references, and bounds stay uniform. Builders never accept
free-form payloads; only the bounded fields of the canonical event model.
"""
from __future__ import annotations

from datetime import datetime

from aipm.control_plane.audit.models import (
    SYSTEM_ACTOR_SUBJECT,
    UNAUTHENTICATED_ACTOR_SUBJECT,
    AuditActorRole,
    AuditEventDraft,
    AuditEventType,
)


def authentication_success(*, actor_subject: str, occurred_at: datetime) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.AUTHENTICATION_SUCCESS,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.REQUESTER,
        occurred_at=occurred_at,
        result_code="accepted",
    )


def authentication_failure(*, reason_code: str, occurred_at: datetime) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.AUTHENTICATION_FAILURE,
        actor_subject=UNAUTHENTICATED_ACTOR_SUBJECT,
        actor_role=AuditActorRole.SYSTEM,
        occurred_at=occurred_at,
        result_code=reason_code,
    )


def session_created(*, actor_subject: str, occurred_at: datetime) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.SESSION_CREATED,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.REQUESTER,
        occurred_at=occurred_at,
    )


def session_revoked(*, actor_subject: str, occurred_at: datetime) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.SESSION_REVOKED,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.REQUESTER,
        occurred_at=occurred_at,
    )


def credential_epoch_rotated(*, occurred_at: datetime, epoch: int) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.CREDENTIAL_EPOCH_ROTATED,
        actor_subject=SYSTEM_ACTOR_SUBJECT,
        actor_role=AuditActorRole.SYSTEM,
        occurred_at=occurred_at,
        result_code="rotated",
        reason=f"authentication epoch advanced to {int(epoch)}",
    )


def authorization_allowed(*, actor_subject: str, occurred_at: datetime, decision) -> AuditEventDraft:
    identity = decision.action_identity
    return AuditEventDraft(
        event_type=AuditEventType.AUTHORIZATION_ALLOWED,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.REQUESTER,
        occurred_at=occurred_at,
        action_id=identity.action_id if identity else None,
        plan_id=identity.plan_id if identity else None,
        plan_revision=identity.target_revision if identity else None,
        plan_digest=identity.plan_digest if identity else None,
        target_id=decision.target_id,
        environment=decision.environment,
        operation=decision.operation.value if decision.operation else None,
        decision_id=decision.decision_id,
        policy_version=decision.policy_version,
        result_code=decision.code.value,
    )


def authorization_denied(*, actor_subject: str | None, occurred_at: datetime, decision) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.AUTHORIZATION_DENIED,
        actor_subject=actor_subject if actor_subject else UNAUTHENTICATED_ACTOR_SUBJECT,
        actor_role=AuditActorRole.REQUESTER if actor_subject else AuditActorRole.SYSTEM,
        occurred_at=occurred_at,
        target_id=decision.target_id,
        environment=decision.environment,
        operation=decision.operation.value if decision.operation else None,
        decision_id=decision.decision_id,
        policy_version=decision.policy_version,
        result_code=decision.code.value,
    )


def action_created(*, actor_subject: str, occurred_at: datetime, decision, lifecycle_from: str, lifecycle_to: str) -> AuditEventDraft:
    identity = decision.action_identity
    return AuditEventDraft(
        event_type=AuditEventType.ACTION_CREATED,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.REQUESTER,
        occurred_at=occurred_at,
        action_id=identity.action_id if identity else None,
        plan_id=identity.plan_id if identity else None,
        plan_revision=identity.target_revision if identity else None,
        plan_digest=identity.plan_digest if identity else None,
        target_id=decision.target_id,
        environment=decision.environment,
        operation=decision.operation.value if decision.operation else None,
        decision_id=decision.decision_id,
        policy_version=decision.policy_version,
        lifecycle_from=lifecycle_from,
        lifecycle_to=lifecycle_to,
        result_code="created",
    )


def action_idempotency_replay(*, actor_subject: str, occurred_at: datetime, action_id: str, decision_id: str) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.ACTION_IDEMPOTENCY_REPLAY,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.REQUESTER,
        occurred_at=occurred_at,
        action_id=action_id,
        decision_id=decision_id,
        result_code="replayed",
    )


def action_idempotency_conflict(*, actor_subject: str, occurred_at: datetime) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.ACTION_IDEMPOTENCY_CONFLICT,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.REQUESTER,
        occurred_at=occurred_at,
        result_code="idempotency_conflict",
    )


def lifecycle_transition(*, actor_subject: str, occurred_at: datetime, decision, from_state: str, to_state: str) -> AuditEventDraft:
    identity = decision.action_identity
    return AuditEventDraft(
        event_type=AuditEventType.LIFECYCLE_TRANSITION,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.SYSTEM,
        occurred_at=occurred_at,
        action_id=identity.action_id if identity else None,
        plan_id=identity.plan_id if identity else None,
        plan_revision=identity.target_revision if identity else None,
        plan_digest=identity.plan_digest if identity else None,
        target_id=decision.target_id,
        environment=decision.environment,
        operation=decision.operation.value if decision.operation else None,
        decision_id=decision.decision_id,
        policy_version=decision.policy_version,
        lifecycle_from=from_state,
        lifecycle_to=to_state,
        result_code="advanced",
    )


def lifecycle_transition_confirmed(*, actor_subject: str, occurred_at: datetime, binding, from_state: str, to_state: str) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.LIFECYCLE_TRANSITION,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.CONFIRMER,
        occurred_at=occurred_at,
        action_id=binding.action_id,
        plan_id=binding.plan_id,
        plan_revision=binding.target_revision,
        plan_digest=binding.plan_digest,
        target_id=binding.request.target_id,
        environment=binding.request.environment,
        operation=binding.request.operation.value,
        decision_id=binding.decision_id,
        confirmation_id=binding.confirmation_id,
        policy_version=binding.policy_version,
        lifecycle_from=from_state,
        lifecycle_to=to_state,
        result_code="advanced",
    )


def owner_confirmation_requested(*, actor_subject: str, occurred_at: datetime, binding) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.OWNER_CONFIRMATION_REQUESTED,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.REQUESTER,
        occurred_at=occurred_at,
        action_id=binding.action_id,
        plan_id=binding.plan_id,
        plan_revision=binding.target_revision,
        plan_digest=binding.plan_digest,
        target_id=binding.request.target_id,
        environment=binding.request.environment,
        operation=binding.request.operation.value,
        decision_id=binding.decision_id,
        confirmation_id=binding.confirmation_id,
        policy_version=binding.policy_version,
        result_code="requested",
    )


def owner_confirmed(*, actor_subject: str, occurred_at: datetime, binding) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.OWNER_CONFIRMED,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.CONFIRMER,
        occurred_at=occurred_at,
        action_id=binding.action_id,
        plan_id=binding.plan_id,
        plan_revision=binding.target_revision,
        plan_digest=binding.plan_digest,
        target_id=binding.request.target_id,
        environment=binding.request.environment,
        operation=binding.request.operation.value,
        decision_id=binding.decision_id,
        confirmation_id=binding.confirmation_id,
        policy_version=binding.policy_version,
        result_code="confirmed",
    )


def owner_confirmation_rejected(*, actor_subject: str, occurred_at: datetime, decision, reason_code: str) -> AuditEventDraft:
    identity = decision.action_identity
    return AuditEventDraft(
        event_type=AuditEventType.OWNER_CONFIRMATION_REJECTED,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.CONFIRMER,
        occurred_at=occurred_at,
        action_id=identity.action_id if identity else None,
        decision_id=decision.decision_id,
        confirmation_id=None,
        policy_version=decision.policy_version,
        result_code=reason_code,
    )


def owner_confirmation_consumed(*, actor_subject: str, occurred_at: datetime, binding) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.OWNER_CONFIRMATION_CONSUMED,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.SYSTEM,
        occurred_at=occurred_at,
        action_id=binding.action_id,
        decision_id=binding.decision_id,
        confirmation_id=binding.confirmation_id,
        policy_version=binding.policy_version,
        result_code="consumed",
    )


def verification_started(*, actor_subject: str, occurred_at: datetime, action_id: str, plan_id: str | None, plan_revision: int | None, plan_digest: str | None, target_id: str | None, environment: str | None, policy_version: str | None = None) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.VERIFICATION_STARTED,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.VERIFIER,
        occurred_at=occurred_at,
        action_id=action_id,
        plan_id=plan_id,
        plan_revision=plan_revision,
        plan_digest=plan_digest,
        target_id=target_id,
        environment=environment,
        policy_version=policy_version,
        result_code="started",
    )


def verification_finished(*, actor_subject: str, occurred_at: datetime, action_id: str, plan_id: str | None, plan_revision: int | None, plan_digest: str | None, target_id: str | None, environment: str | None, verification_id: str, success: bool, reason_code: str, verification_version: str) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.VERIFICATION_SUCCEEDED if success else AuditEventType.VERIFICATION_FAILED,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.VERIFIER,
        occurred_at=occurred_at,
        action_id=action_id,
        plan_id=plan_id,
        plan_revision=plan_revision,
        plan_digest=plan_digest,
        target_id=target_id,
        environment=environment,
        parent_event_id=verification_id,
        result_code=reason_code,
        reason=f"verification contract {verification_version}",
    )


def rollback_requested(*, actor_subject: str, occurred_at: datetime, action_id: str, snapshot_id: str, plan_id: str | None, plan_revision: int | None, plan_digest: str | None, target_id: str, environment: str, decision_id: str, policy_version: str) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.ROLLBACK_REQUESTED,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.REQUESTER,
        occurred_at=occurred_at,
        action_id=action_id,
        plan_id=plan_id,
        plan_revision=plan_revision,
        plan_digest=plan_digest,
        target_id=target_id,
        environment=environment,
        decision_id=decision_id,
        parent_event_id=snapshot_id,
        policy_version=policy_version,
        result_code="requested",
    )


def lease_acquired(*, actor_subject: str, occurred_at: datetime, action_id: str, plan_id: str | None, target_id: str, environment: str, lease_id: str, fencing_token: int) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.LEASE_ACQUIRED,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.SYSTEM,
        occurred_at=occurred_at,
        action_id=action_id,
        plan_id=plan_id,
        target_id=target_id,
        environment=environment,
        parent_event_id=lease_id,
        result_code=f"fence_{int(fencing_token)}",
    )


def execution_started(*, actor_subject: str, occurred_at: datetime, action_id: str, plan_id: str | None, target_id: str, environment: str, lease_id: str, fencing_token: int) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.EXECUTION_STARTED,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.EXECUTOR,
        occurred_at=occurred_at,
        action_id=action_id,
        plan_id=plan_id,
        target_id=target_id,
        environment=environment,
        parent_event_id=lease_id,
        result_code=f"fence_{int(fencing_token)}",
    )


def execution_finished(*, actor_subject: str, occurred_at: datetime, action_id: str, plan_id: str | None, target_id: str, environment: str, success: bool) -> AuditEventDraft:
    return AuditEventDraft(
        event_type=AuditEventType.EXECUTION_SUCCEEDED if success else AuditEventType.EXECUTION_FAILED,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.EXECUTOR,
        occurred_at=occurred_at,
        action_id=action_id,
        plan_id=plan_id,
        target_id=target_id,
        environment=environment,
        result_code="succeeded" if success else "failed",
    )


def rollback_finished(*, actor_subject: str, occurred_at: datetime, action_id: str, plan_id: str | None, target_id: str, environment: str, success: bool, contract_digest: str | None = None) -> AuditEventDraft:
    code = "restored" if success else "restore_failed"
    if contract_digest:
        code += ":cd=" + contract_digest[:16]
    return AuditEventDraft(
        event_type=AuditEventType.ROLLBACK_SUCCEEDED if success else AuditEventType.ROLLBACK_FAILED,
        actor_subject=actor_subject,
        actor_role=AuditActorRole.EXECUTOR,
        occurred_at=occurred_at,
        action_id=action_id,
        plan_id=plan_id,
        target_id=target_id,
        environment=environment,
        result_code=code,
    )


def kill_switch_changed(*, actor_subject: str, occurred_at: datetime, environment: str, from_state: str, to_state: str, epoch: int, reason: str) -> AuditEventDraft:
    event_type = {
        ("engaged", "engaged"): AuditEventType.KILL_SWITCH_ENGAGED,
        ("engaged", "disengaged"): AuditEventType.KILL_SWITCH_DISENGAGED,
        ("disengaged", "engaged"): AuditEventType.KILL_SWITCH_ENGAGED,
        ("disengaged", "disengaged"): AuditEventType.KILL_SWITCH_DISENGAGED,
        ("disengaged", "permanent"): AuditEventType.KILL_SWITCH_PERMANENT,
        ("engaged", "permanent"): AuditEventType.KILL_SWITCH_PERMANENT,
    }.get((from_state, to_state), AuditEventType.KILL_SWITCH_ENGAGED if to_state == "engaged" else AuditEventType.KILL_SWITCH_DISENGAGED)
    return AuditEventDraft(
        event_type=event_type,
        actor_subject=actor_subject if actor_subject else SYSTEM_ACTOR_SUBJECT,
        actor_role=AuditActorRole.KILL_SWITCH_OPERATOR if actor_subject else AuditActorRole.SYSTEM,
        occurred_at=occurred_at,
        environment=environment,
        lifecycle_from=from_state,
        lifecycle_to=to_state,
        result_code=f"epoch_{int(epoch)}",
        reason=reason if reason else None,
    )
