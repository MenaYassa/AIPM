from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class BackupArchive:
    project_name: str
    archive_path: Path
    timestamp: datetime
    size_bytes: int