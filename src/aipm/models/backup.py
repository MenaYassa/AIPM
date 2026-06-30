from dataclasses import dataclass
from datetime import datetime

@dataclass
class BackupArchive:
    project_name: str
    archive_path: str
    timestamp: datetime
    size_bytes: int