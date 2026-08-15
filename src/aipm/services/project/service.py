from __future__ import annotations

import os
from pathlib import Path

from aipm.core.app import Application
from aipm.core.exceptions import ProviderError
from aipm.models.project import Project, ProjectCapabilities
from aipm.providers.git.provider import GitProvider


class ProjectService:
    def __init__(self, app: Application | None = None):
        self.app = app or Application.create()
        self.git_provider = GitProvider()

    def discover(self) -> list[Project]:
        """Discover Compose- or Git-backed projects under configured paths."""
        config = self.app.config.discovery
        discovered: dict[Path, Project] = {}

        for configured_path in config.search_paths:
            base_dir = Path(configured_path).expanduser()
            if not base_dir.is_dir():
                continue

            for root, directories, _files in os.walk(
                base_dir,
                topdown=True,
                followlinks=config.follow_symlinks,
            ):
                root_path = Path(root)
                try:
                    depth = len(root_path.relative_to(base_dir).parts)
                except ValueError:
                    continue

                directories[:] = sorted(
                    directory
                    for directory in directories
                    if directory not in config.ignore_dirs
                )

                project = self._inspect_directory(root_path)
                if project is not None:
                    discovered[root_path] = project
                    # A discovered repository is a boundary; nested Git metadata
                    # should not create duplicate managed projects.
                    directories[:] = []
                    continue

                if depth >= config.max_depth:
                    directories[:] = []

        projects = []
        for path in sorted(discovered, key=lambda item: (item.name.lower(), str(item))):
            project = discovered[path]
            project.git = self.git_provider.repository(project) if project.capabilities.has_git else None
            projects.append(project)
        return projects

    def _inspect_directory(self, path: Path) -> Project | None:
        capabilities = ProjectCapabilities()
        compose_files: list[str] = []
        for filename in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
            compose_path = path / filename
            if compose_path.is_file():
                capabilities.has_compose = True
                compose_files.append(str(compose_path))

        capabilities.has_git = (path / ".git").is_dir()
        if not capabilities.has_compose and not capabilities.has_git:
            return None

        return Project(
            name=path.name,
            path=str(path),
            capabilities=capabilities,
            compose_files=compose_files,
        )

    def get_project(self, name: str) -> Project:
        if not name.strip():
            raise ProviderError("Project name cannot be empty.")
        for project in self.discover():
            if project.name == name:
                return project
        raise ProviderError(f"Project '{name}' not found in configured search paths.")
