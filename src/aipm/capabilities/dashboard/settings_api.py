from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from aipm.capabilities.dashboard.service_health_api import DashboardServiceHealthApi
from aipm.core.app import Application
from aipm.mappers.settings import SettingsResponseMapper
from aipm.models.settings import (
    ApplicationPosture,
    CapabilityPosture,
    DatabasePosture,
    DeploymentPosture,
    NotificationAuditAvailability,
    NotificationAuditMetrics,
    NotificationPosture,
    NotificationProviderState,
    PostureState,
    SettingsPosture,
    TelemetryPosture,
    bounded_count,
    bounded_interval,
    bounded_latency,
    bounded_optional_age,
)
from aipm.repositories.notifications.sqlite import SQLiteNotificationRepository
from aipm.version import VERSION


class DashboardSettingsApi:
    """Read-only effective Settings & Notification Posture façade."""

    _CAPABILITIES = (
        "telemetry",
        "mc3",
        "server",
        "docker",
        "projects",
        "systemd",
        "logs",
        "incidents",
        "history",
    )

    def __init__(
        self,
        application: Application,
        repository: SQLiteNotificationRepository | None,
        *,
        service_health_api: DashboardServiceHealthApi | None = None,
        mapper: SettingsResponseMapper | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.application = application
        self.repository = repository
        self.service_health_api = service_health_api
        self.mapper = mapper or SettingsResponseMapper()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def from_application(
        cls,
        application: Application,
        *,
        service_health_api: DashboardServiceHealthApi | None = None,
    ) -> "DashboardSettingsApi":
        repository: SQLiteNotificationRepository | None = None
        try:
            repository = SQLiteNotificationRepository(
                application.config.telemetry.database_path,
                read_only=True,
            )
        except Exception as exc:
            logger = getattr(application, "logger", None)
            if logger is not None:
                logger.exception("Settings notification posture unavailable", exc_info=exc)
        return cls(application, repository, service_health_api=service_health_api)

    def posture(self) -> dict[str, Any]:
        now = self._now()
        try:
            config = self.application.config
            notification_config = config.notifications
            audit = self._audit(now)
            provider_state = (
                NotificationProviderState.DISABLED
                if not notification_config.enabled
                else NotificationProviderState.NOT_INSTANTIATED
            )
            notification = NotificationPosture(
                enabled=bool(notification_config.enabled),
                provider_state=provider_state,
                configured_channel_count=bounded_count(len(notification_config.channels)),
                enabled_channel_count=bounded_count(
                    sum(1 for item in notification_config.channels if bool(item.enabled))
                ),
                configured_policy_count=bounded_count(len(notification_config.policies)),
                enabled_policy_count=bounded_count(
                    sum(1 for item in notification_config.policies if bool(item.enabled))
                ),
                audit=audit,
            )
            telemetry, mc3 = self._service_posture(config)
            posture = SettingsPosture(
                available=True,
                status=PostureState.OK,
                error=None,
                generated_at=now.isoformat(),
                application=ApplicationPosture(VERSION, None, PostureState.OK),
                deployment=DeploymentPosture(
                    "loopback_only_required",
                    "not_observed",
                    "not_observed",
                ),
                database=DatabasePosture(
                    "read_only",
                    True,
                    "required",
                    "prohibited",
                    "prohibited",
                ),
                telemetry=telemetry,
                mc3=mc3,
                notifications=notification,
                capabilities=self._capabilities(telemetry, mc3),
            )
            return self.mapper.to_response(posture)
        except (TypeError, ValueError):
            return self.mapper.to_response(
                SettingsPosture.unavailable(
                    generated_at=now.isoformat(),
                    error="Invalid settings posture",
                )
            )
        except Exception as exc:
            self._log("Settings posture failed", exc)
            return self.mapper.to_response(
                SettingsPosture.unavailable(generated_at=now.isoformat())
            )

    def _audit(self, now: datetime) -> NotificationAuditMetrics:
        if self.repository is None:
            return self._unavailable_audit()
        try:
            metrics = self.repository.metrics(now=now)
            return NotificationAuditMetrics(
                availability=NotificationAuditAvailability.OBSERVED,
                schema_version=bounded_count(self.repository.schema_version(), maximum=100),
                pending=bounded_count(metrics.get("pending")),
                sending=bounded_count(metrics.get("sending")),
                sent=bounded_count(metrics.get("sent")),
                failed=bounded_count(metrics.get("failed")),
                unknown=bounded_count(metrics.get("unknown")),
                suppressed=bounded_count(metrics.get("suppressed")),
                retry_exhaustion_count=bounded_count(metrics.get("retry_exhaustion_count")),
                recent_delivery_latency_seconds=bounded_latency(
                    metrics.get("recent_delivery_latency_seconds")
                ),
                oldest_pending_age_seconds=bounded_optional_age(
                    metrics.get("oldest_pending_age_seconds")
                ),
                oldest_unknown_age_seconds=bounded_optional_age(
                    metrics.get("oldest_unknown_age_seconds")
                ),
                lease_expiry_count=bounded_count(metrics.get("lease_expiry_count")),
            )
        except Exception as exc:
            self._log("Settings notification audit unavailable", exc)
            return self._unavailable_audit()

    @staticmethod
    def _unavailable_audit() -> NotificationAuditMetrics:
        return NotificationAuditMetrics(
            availability=NotificationAuditAvailability.UNAVAILABLE,
            schema_version=None,
            pending=None,
            sending=None,
            sent=None,
            failed=None,
            unknown=None,
            suppressed=None,
            retry_exhaustion_count=None,
            recent_delivery_latency_seconds=None,
            oldest_pending_age_seconds=None,
            oldest_unknown_age_seconds=None,
            lease_expiry_count=None,
        )

    def _service_posture(self, config: Any) -> tuple[TelemetryPosture, TelemetryPosture]:
        telemetry_state = PostureState.NOT_OBSERVED
        mc3_state = PostureState.NOT_OBSERVED
        if self.service_health_api is not None:
            try:
                health = self.service_health_api.services()
                services = health.get("services", {}) if isinstance(health, dict) else {}
                telemetry_state = self._state(services.get("telemetry", {}).get("state"))
                mc3_state = self._state(services.get("mc3", {}).get("state"))
            except Exception as exc:
                self._log("Settings service posture unavailable", exc)
        return (
            TelemetryPosture(
                bool(config.telemetry.enabled),
                bounded_interval(config.telemetry.interval_seconds),
                telemetry_state,
            ),
            TelemetryPosture(
                bool(config.events.enabled),
                bounded_interval(config.events.interval_seconds),
                mc3_state,
            ),
        )

    def _capabilities(
        self,
        telemetry: TelemetryPosture,
        mc3: TelemetryPosture,
    ) -> tuple[CapabilityPosture, ...]:
        states = {"telemetry": telemetry, "mc3": mc3}
        return tuple(
                CapabilityPosture(
                name,
                states[name].state if name in states else PostureState.NOT_OBSERVED,
                name in states and states[name].state not in {PostureState.NOT_OBSERVED, PostureState.UNAVAILABLE},
            )
            for name in self._CAPABILITIES
        )

    @staticmethod
    def _state(value: Any) -> PostureState:
        try:
            return PostureState(str(value))
        except ValueError:
            return PostureState.UNKNOWN

    def _now(self) -> datetime:
        now = self.clock()
        return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)

    def _log(self, message: str, exc: Exception) -> None:
        logger = getattr(self.application, "logger", None)
        if logger is not None:
            logger.exception(message, exc_info=exc)
