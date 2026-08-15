from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from aipm.models.update import UpdateAudit


class AuditService:
    def __init__(self, audit_dir: str | Path | None = None):
        configured = os.environ.get("AIPM_AUDIT_DIR")
        self.audit_dir = Path(audit_dir or configured or (Path.home() / ".local" / "state" / "aipm" / "audit")).expanduser()
        self.audit_dir.mkdir(parents=True, exist_ok=True)

    def write(self, audit: UpdateAudit) -> Path:
        safe_project = "".join(character if character.isalnum() or character in "-_." else "_" for character in audit.project)
        filename = f"{audit.started_at.strftime('%Y%m%dT%H%M%S%fZ')}_{safe_project}.json"
        path = self.audit_dir / filename
        payload = asdict(audit)
        path.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")
        return path


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
