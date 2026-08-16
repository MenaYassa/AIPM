from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from aipm.models.notifications import DeliveryStatus, NotificationChannel, NotificationPolicy, NotificationStatus
from aipm.repositories.notifications.sqlite import SQLiteNotificationRepository, provider_key
from aipm.services.notifications.channels import ChannelRegistry
from aipm.services.notifications.policy import evaluate


class NotificationProjector:
    def __init__(self, repository: SQLiteNotificationRepository, policies: tuple[NotificationPolicy, ...], channels: tuple[NotificationChannel, ...], *, logger: Any | None = None):
        self.repository = repository
        self.policies = policies
        self.channels = {channel.id: channel for channel in channels}
        self.logger = logger

    def project_once(self, limit: int = 100) -> int:
        count = 0
        for transition in self.repository.get_unprojected_transitions(limit):
            for policy in self.policies:
                for channel_id in policy.channels:
                    if channel_id not in self.channels:
                        continue
                    decision = evaluate(policy, channel_id, transition)
                    if decision.matched:
                        title = f"[{transition.current_severity.value.upper()}] Incident {transition.transition.value}"
                        body = f"Incident {transition.incident_id} on {transition.resource.identifier}: {transition.current_status.value}"
                        notification_id = self.repository.create_notification(identity_key=decision.identity_key, transition=transition, policy_id=policy.id, channel_id=channel_id, status=NotificationStatus.PENDING, title=title, body=body)
                        self.repository.create_delivery(notification_id, channel_id, provider_key(notification_id, channel_id))
                    elif decision.suppressed:
                        self.repository.create_notification(identity_key=decision.identity_key, transition=transition, policy_id=policy.id, channel_id=channel_id, status=NotificationStatus.SUPPRESSED, title="Notification suppressed", body=decision.reason or "suppressed", suppressed_reason=decision.reason)
            if transition.id is not None:
                self.repository.mark_projected(transition.id)
            count += 1
        return count


class NotificationWorker:
    def __init__(self, repository: SQLiteNotificationRepository, registry: ChannelRegistry, channels: tuple[NotificationChannel, ...], *, logger: Any | None = None, lease_seconds: int = 60):
        self.repository = repository
        self.registry = registry
        self.channels = {channel.id: channel for channel in channels}
        self.logger = logger
        self.lease_seconds = lease_seconds

    def deliver_once(self) -> bool:
        claimed = self.repository.claim_due(datetime.now(timezone.utc), self.lease_seconds)
        if claimed is None:
            return False
        delivery_id, notification = claimed
        channel = self.channels.get(notification.channel_id)
        if channel is None or not channel.enabled:
            self.repository.finish_delivery(delivery_id, DeliveryStatus.FAILED, retryable=False, error_code="channel_disabled", error_message="Notification channel is disabled or unavailable")
            return True
        try:
            result = self.registry.adapter_for(channel).send(notification, self.registry.context(channel, provider_key(notification.id or 0, channel.id)))
        except Exception as exc:
            result = type("Result", (), {"status": DeliveryStatus.FAILED, "retryable": True, "provider_message_id": None, "provider_status_code": None, "error_code": "adapter_exception", "error_message": str(exc)})()
        next_attempt = None
        if result.retryable:
            next_attempt = datetime.now(timezone.utc) + timedelta(seconds=min(3600, 30 * (2 ** max(0, notification.attempt_count - 1))))
        self.repository.finish_delivery(delivery_id, result.status, retryable=result.retryable, provider_message_id=result.provider_message_id, provider_status_code=result.provider_status_code, error_code=result.error_code, error_message=result.error_message, next_attempt_at=next_attempt)
        return True
