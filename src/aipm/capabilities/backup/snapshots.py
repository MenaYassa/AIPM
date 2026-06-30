from rich.console import Console
from aipm.core.decorators import cli_handler
from aipm.services.project.service import ProjectService
from aipm.services.backup.engine import BackupEngine

class BackupCapability:
    def __init__(self):
        self.console = Console()
        self.project_service = ProjectService()
        self.backup_engine = BackupEngine()

    @cli_handler(action_name="aipm backup")
    def snapshot(self, project_name: str):
        project = self.project_service.get_project(project_name)
        self.console.print(f"[cyan]Creating pre-update state snapshot for {project.name}...[/cyan]")
        
        archive = self.backup_engine.create_snapshot(project)
        
        size_mb = archive.size_bytes / (1024 * 1024)
        self.console.print(
            f"[bold green]Snapshot successfully stored![/bold green]\n"
            f"Path: {archive.archive_path}\n"
            f"Size: {size_mb:.2f} MB"
        )