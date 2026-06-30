import os
import tarfile
from pathlib import Path
from datetime import datetime
from aipm.models.project import Project
from aipm.models.backup import BackupArchive
from aipm.core.exceptions import ProviderError

class BackupError(ProviderError):
    pass

class BackupEngine:
    def __init__(self, backup_dir: str = "/home/ubuntu/aipm_backups"):
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def create_snapshot(self, project: Project) -> BackupArchive:
        """Creates a timestamped tar.gz snapshot of the project's config directory."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_name = f"{project.name}_{timestamp}.tar.gz"
        target_archive_path = self.backup_dir / archive_name

        try:
            with tarfile.open(target_archive_path, "w:gz") as tar:
                # Walk the directory tree manually to handle permissions
                for root, dirs, files in os.walk(project.path):
                    
                    # 1. Ignore heavy/runtime directories by modifying 'dirs' in-place
                    for excluded in ['volumes', 'node_modules', '.venv', '__pycache__']:
                        if excluded in dirs:
                            dirs.remove(excluded)
                    
                    # 2. Add files safely
                    for file in files:
                        file_path = Path(root) / file
                        arcname = Path(project.name) / file_path.relative_to(project.path)
                        
                        # 3. Only attempt to add if we have read access
                        if os.access(file_path, os.R_OK):
                            tar.add(file_path, arcname=str(arcname))

            size = target_archive_path.stat().st_size
            return BackupArchive(
                project_name=project.name,
                archive_path=str(target_archive_path),
                timestamp=datetime.now(),
                size_bytes=size
            )
        except Exception as e:
            if target_archive_path.exists():
                target_archive_path.unlink()
            raise BackupError(f"Failed to create snapshot for {project.name}: {e}")