# src/aipm/models/config.py

from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/aipm.log"
    max_size_mb: int = 10
    backup_count: int = 3

@dataclass
class DiscoveryConfig:
    # Directories where AIPM should look for projects
    search_paths: list[str] = field(default_factory=lambda: [
        str(Path.home() / "workspace"),
        str(Path.home() / "docker"),
        str(Path.home() / "projects")
    ])
    ignore_dirs: list[str] = field(default_factory=lambda: ["node_modules", ".git", "venv"])

@dataclass
class AIPMConfig:
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)