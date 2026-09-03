from __future__ import annotations

import os
import tarfile
from pathlib import Path

from aipm.models.project import Project
from aipm.models.rollback import RestoreResult
from aipm.services.backup.engine import BackupEngine


class RestoreError(Exception):
    """Raised when a snapshot archive cannot be restored safely."""


class RollbackManager:
    """Restore a project's files from a BackupEngine snapshot archive.

    Contract:
    - Restores exactly the regular files recorded in the snapshot archive,
      overwriting their current on-disk versions.
    - Never deletes anything: files created after the snapshot are left in
      place and reported in ``RestoreResult.left_in_place``.
    - Out of scope (never touched): the ``.git`` directory, directories
      excluded by ``BackupEngine.DEFAULT_EXCLUDES``, Docker volumes,
      databases, runtime state, and anything outside the project directory.
    - Path safety: every archive member must sit under the project-name
      prefix, resolve inside the project directory, and be a regular file.
      Symlinks, traversal members, and foreign prefixes fail the whole
      restore (fail closed).
    """

    OUT_OF_SCOPE_DIRS = {".git", *BackupEngine.DEFAULT_EXCLUDES}

    def restore_plan(self, archive_path: Path | str, project: Project) -> RestoreResult:
        """Inspect what a restore would do without writing anything."""
        try:
            _, project_path, planned, skipped = self._validated_members(archive_path, project)
        except RestoreError as exc:
            return RestoreResult(attempted=False, success=False, error=str(exc))
        archived = {str(target.relative_to(project_path)) for _, target in planned}
        return RestoreResult(
            attempted=False,
            success=True,
            restored=sorted(str(relative) for relative in archived),
            left_in_place=self._left_in_place(project_path, archived),
            skipped=skipped,
        )

    def restore(self, archive_path: Path | str, project: Project) -> RestoreResult:
        """Restore the project files captured in the snapshot archive."""
        try:
            archive_path, project_path, planned, skipped = self._validated_members(archive_path, project)
        except RestoreError as exc:
            return RestoreResult(attempted=True, success=False, error=str(exc))

        restored: list[str] = []
        try:
            with tarfile.open(archive_path, "r:*") as archive:
                for member, target in planned:
                    source = archive.extractfile(member)
                    if source is None:
                        raise RestoreError(f"Archive member could not be read: {member.name}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, "wb") as handle:
                        handle.write(source.read())
                    restored.append(str(target.relative_to(project_path)))
        except (RestoreError, OSError, tarfile.TarError) as exc:
            return RestoreResult(
                attempted=True,
                success=False,
                restored=sorted(restored),
                skipped=skipped,
                error=str(exc),
            )
        archived = {str(target.relative_to(project_path)) for _, target in planned}
        return RestoreResult(
            attempted=True,
            success=True,
            restored=sorted(restored),
            left_in_place=self._left_in_place(project_path, archived),
            skipped=skipped,
        )

    def _validated_members(
        self, archive_path: Path | str, project: Project
    ) -> tuple[Path, Path, list[tuple[tarfile.TarInfo, Path]], list[str]]:
        archive_path = Path(archive_path).expanduser().resolve()
        if not archive_path.is_file():
            raise RestoreError(f"Snapshot archive not found: {archive_path}")

        project_path = Path(project.path).expanduser().resolve()
        if not project_path.is_dir():
            raise RestoreError(f"Project path does not exist or is not a directory: {project_path}")

        prefix = f"{project.name}/"
        planned: list[tuple[tarfile.TarInfo, Path]] = []
        skipped: list[str] = []
        try:
            with tarfile.open(archive_path, "r:*") as archive:
                for member in archive.getmembers():
                    relative = self._relative_target(member, prefix, project_path)
                    if relative is None:
                        skipped.append(member.name)
                        continue
                    planned.append((member, relative))
        except (tarfile.TarError, OSError) as exc:
            raise RestoreError(f"Snapshot archive could not be read: {exc}") from exc
        return archive_path, project_path, planned, skipped

    def _relative_target(
        self, member: tarfile.TarInfo, prefix: str, project_path: Path
    ) -> Path | None:
        name = member.name
        if member.isdir():
            return None
        if not member.isfile():
            raise RestoreError(f"Refusing non-regular archive member: {name}")
        relative_name = name[len(prefix) :] if name.startswith(prefix) else ""
        if not relative_name:
            raise RestoreError(f"Archive member is outside the project snapshot layout: {name}")
        candidate = Path(relative_name)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise RestoreError(f"Archive member escapes the project directory: {name}")
        if candidate.parts[0] in self.OUT_OF_SCOPE_DIRS:
            return None
        target = project_path / candidate
        current = project_path
        for part in candidate.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise RestoreError(f"Refusing to restore through a symlinked directory: {current}")
        return target

    def _left_in_place(self, project_path: Path, archived: set[str]) -> list[str]:
        present: list[str] = []
        for root, directories, files in os.walk(project_path, topdown=True, followlinks=False):
            root_path = Path(root)
            directories[:] = sorted(
                directory for directory in directories if directory not in self.OUT_OF_SCOPE_DIRS
            )
            for filename in sorted(files):
                file_path = root_path / filename
                if file_path.is_symlink() or not file_path.is_file():
                    continue
                relative = str(file_path.relative_to(project_path))
                if relative not in archived:
                    present.append(relative)
        return sorted(present)