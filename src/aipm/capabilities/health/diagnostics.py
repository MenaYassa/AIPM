from rich.console import Console
from rich.table import Table
from aipm.core.decorators import cli_handler
from aipm.services.project.service import ProjectService
from aipm.services.health.service import HealthService
from aipm.models.health import HealthState

class HealthCapability:
    def __init__(self):
        self.console = Console()
        self.project_service = ProjectService()
        self.health_service = HealthService()

    @cli_handler(action_name="aipm health")
    def check_health(self, project_name: str):
        project = self.project_service.get_project(project_name)
        
        self.console.print(f"[cyan]Running diagnostics for {project.name}...[/cyan]\n")
        
        results = self.health_service.check_project(project)
        
        table = Table(title=f"Health Report: {project.name}")
        table.add_column("Component", style="cyan")
        table.add_column("Status", justify="center")
        table.add_column("Details", style="dim")
        
        for r in results:
            # Color code the statuses
            if r.state == HealthState.HEALTHY:
                status_str = "[bold green]HEALTHY[/bold green]"
            elif r.state == HealthState.DEGRADED:
                status_str = "[bold yellow]DEGRADED[/bold yellow]"
            elif r.state == HealthState.CRITICAL:
                status_str = "[bold red]CRITICAL[/bold red]"
            else:
                status_str = "[dim]UNKNOWN[/dim]"
                
            table.add_row(r.component, status_str, r.message)
            
        self.console.print(table)