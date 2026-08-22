import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from aipm.core.exceptions import AIPMError
from aipm.models.events import EventType, ResourceType
from aipm.models.finding import Severity
from aipm.models.notifications import NotificationTrigger
from aipm.models.config import (
    AIPMConfig,
    DiscoveryConfig,
    EventConfig,
    LoggingConfig,
    NotificationChannelConfig,
    NotificationConfig,
    NotificationPolicyConfig,
    TelemetryConfig,
)


class ConfigManager:
    """Load, validate, and create the user configuration for AIPM."""

    def __init__(self, config_path: Path | None = None):
        configured_path = os.environ.get("AIPM_CONFIG")
        self.config_path = config_path or (
            Path(configured_path).expanduser()
            if configured_path
            else Path.home() / ".config" / "aipm" / "config.yaml"
        )
        self.config = self._load_or_create()

    def _load_or_create(self) -> AIPMConfig:
        if not self.config_path.exists():
            return self._create_default()

        try:
            with self.config_path.open("r", encoding="utf-8") as handle:
                data: Any = yaml.safe_load(handle) or {}
            if not isinstance(data, dict):
                raise ValueError("the root value must be a mapping")

            logging_data = data.get("logging", {})
            discovery_data = data.get("discovery", {})
            telemetry_data = dict(data.get("telemetry", {}) or {})
            events_data = dict(data.get("events", {}) or {})
            notifications_data = dict(data.get("notifications", {}) or {})
            if not isinstance(logging_data, dict) or not isinstance(discovery_data, dict) or not isinstance(telemetry_data, dict) or not isinstance(events_data, dict) or not isinstance(notifications_data, dict):
                raise ValueError("logging, discovery, telemetry, events, and notifications must be mappings")

            env_database_path = os.environ.get("AIPM_TELEMETRY_DB")
            if env_database_path:
                telemetry_data["database_path"] = env_database_path

            channel_data = notifications_data.pop("channels", []) or []
            policy_data = notifications_data.pop("policies", []) or []
            if not isinstance(channel_data, list) or not isinstance(policy_data, list):
                raise ValueError("notifications.channels and notifications.policies must be lists")
            notification_config = NotificationConfig(
                **notifications_data,
                channels=[NotificationChannelConfig(**item) for item in channel_data],
                policies=[NotificationPolicyConfig(**item) for item in policy_data],
            )
            logging_config = LoggingConfig(**logging_data)
            discovery_config = DiscoveryConfig(**discovery_data)
            telemetry_config = TelemetryConfig(**telemetry_data)
            event_config = EventConfig(**events_data)
            self._validate(logging_config, discovery_config, telemetry_config, event_config, notification_config)
            return AIPMConfig(logging=logging_config, discovery=discovery_config, telemetry=telemetry_config, events=event_config, notifications=notification_config)
        except (TypeError, ValueError, yaml.YAMLError) as exc:
            raise AIPMError(f"Failed to load configuration from {self.config_path}: {exc}") from exc
        except OSError as exc:
            raise AIPMError(f"Unable to read configuration from {self.config_path}: {exc}") from exc

    @staticmethod
    def _validate(logging_config: LoggingConfig, discovery_config: DiscoveryConfig, telemetry_config: TelemetryConfig, event_config: EventConfig, notification_config: NotificationConfig | None = None) -> None:
        if logging_config.max_size_mb <= 0:
            raise ValueError("logging.max_size_mb must be greater than zero")
        if logging_config.backup_count < 0:
            raise ValueError("logging.backup_count cannot be negative")
        if discovery_config.max_depth < 0:
            raise ValueError("discovery.max_depth cannot be negative")
        if discovery_config.max_directories <= 0 or discovery_config.max_entries <= 0 or discovery_config.max_projects <= 0 or discovery_config.max_git_enrichments <= 0 or discovery_config.max_git_items <= 0:
            raise ValueError("discovery bounds must be greater than zero")
        if discovery_config.git_timeout_seconds <= 0:
            raise ValueError("discovery.git_timeout_seconds must be greater than zero")
        if not discovery_config.search_paths:
            raise ValueError("discovery.search_paths cannot be empty")
        home = Path.home().resolve()
        if any(Path(path).expanduser().resolve() == home for path in discovery_config.search_paths):
            raise ValueError("discovery.search_paths cannot be the entire home directory")
        if telemetry_config.interval_seconds <= 0:
            raise ValueError("telemetry.interval_seconds must be greater than zero")
        if telemetry_config.resource_interval_seconds <= 0 or telemetry_config.project_interval_seconds <= 0:
            raise ValueError("telemetry slow intervals must be greater than zero")
        if telemetry_config.resource_timeout_seconds <= 0 or telemetry_config.project_timeout_seconds <= 0:
            raise ValueError("telemetry slow timeouts must be greater than zero")
        if telemetry_config.resource_stale_after_seconds <= telemetry_config.resource_interval_seconds:
            raise ValueError("telemetry.resource_stale_after_seconds must exceed resource_interval_seconds")
        if telemetry_config.slow_task_max_concurrency != 1:
            raise ValueError("telemetry.slow_task_max_concurrency must be exactly 1")
        if telemetry_config.sampling_mode not in {"split", "legacy"}:
            raise ValueError("telemetry.sampling_mode must be split or legacy")
        if telemetry_config.retention_days <= 0:
            raise ValueError("telemetry.retention_days must be greater than zero")
        database_path = str(telemetry_config.database_path).strip()
        if not database_path:
            raise ValueError("telemetry.database_path cannot be empty")
        expanded = Path(database_path).expanduser()
        if expanded.name in {"", ".", ".."} or expanded == expanded.parent:
            raise ValueError("telemetry.database_path must point to a database file")
        if expanded.exists() and expanded.is_dir():
            raise ValueError("telemetry.database_path cannot be a directory")
        if event_config.interval_seconds <= 0:
            raise ValueError("events.interval_seconds must be greater than zero")
        if event_config.event_retention_days <= 0:
            raise ValueError("events.event_retention_days must be greater than zero")
        if event_config.incident_retention_days <= 0:
            raise ValueError("events.incident_retention_days must be greater than zero")
        if notification_config is not None:
            if notification_config.interval_seconds <= 0 or notification_config.retention_days <= 0:
                raise ValueError("notification intervals and retention must be greater than zero")
            if notification_config.default_cooldown_seconds < 0 or notification_config.default_window_seconds <= 0 or notification_config.default_max_notifications <= 0:
                raise ValueError("notification defaults are invalid")
            channel_ids = set()
            supported_channels = {"telegram", "webhook", "http"}
            for channel in notification_config.channels:
                if not channel.id or channel.id in channel_ids or channel.timeout_seconds <= 0 or channel.max_attempts <= 0:
                    raise ValueError("notification channel configuration is invalid")
                if channel.channel_type not in supported_channels:
                    raise ValueError(f"notifications channel type is unsupported: {channel.channel_type}")
                if channel.enabled and channel.destination_ref is None:
                    raise ValueError(f"enabled notification channel {channel.id} requires destination_ref")
                if channel.enabled and channel.destination_ref and not os.environ.get(channel.destination_ref):
                    raise ValueError(f"enabled notification channel {channel.id} destination is not configured")
                if channel.enabled and channel.channel_type == "telegram" and channel.secret_ref is None:
                    raise ValueError(f"enabled Telegram channel {channel.id} requires secret_ref")
                if channel.enabled and channel.secret_ref and not os.environ.get(channel.secret_ref):
                    raise ValueError(f"enabled notification channel {channel.id} secret is not configured")
                if channel.secret_ref and ("=" in channel.secret_ref or " " in channel.secret_ref):
                    raise ValueError("notification secrets must be environment variable references")
                if channel.destination_ref and ("=" in channel.destination_ref or " " in channel.destination_ref):
                    raise ValueError("notification destinations must be environment variable references")
                channel_ids.add(channel.id)
            policy_ids = set()
            for policy in notification_config.policies:
                if not policy.id or policy.id in policy_ids or policy.cooldown_seconds < 0 or policy.window_seconds <= 0 or policy.max_notifications <= 0:
                    raise ValueError("notification policy configuration is invalid")
                policy_ids.add(policy.id)
                try:
                    Severity(policy.minimum_severity)
                    [EventType(value) for value in policy.event_types]
                    [ResourceType(value) for value in policy.resource_types]
                    [NotificationTrigger(value) for value in policy.transitions]
                except ValueError as exc:
                    raise ValueError(f"notification policy {policy.id} contains an unsupported enum value") from exc
                if any(channel_id not in channel_ids for channel_id in policy.channels):
                    raise ValueError(f"notification policy {policy.id} references an unknown channel")
                if policy.enabled and not policy.channels:
                    raise ValueError(f"enabled notification policy {policy.id} must select a channel")

    def _create_default(self) -> AIPMConfig:
        default_config = AIPMConfig()
        env_database_path = os.environ.get("AIPM_TELEMETRY_DB")
        if env_database_path:
            default_config.telemetry.database_path = env_database_path
        self._validate(default_config.logging, default_config.discovery, default_config.telemetry, default_config.events, default_config.notifications)
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with self.config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(asdict(default_config), handle, sort_keys=False)
        except OSError as exc:
            raise AIPMError(f"Unable to create configuration at {self.config_path}: {exc}") from exc
        return default_config
