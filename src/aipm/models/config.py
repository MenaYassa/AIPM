from dataclasses import dataclass, field
from pathlib import Path


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
    retention_days: int = 1
    database_path: str = str(Path.home() / ".local" / "state" / "aipm" / "telemetry" / "mission_control.db")


@dataclass
class AIPMConfig:
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
