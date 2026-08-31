"""Non-executing MC-6.12 control-plane foundation.

The package exposes the canonical identity/authorization seam: one owner
principal, one authorization policy, one action identity derivation, one
confirmation service, and the composition service that binds them. It also
exposes the bounded session, lifecycle, audit, project-plan, action, and
kill-switch contracts with pluggable persistence (in-memory test doubles and
the dedicated durable control-plane store under ``control_plane.storage``).

It contains no executor, provider, shell, network, lease grantor, or
runtime-mutation boundary, and nothing here can change the external system.
The only persistence is the dedicated control-plane database; telemetry,
events, incidents, and notification databases are never touched.
"""

from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.identity import (
    ACTION_IDENTITY_VERSION,
    ActionIdentity,
    AuthenticationMethod,
    IdentityError,
    OwnerPrincipal,
    PLAN_IDENTITY_VERSION,
    PRINCIPAL_IDENTITY_VERSION,
    PrincipalVerification,
    derive_action_identity,
)
from aipm.control_plane.lifecycle import advance, allowed_transitions, implemented_states, terminal_states, validate_transition
from aipm.control_plane.audit import (
    AuditActorRole,
    AuditEvent,
    AuditEventDraft,
    AuditEventType,
    ChainVerificationResult,
    InMemoryAuditLedger,
    SQLiteAuditLedger,
)
from aipm.control_plane.models import (
    ActionLifecycle,
    ActionRequest,
    ActionScope,
    ActorRole,
    ConfirmationBinding,
    ConfirmationKind,
    ConfirmationState,
    LifecycleError,
    LifecycleState,
)
from aipm.control_plane.owner_auth import Argon2idVerifier, FailureReason, OwnerAuthenticator
from aipm.control_plane.policy import AuthorizationContext, AuthorizationDecision, AuthorizationPolicy, PolicyCode
from aipm.control_plane.service import OwnerControlPlaneService
from aipm.control_plane.session import OwnerSession, OwnerSessionStore, SessionCookiePolicy, SessionStore

__all__ = [
    "ACTION_IDENTITY_VERSION",
    "ActionIdentity",
    "ActionLifecycle",
    "ActionRequest",
    "ActionScope",
    "ActorRole",
    "AuditActorRole",
    "AuditEvent",
    "AuditEventDraft",
    "AuditEventType",
    "Argon2idVerifier",
    "AuthenticationMethod",
    "AuthorizationContext",
    "AuthorizationDecision",
    "AuthorizationPolicy",
    "ChainVerificationResult",
    "ConfirmationBinding",
    "ConfirmationKind",
    "ConfirmationState",
    "FailureReason",
    "IdentityError",
    "InMemoryAuditLedger",
    "LifecycleError",
    "LifecycleState",
    "OwnerAuthenticator",
    "OwnerConfirmationService",
    "OwnerControlPlaneService",
    "OwnerPrincipal",
    "OwnerSession",
    "OwnerSessionStore",
    "PLAN_IDENTITY_VERSION",
    "PRINCIPAL_IDENTITY_VERSION",
    "PolicyCode",
    "PrincipalVerification",
    "SQLiteAuditLedger",
    "SessionCookiePolicy",
    "SessionStore",
    "advance",
    "allowed_transitions",
    "derive_action_identity",
    "implemented_states",
    "terminal_states",
    "validate_transition",
]
