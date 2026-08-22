from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Event

from aipm.core.app import Application
from aipm.core.exceptions import ProviderError
from aipm.models.project import Project, ProjectCapabilities
from aipm.providers.git.provider import GitProvider


class DiscoveryCancelled(ProviderError):
    """Raised when a cooperative telemetry discovery cancellation is requested."""


class DiscoveryLimitExceeded(ProviderError):
    """Raised when bounded discovery reaches a configured work limit."""


class ProjectService:
    def __init__(self, app: Application | None = None):
        self.app = app or Application.create()
        self.git_provider = GitProvider()

    def discover(self, *, cancel_event: Event | None = None, deadline: float | None = None, bounded: bool = False) -> list[Project]:
        """Discover Compose- or Git-backed projects, optionally with telemetry bounds."""
        config = self.app.config.discovery
        discovered: dict[Path, Project] = {}
        directories_visited = 0
        entries_seen = 0
        max_directories = getattr(config, "max_directories", 2000)
        max_entries = getattr(config, "max_entries", 10000)
        max_projects = getattr(config, "max_projects", 128)
        max_git_enrichments = getattr(config, "max_git_enrichments", max_projects)
        git_timeout_seconds = getattr(config, "git_timeout_seconds", 5.0)
        max_git_items = getattr(config, "max_git_items", 100)

        for configured_path in config.search_paths:
            base_dir = Path(configured_path).expanduser()
            if not base_dir.is_dir():
                continue

            for root, directories, files in os.walk(
                base_dir,
                topdown=True,
                followlinks=config.follow_symlinks,
            ):
                self._check_cancel(cancel_event, deadline)
                directories_visited += 1
                entries_seen += len(directories) + len(files)
                if bounded and directories_visited > max_directories:
                    raise DiscoveryLimitExceeded("project discovery directory bound reached")
                if bounded and entries_seen > max_entries:
                    raise DiscoveryLimitExceeded("project discovery entry bound reached")

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
                    if bounded and len(discovered) >= max_projects:
                        raise DiscoveryLimitExceeded("project discovery project bound reached")

                    discovered[root_path] = project
                    # A discovered repository is a boundary; nested Git metadata
                    # should not create duplicate managed projects.
                    directories[:] = []
                    continue

                if depth >= config.max_depth:
                    directories[:] = []

        projects = []
        git_enrichments = 0
        for path in sorted(discovered, key=lambda item: (item.name.lower(), str(item))):
            self._check_cancel(cancel_event, deadline)
            project = discovered[path]
            if project.capabilities.has_git:
                git_enrichments += 1
                if bounded and git_enrichments > max_git_enrichments:
                    raise DiscoveryLimitExceeded("project discovery Git enrichment bound reached")
                if bounded:
                    project.git = self.git_provider.repository_bounded(
                        project,
                        timeout_seconds=git_timeout_seconds,
                        max_items=max_git_items,
                        cancel_event=cancel_event,
                        deadline=deadline,
                    )
                else:
                    project.git = self.git_provider.repository(project)
            projects.append(project)
        return projects

    @staticmethod
    def _check_cancel(cancel_event: Event | None, deadline: float | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DiscoveryCancelled("project discovery cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise DiscoveryLimitExceeded("project discovery deadline reached")

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
