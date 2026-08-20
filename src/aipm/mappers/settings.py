from __future__ import annotations

from dataclasses import asdict
from typing import Any

from aipm.models.settings import SettingsPosture


class SettingsResponseMapper:
    """Allow-listed mapper for the Settings & Notification Posture response."""

    def to_response(self, posture: SettingsPosture) -> dict[str, Any]:
        application = posture.application
        deployment = posture.deployment
        database = posture.database
        telemetry = posture.telemetry
        mc3 = posture.mc3
        notifications = posture.notifications
        audit = notifications.audit
        return {
            "available": posture.available,
            "status": posture.status.value,
            "error": posture.error,
            "generated_at": posture.generated_at,
            "application": {
                "version": application.version,
                "commit": application.commit,
                "state": application.state.value,
            },
            "deployment": {
                "binding": deployment.binding,
                "public_ingress": deployment.public_ingress,
                "permanent_service": deployment.permanent_service,
            },
            "read_only": {
                "sqlite_mode": database.sqlite_mode,
                "query_only": database.query_only,
                "filesystem_write_boundary": database.filesystem_write_boundary,
                "schema_mutation": database.schema_mutation,
                "checkpointing": database.checkpointing,
            },
            "telemetry": {
                "enabled": telemetry.enabled,
                "interval_seconds": telemetry.interval_seconds,
                "state": telemetry.state.value,
            },
            "mc3": {
                "enabled": mc3.enabled,
                "interval_seconds": mc3.interval_seconds,
                "state": mc3.state.value,
            },
            "notifications": {
                "enabled": notifications.enabled,
                "provider_state": notifications.provider_state.value,
                "configured_channel_count": notifications.configured_channel_count,
                "enabled_channel_count": notifications.enabled_channel_count,
                "configured_policy_count": notifications.configured_policy_count,
                "enabled_policy_count": notifications.enabled_policy_count,
                "audit": {
                    "availability": audit.availability.value,
                    "schema_version": audit.schema_version,
                    "pending": audit.pending,
                    "sending": audit.sending,
                    "sent": audit.sent,
                    "failed": audit.failed,
                    "unknown": audit.unknown,
                    "suppressed": audit.suppressed,
                    "retry_exhaustion_count": audit.retry_exhaustion_count,
                    "recent_delivery_latency_seconds": audit.recent_delivery_latency_seconds,
                    "oldest_pending_age_seconds": audit.oldest_pending_age_seconds,
                    "oldest_unknown_age_seconds": audit.oldest_unknown_age_seconds,
                    "lease_expiry_count": audit.lease_expiry_count,
                },
            },
            "capabilities": [
                {"name": item.name, "state": item.state.value, "available": item.available}
                for item in posture.capabilities
            ],
        }

    def unavailable(self, error: str = "Settings posture unavailable") -> dict[str, Any]:
        from datetime import datetime, timezone

        return self.to_response(
            SettingsPosture.unavailable(
                generated_at=datetime.now(timezone.utc).isoformat(),
                error=error if error in {"Settings posture unavailable", "Invalid settings posture"} else "Settings posture unavailable",
            )
        )
