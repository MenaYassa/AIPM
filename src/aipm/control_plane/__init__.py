"""Non-executing MC-6.12 control-plane foundation.

The package intentionally exposes only pure identity, policy, lifecycle, and
bounded audit contracts. It contains no executor, provider, persistence,
shell, network, lease, or runtime-mutation boundary.
"""

from aipm.control_plane.identity import IdentityError, PrincipalVerification, VerifiedPrincipal
from aipm.control_plane.lifecycle import advance, allowed_transitions, terminal_states, validate_transition
from aipm.control_plane.models import ActionLifecycle, ActionScope, ActorRole, LifecycleError, LifecycleState, Stage2AuditEvent
from aipm.control_plane.policy import AuthorizationPolicy, PolicyCode, PolicyDecision

__all__ = [
    "ActionLifecycle",
    "ActionScope",
    "ActorRole",
    "AuthorizationPolicy",
    "IdentityError",
    "LifecycleError",
    "LifecycleState",
    "PolicyCode",
    "PolicyDecision",
    "PrincipalVerification",
    "Stage2AuditEvent",
    "VerifiedPrincipal",
    "advance",
    "allowed_transitions",
    "terminal_states",
    "validate_transition",
]
