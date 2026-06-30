import typer
from rich.console import Console
from aipm.core.decorators import cli_handler
from aipm.services.project.service import ProjectService
from aipm.providers.git.provider import GitProvider

git_app = typer.Typer(help="Manage Git repositories")

@git_app.command("pull")
@cli_handler(action_name="git pull")
def pull(project_name: str):
    """Safely pull latest infrastructure changes for a project."""
    console = Console()
    project_service = ProjectService()
    git_provider = GitProvider()
    
    project = project_service.get_project(project_name)
    console.print(f"[cyan]Pulling latest changes for {project.name}...[/cyan]")
    
    git_provider.pull(project)
    console.print(f"[bold green]Successfully updated {project.name} from remote.[/bold green]")