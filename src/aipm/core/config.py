import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from aipm.core.exceptions import AIPMError
from aipm.models.config import AIPMConfig, DiscoveryConfig, EventConfig, LoggingConfig, TelemetryConfig


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
            if not isinstance(logging_data, dict) or not isinstance(discovery_data, dict) or not isinstance(telemetry_data, dict) or not isinstance(events_data, dict):
                raise ValueError("logging, discovery, telemetry, and events must be mappings")

            env_database_path = os.environ.get("AIPM_TELEMETRY_DB")
            if env_database_path:
                telemetry_data["database_path"] = env_database_path

            logging_config = LoggingConfig(**logging_data)
            discovery_config = DiscoveryConfig(**discovery_data)
            telemetry_config = TelemetryConfig(**telemetry_data)
            event_config = EventConfig(**events_data)
            self._validate(logging_config, discovery_config, telemetry_config, event_config)
            return AIPMConfig(logging=logging_config, discovery=discovery_config, telemetry=telemetry_config, events=event_config)
        except (TypeError, ValueError, yaml.YAMLError) as exc:
            raise AIPMError(f"Failed to load configuration from {self.config_path}: {exc}") from exc
        except OSError as exc:
            raise AIPMError(f"Unable to read configuration from {self.config_path}: {exc}") from exc

    @staticmethod
    def _validate(logging_config: LoggingConfig, discovery_config: DiscoveryConfig, telemetry_config: TelemetryConfig, event_config: EventConfig) -> None:
        if logging_config.max_size_mb <= 0:
            raise ValueError("logging.max_size_mb must be greater than zero")
        if logging_config.backup_count < 0:
            raise ValueError("logging.backup_count cannot be negative")
        if discovery_config.max_depth < 0:
            raise ValueError("discovery.max_depth cannot be negative")
        if not discovery_config.search_paths:
            raise ValueError("discovery.search_paths cannot be empty")
        if telemetry_config.interval_seconds <= 0:
            raise ValueError("telemetry.interval_seconds must be greater than zero")
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

    def _create_default(self) -> AIPMConfig:
        default_config = AIPMConfig()
        env_database_path = os.environ.get("AIPM_TELEMETRY_DB")
        if env_database_path:
            default_config.telemetry.database_path = env_database_path
        self._validate(default_config.logging, default_config.discovery, default_config.telemetry, default_config.events)
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with self.config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(asdict(default_config), handle, sort_keys=False)
        except OSError as exc:
            raise AIPMError(f"Unable to create configuration at {self.config_path}: {exc}") from exc
        return default_config
