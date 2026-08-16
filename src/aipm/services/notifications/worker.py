from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from aipm.models.notifications import DeliveryStatus, NotificationChannel, NotificationPolicy
from aipm.repositories.notifications.sqlite import SQLiteNotificationRepository, identity_key
from aipm.services.notifications.channels import ChannelRegistry
from aipm.services.notifications.policy import evaluate


class NotificationProjector:
    def __init__(self, repository: SQLiteNotificationRepository, policies: tuple[NotificationPolicy, ...], channels: tuple[NotificationChannel, ...], *, global_window_seconds: int = 0, global_max_notifications: int = 0, logger: Any | None = None):
        self.repository = repository
        self.policies = policies
        self.channels = {channel.id: channel for channel in channels}
        self.global_window_seconds = global_window_seconds
        self.global_max_notifications = global_max_notifications
        self.logger = logger

    def project_once(self, limit: int = 100) -> int:
        count = 0
        for transition in self.repository.get_unprojected_transitions(limit):
            for policy in self.policies:
                for channel_id in policy.channels:
                    channel = self.channels.get(channel_id)
                    if channel is None:
                        continue
                    decision = evaluate(policy, channel_id, transition)
                    if decision.matched and channel.enabled:
                        title = f"[{transition.current_severity.value.upper()}] Incident {transition.transition.value}"
                        body = f"Incident {transition.incident_id} on {transition.resource.identifier}: {transition.current_status.value}"
                        notification_id, reason = self.repository.create_decision(
                            identity_key_value=decision.identity_key,
                            transition=transition,
                            policy_id=policy.id,
                            channel_id=channel_id,
                            title=title,
                            body=body,
                            cooldown_seconds=policy.cooldown_seconds,
                            window_seconds=policy.window_seconds,
                            max_notifications=policy.max_notifications,
                            global_window_seconds=self.global_window_seconds,
                            global_max_notifications=self.global_max_notifications,
                        )
                        if self.logger is not None:
                            if notification_id:
                                self.logger.info("Notification projected policy=%s channel=%s incident=%s notification=%s", policy.id, channel_id, transition.incident_id, notification_id)
                            elif reason:
                                self.logger.info("Notification suppressed policy=%s channel=%s incident=%s reason=%s", policy.id, channel_id, transition.incident_id, reason)
                    else:
                        reason = decision.reason or ("channel_disabled" if not channel.enabled else "channel_unavailable")
                        self.repository.record_suppression(identity_key_value=decision.identity_key, transition=transition, policy_id=policy.id, channel_id=channel_id, reason=reason)
                        if self.logger is not None:
                            self.logger.info("Notification suppressed policy=%s channel=%s incident=%s reason=%s", policy.id, channel_id, transition.incident_id, reason)
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
            self.repository.finish_delivery(delivery_id, DeliveryStatus.FAILED, retryable=False, max_attempts=1, lease_token=notification.lease_token, error_code="channel_disabled", error_message="Notification channel is disabled or unavailable")
            self._log("Notification terminal failure notification=%s channel=%s reason=channel_disabled", notification.id, notification.channel_id)
            return True
        try:
            result = self.registry.adapter_for(channel).send(notification, self.registry.context(channel, self._provider_key(notification.id, channel.id)))
        except Exception:
            result = DeliveryStatus.FAILED, True, None, None, "adapter_exception", "Adapter raised an exception"
            from types import SimpleNamespace
            result = SimpleNamespace(status=result[0], retryable=result[1], provider_message_id=result[2], provider_status_code=result[3], error_code=result[4], error_message=result[5])
        next_attempt = None
        if result.retryable and notification.attempt_count < channel.max_attempts:
            next_attempt = datetime.now(timezone.utc) + timedelta(seconds=min(3600, 30 * (2 ** max(0, notification.attempt_count - 1))))
        self.repository.finish_delivery(delivery_id, result.status, retryable=result.retryable, max_attempts=channel.max_attempts, lease_token=notification.lease_token, provider_message_id=result.provider_message_id, provider_status_code=result.provider_status_code, error_code=result.error_code, error_message=result.error_message, next_attempt_at=next_attempt)
        if result.status is DeliveryStatus.UNKNOWN:
            self._log("Notification UNKNOWN notification=%s channel=%s code=%s", notification.id, notification.channel_id, result.error_code)
        elif result.status is DeliveryStatus.SENT:
            self._log("Notification delivered notification=%s channel=%s attempt=%s", notification.id, notification.channel_id, notification.attempt_count)
        elif result.retryable and notification.attempt_count < channel.max_attempts:
            self._log("Notification retry scheduled notification=%s channel=%s attempt=%s", notification.id, notification.channel_id, notification.attempt_count)
        else:
            self._log("Notification terminal failure notification=%s channel=%s attempt=%s", notification.id, notification.channel_id, notification.attempt_count)
        return True

    @staticmethod
    def _provider_key(notification_id: int | None, channel_id: str) -> str:
        from aipm.repositories.notifications.sqlite import provider_key
        return provider_key(notification_id or 0, channel_id)

    def _log(self, message: str, *args: object) -> None:
        if self.logger is not None:
            self.logger.info(message, *args)
