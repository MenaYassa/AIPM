import os
from pathlib import Path
from aipm.models.project import Project, ProjectCapabilities
from aipm.providers.compose.provider import ComposeProvider
from aipm.core.app import Application
from aipm.core.exceptions import ProviderError
from aipm.providers.git.provider import GitProvider

class ProjectService:
    def __init__(self):
        self.app = Application.create()
        self.compose_provider = ComposeProvider()
        self.git_provider = GitProvider() # Instantiate the git provider
        
    def discover(self) -> list[Project]:
        """Scans configured paths and returns discovered projects."""
        config = self.app.config.discovery
        projects = []
        
        for search_path in config.search_paths:
            base_dir = Path(search_path).expanduser()
            if not base_dir.exists():
                continue
                
            # Scan one level deep inside the search path
            for entry in base_dir.iterdir():
                if not entry.is_dir() or entry.name in config.ignore_dirs:
                    continue
                    
                project = self._inspect_directory(entry)
                if project:
                    # NEW: Enrich the project with Git state right after discovery
                    self.git_provider.enrich(project) 
                    projects.append(project)
                    
        return projects

    def _inspect_directory(self, path: Path) -> Project | None:
        """Analyzes a directory to see if it is an AIPM-manageable project."""
        capabilities = ProjectCapabilities()
        compose_files = []
        
        # 1. Check for Compose capability
        possible_compose_files = ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]
        for cf in possible_compose_files:
            if (path / cf).exists():
                capabilities.has_compose = True
                compose_files.append(str(path / cf))
                
        # 2. Check for Git capability
        if (path / ".git").exists() and (path / ".git").is_dir():
            capabilities.has_git = True
            
        # If it has neither, we ignore it (it's just a regular folder)
        if not capabilities.has_compose and not capabilities.has_git:
            return None
            
        return Project(
            name=path.name,
            path=str(path),
            capabilities=capabilities,
            compose_files=compose_files
        )
        
    def get_project(self, name: str) -> Project:
        """Finds a specific project by name."""
        projects = self.discover()
        for p in projects:
            if p.name == name:
                return p
        raise ProviderError(f"Project '{name}' not found in any search path.")