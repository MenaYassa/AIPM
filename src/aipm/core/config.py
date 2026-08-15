from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from aipm.core.exceptions import AIPMError
from aipm.models.config import AIPMConfig, DiscoveryConfig, LoggingConfig


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
            if not isinstance(logging_data, dict) or not isinstance(discovery_data, dict):
                raise ValueError("logging and discovery must be mappings")

            logging_config = LoggingConfig(**logging_data)
            discovery_config = DiscoveryConfig(**discovery_data)
            if logging_config.max_size_mb <= 0:
                raise ValueError("logging.max_size_mb must be greater than zero")
            if logging_config.backup_count < 0:
                raise ValueError("logging.backup_count cannot be negative")
            if discovery_config.max_depth < 0:
                raise ValueError("discovery.max_depth cannot be negative")
            if not discovery_config.search_paths:
                raise ValueError("discovery.search_paths cannot be empty")

            return AIPMConfig(logging=logging_config, discovery=discovery_config)
        except (TypeError, ValueError, yaml.YAMLError) as exc:
            raise AIPMError(f"Failed to load configuration from {self.config_path}: {exc}") from exc
        except OSError as exc:
            raise AIPMError(f"Unable to read configuration from {self.config_path}: {exc}") from exc

    def _create_default(self) -> AIPMConfig:
        default_config = AIPMConfig()
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with self.config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(asdict(default_config), handle, sort_keys=False)
        except OSError as exc:
            raise AIPMError(f"Unable to create configuration at {self.config_path}: {exc}") from exc
        return default_config
