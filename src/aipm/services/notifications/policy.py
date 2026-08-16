from __future__ import annotations

from aipm.models.config import NotificationChannelConfig, NotificationPolicyConfig
from aipm.models.events import EventType, ResourceType
from aipm.models.finding import Severity
from aipm.models.notifications import IncidentTransition, NotificationChannel, NotificationPolicy, NotificationTrigger, PolicyDecision
from aipm.repositories.notifications.sqlite import identity_key


_SEVERITY_RANK = {Severity.INFO: 1, Severity.WARNING: 2, Severity.HIGH: 3, Severity.CRITICAL: 4}


def channel_from_config(config: NotificationChannelConfig) -> NotificationChannel:
    return NotificationChannel(config.id, config.name, config.channel_type, config.enabled, config.secret_ref, config.destination_ref, config.timeout_seconds, config.max_attempts)


def policy_from_config(config: NotificationPolicyConfig) -> NotificationPolicy:
    return NotificationPolicy(
        id=config.id,
        name=config.name,
        enabled=config.enabled,
        minimum_severity=Severity(config.minimum_severity),
        event_types=tuple(EventType(value) for value in config.event_types),
        resource_types=tuple(ResourceType(value) for value in config.resource_types),
        project_paths=tuple(config.project_paths),
        transitions=tuple(NotificationTrigger(value) for value in config.transitions),
        notify_recovery=config.notify_recovery,
        notify_acknowledgement=config.notify_acknowledgement,
        notify_updates=config.notify_updates,
        cooldown_seconds=config.cooldown_seconds,
        window_seconds=config.window_seconds,
        max_notifications=config.max_notifications,
        channels=tuple(config.channels),
    )


def evaluate(policy: NotificationPolicy, channel_id: str, transition: IncidentTransition) -> PolicyDecision:
    key = identity_key(policy.id, channel_id, transition)
    if not policy.enabled:
        return PolicyDecision(False, True, "policy_disabled", policy.id, channel_id, key)
    if channel_id not in policy.channels:
        return PolicyDecision(False, True, "channel_not_selected", policy.id, channel_id, key)
    if transition.transition not in policy.transitions:
        return PolicyDecision(False, True, "transition_not_selected", policy.id, channel_id, key)
    if transition.transition is NotificationTrigger.INCIDENT_RECOVERED and not policy.notify_recovery:
        return PolicyDecision(False, True, "recovery_disabled", policy.id, channel_id, key)
    if transition.transition is NotificationTrigger.INCIDENT_ACKNOWLEDGED and not policy.notify_acknowledgement:
        return PolicyDecision(False, True, "acknowledgement_disabled", policy.id, channel_id, key)
    if transition.transition is NotificationTrigger.INCIDENT_UPDATED and not policy.notify_updates:
        return PolicyDecision(False, True, "updates_disabled", policy.id, channel_id, key)
    if _SEVERITY_RANK[transition.current_severity] < _SEVERITY_RANK[policy.minimum_severity]:
        return PolicyDecision(False, True, "severity_below_threshold", policy.id, channel_id, key)
    if policy.event_types and transition.event_type not in policy.event_types:
        return PolicyDecision(False, True, "event_type_not_selected", policy.id, channel_id, key)
    if policy.resource_types and transition.resource.resource_type not in policy.resource_types:
        return PolicyDecision(False, True, "resource_type_not_selected", policy.id, channel_id, key)
    if policy.project_paths and transition.resource.project_path not in policy.project_paths:
        return PolicyDecision(False, True, "project_not_selected", policy.id, channel_id, key)
    return PolicyDecision(True, False, None, policy.id, channel_id, key)
