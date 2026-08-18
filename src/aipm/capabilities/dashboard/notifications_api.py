from __future__ import annotations

import os
from typing import Any

from aipm.core.app import Application
from aipm.models.notifications import NotificationFilter, NotificationStatus
from aipm.repositories.notifications.sqlite import SQLiteNotificationRepository
from aipm.services.notifications.policy import channel_from_config, policy_from_config


class DashboardNotificationsApi:
    def __init__(self, repository: SQLiteNotificationRepository | None, application: Application | None = None):
        self.repository = repository
        self.application = application

    @classmethod
    def from_application(cls, application: Application) -> "DashboardNotificationsApi":
        try:
            return cls(SQLiteNotificationRepository(application.config.telemetry.database_path, read_only=True), application)
        except Exception as exc:
            application.logger.exception("Notification repository unavailable", exc_info=exc)
            return cls(None, application)

    def notifications(self, **filters: Any) -> dict[str, Any]:
        if self.repository is None:
            return self._unavailable()
        try:
            status = NotificationStatus(filters["status"]) if filters.get("status") else None
            rows = self.repository.get_notifications(NotificationFilter(status=status, incident_id=filters.get("incident_id"), channel_id=filters.get("channel_id"), include_suppressed=bool(filters.get("include_suppressed", False)), limit=int(filters.get("limit", 100))))
            return {"available": True, "status": "ok", "error": None, "notifications": [self._notification(item) for item in rows]}
        except (ValueError, TypeError):
            return self._unavailable("Invalid notification query")
        except Exception as exc:
            self._log("Notification API failed", exc)
            return self._unavailable()

    def notification(self, notification_id: int) -> dict[str, Any]:
        if self.repository is None:
            return self._unavailable()
        try:
            item = self.repository.get_notification(notification_id)
            if item is None:
                return self._unavailable("Notification not found")
            return {"available": True, "status": "ok", "error": None, "notification": self._notification(item)}
        except Exception as exc:
            self._log("Notification detail API failed", exc)
            return self._unavailable()

    def channels(self) -> dict[str, Any]:
        if self.application is None:
            return self._unavailable()
        channels = []
        for item in (channel_from_config(config) for config in self.application.config.notifications.channels):
            secret_ready = not item.secret_ref or bool(os.environ.get(item.secret_ref))
            destination_ready = not item.destination_ref or bool(os.environ.get(item.destination_ref))
            channels.append({"id": item.id, "name": item.name, "type": item.channel_type, "enabled": item.enabled, "supported": item.channel_type in {"telegram", "webhook", "http"}, "configured": secret_ready and destination_ready})
        return {"available": True, "status": "ok", "error": None, "channels": channels}

    def metrics(self) -> dict[str, Any]:
        if self.repository is None:
            return self._unavailable()
        try:
            return {"available": True, "status": "ok", "error": None, "metrics": self.repository.metrics()}
        except Exception as exc:
            self._log("Notification metrics API failed", exc)
            return self._unavailable()

    def policies(self) -> dict[str, Any]:
        if self.application is None:
            return self._unavailable()
        return {"available": True, "status": "ok", "error": None, "policies": [{"id": item.id, "name": item.name, "enabled": item.enabled, "minimum_severity": item.minimum_severity.value, "transitions": [value.value for value in item.transitions], "channels": list(item.channels), "cooldown_seconds": item.cooldown_seconds, "max_notifications": item.max_notifications} for item in (policy_from_config(config) for config in self.application.config.notifications.policies)]}

    @staticmethod
    def _notification(item) -> dict[str, Any]:
        return {"id": item.id, "incident_id": item.incident_id, "event_id": item.event_id, "policy_id": item.policy_id, "channel_id": item.channel_id, "trigger": item.trigger.value, "status": item.status.value, "severity": item.severity.value, "resource": {"type": item.resource.resource_type.value, "id": item.resource.identifier, "name": item.resource.name, "project_path": item.resource.project_path}, "title": item.title, "body": item.body, "created_at": item.created_at.isoformat(), "next_attempt_at": item.next_attempt_at.isoformat() if item.next_attempt_at else None, "attempt_count": item.attempt_count, "suppressed_reason": item.suppressed_reason}

    @staticmethod
    def _unavailable(error: str = "Notification data unavailable") -> dict[str, Any]:
        return {"available": False, "status": "unavailable", "error": error, "notifications": [], "metrics": {}}

    def _log(self, message: str, exc: Exception) -> None:
        if self.application is not None:
            self.application.logger.exception(message, exc_info=exc)
