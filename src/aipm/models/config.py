from dataclasses import dataclass, field
from pathlib import Path

from aipm.models.finding import Severity
from aipm.models.notifications import NotificationTrigger


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = str(Path.home() / ".local" / "state" / "aipm" / "logs" / "aipm.log")
    max_size_mb: int = 10
    backup_count: int = 3


@dataclass
class DiscoveryConfig:
    search_paths: list[str] = field(default_factory=lambda: [str(Path.home())])
    ignore_dirs: list[str] = field(
        default_factory=lambda: [
            ".git",
            ".venv",
            "__pycache__",
            "node_modules",
            ".cache",
            ".local",
            ".npm",
            ".cargo",
            ".rustup",
            ".vscode",
            ".idea",
            "dist",
            "build",
            "target",
        ]
    )
    max_depth: int = 4
    follow_symlinks: bool = False


@dataclass
class TelemetryConfig:
    enabled: bool = True
    interval_seconds: int = 15
    resource_sampling_enabled: bool = True
    resource_interval_seconds: int = 60
    resource_timeout_seconds: int = 15
    resource_stale_after_seconds: int = 180
    project_interval_seconds: int = 60
    project_timeout_seconds: int = 15
    slow_task_max_concurrency: int = 1
    sampling_mode: str = "split"
    retention_days: int = 1
    database_path: str = str(Path.home() / ".local" / "state" / "aipm" / "telemetry" / "mission_control.db")


@dataclass
class EventConfig:
    enabled: bool = True
    interval_seconds: int = 15
    event_retention_days: int = 30
    incident_retention_days: int = 180
    acknowledgement_enabled: bool = True


@dataclass
class NotificationChannelConfig:
    id: str
    name: str
    channel_type: str
    enabled: bool = False
    secret_ref: str | None = None
    destination_ref: str | None = None
    timeout_seconds: int = 10
    max_attempts: int = 3


@dataclass
class NotificationPolicyConfig:
    id: str
    name: str
    enabled: bool = False
    minimum_severity: str = Severity.CRITICAL.value
    event_types: list[str] = field(default_factory=list)
    resource_types: list[str] = field(default_factory=list)
    project_paths: list[str] = field(default_factory=list)
    transitions: list[str] = field(default_factory=lambda: [NotificationTrigger.INCIDENT_OPENED.value])
    notify_recovery: bool = False
    notify_acknowledgement: bool = False
    notify_updates: bool = False
    cooldown_seconds: int = 900
    window_seconds: int = 3600
    max_notifications: int = 3
    channels: list[str] = field(default_factory=list)


@dataclass
class NotificationConfig:
    enabled: bool = False
    interval_seconds: int = 5
    retention_days: int = 180
    default_cooldown_seconds: int = 900
    default_window_seconds: int = 3600
    default_max_notifications: int = 3
    global_window_seconds: int = 3600
    global_max_notifications: int = 100
    channels: list[NotificationChannelConfig] = field(default_factory=list)
    policies: list[NotificationPolicyConfig] = field(default_factory=list)


@dataclass
class AIPMConfig:
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    events: EventConfig = field(default_factory=EventConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
