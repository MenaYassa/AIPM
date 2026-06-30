from rich.console import Console
from rich.table import Table
from aipm.core.decorators import cli_handler
from aipm.services.project.service import ProjectService

class ProjectCapability:
    def __init__(self):
        self.console = Console()
        self.project_service = ProjectService()

    @cli_handler(action_name="aipm discover")
    def discover(self):
        self.console.print("[cyan]Scanning infrastructure...[/cyan]")
        projects = self.project_service.discover()
        
        if not projects:
            self.console.print("[yellow]No projects found in configured search paths.[/yellow]")
            return

        table = Table(title="Discovered Projects")
        table.add_column("Name", style="bold cyan")
        table.add_column("Path", style="dim")
        table.add_column("Compose", justify="center")
        table.add_column("Git", justify="center")
        table.add_column("Branch", style="magenta") # NEW
        table.add_column("Status", style="bold")   # NEW

        for p in projects:
            compose_check = "[green]✓[/green]" if p.capabilities.has_compose else "[red]✗[/red]"
            git_check = "[green]✓[/green]" if p.capabilities.has_git else "[red]✗[/red]"
            
            # Format the branch and dirty status cleanly
            branch = p.git_branch if p.git_branch else "N/A"
            if not p.capabilities.has_git:
                status = "N/A"
            else:
                status = "[red]Dirty (Unsaved)[/red]" if p.git_dirty else "[green]Clean[/green]"
            
            table.add_row(p.name, p.path, compose_check, git_check, branch, status)
            
        self.console.print(table)
        self.console.print(f"\n[green]Total projects managed by AIPM: {len(projects)}[/green]")