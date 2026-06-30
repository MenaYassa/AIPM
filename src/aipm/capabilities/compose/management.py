from rich.console import Console
from rich.table import Table
from aipm.core.decorators import cli_handler
from aipm.services.project.service import ProjectService
from aipm.providers.compose.provider import ComposeProvider

class ComposeCapability:
    def __init__(self):
        self.console = Console()
        self.project_service = ProjectService()
        self.compose_provider = ComposeProvider()

    @cli_handler(action_name="compose ps")
    def ps(self, project_name: str):
        # 1. Get the domain model
        project = self.project_service.get_project(project_name)
        
        if not project.capabilities.has_compose:
            self.console.print(f"[yellow]Project '{project.name}' does not have a Compose file.[/yellow]")
            return
            
        # 2. Fetch the services using our provider
        self.console.print(f"[cyan]Fetching services for {project.name}...[/cyan]")
        services = self.compose_provider.ps(project)
        
        # 3. Present the data
        table = Table(title=f"Project: {project.name}")
        table.add_column("Service", style="bold cyan")
        table.add_column("Image", style="green")
        table.add_column("State", style="yellow")
        table.add_column("Ports")
        
        for s in services:
            table.add_row(s.name, s.image, s.state, "\n".join(s.ports) if s.ports else "None")
            
        self.console.print(table)
        
    @cli_handler(action_name="compose up")
    def up(self, project_name: str, detach: bool = True):
        project = self.project_service.get_project(project_name)
        
        if not project.capabilities.has_compose:
            self.console.print(f"[yellow]Project '{project.name}' does not have a Compose file.[/yellow]")
            return
            
        self.console.print(f"[cyan]Bringing up infrastructure for {project.name}...[/cyan]")
        self.compose_provider.up(project, detach=detach)
        self.console.print(f"[bold green]Successfully started {project.name}[/bold green]")

    @cli_handler(action_name="compose down")
    def down(self, project_name: str):
        project = self.project_service.get_project(project_name)
        
        if not project.capabilities.has_compose:
            self.console.print(f"[yellow]Project '{project.name}' does not have a Compose file.[/yellow]")
            return
            
        self.console.print(f"[cyan]Tearing down infrastructure for {project.name}...[/cyan]")
        self.compose_provider.down(project)
        self.console.print(f"[bold green]Successfully stopped {project.name}[/bold green]")