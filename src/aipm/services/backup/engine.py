from __future__ import annotations

import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from aipm.core.exceptions import ProviderError
from aipm.models.backup import BackupArchive
from aipm.models.project import Project


class BackupError(ProviderError):
    """Raised when a project snapshot cannot be created."""


class BackupEngine:
    DEFAULT_EXCLUDES = {".git", ".venv", "venv", "node_modules", "__pycache__", ".cache", "dist", "build", "target"}

    def __init__(self, backup_dir: str | Path | None = None):
        configured = os.environ.get("AIPM_BACKUP_DIR")
        self.backup_dir = Path(backup_dir or configured or (Path.home() / ".local" / "state" / "aipm" / "backups")).expanduser()
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackupError(f"Unable to create backup directory {self.backup_dir}: {exc}") from exc

    def create_snapshot(self, project: Project) -> BackupArchive:
        """Create a timestamped tar.gz snapshot of project configuration and source files."""
        project_path = Path(project.path).expanduser().resolve()
        if not project_path.is_dir():
            raise BackupError(f"Project path does not exist or is not a directory: {project_path}")

        timestamp = datetime.now(timezone.utc)
        archive_name = f"{project.name}_{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}.tar.gz"
        archive_path = self.backup_dir / archive_name
        backup_root = self.backup_dir.resolve()

        try:
            with tarfile.open(archive_path, "w:gz") as archive:
                for root, directories, files in os.walk(project_path, topdown=True, followlinks=False):
                    root_path = Path(root)
                    directories[:] = sorted(
                        directory
                        for directory in directories
                        if directory not in self.DEFAULT_EXCLUDES
                        and (root_path / directory).resolve() != backup_root
                    )
                    for filename in sorted(files):
                        file_path = root_path / filename
                        if file_path.is_symlink() or not file_path.is_file():
                            continue
                        relative_path = file_path.relative_to(project_path)
                        archive.add(file_path, arcname=str(Path(project.name) / relative_path), recursive=False)

            return BackupArchive(
                project_name=project.name,
                archive_path=archive_path,
                timestamp=timestamp,
                size_bytes=archive_path.stat().st_size,
            )
        except (OSError, tarfile.TarError) as exc:
            archive_path.unlink(missing_ok=True)
            raise BackupError(f"Failed to create snapshot for {project.name}: {exc}") from exc
