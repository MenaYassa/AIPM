import typer
from aipm.capabilities.compose.management import ComposeCapability

compose_app = typer.Typer(help="Manage Compose projects")

@compose_app.command("ps")
def ps(project_name: str):
    """List services inside a specific project."""
    ComposeCapability().ps(project_name)

@compose_app.command("up")
def up(project_name: str, detach: bool = typer.Option(True, "--detach", "-d", help="Run containers in the background")):
    """Start and attach to containers for a project."""
    ComposeCapability().up(project_name, detach=detach)

@compose_app.command("down")
def down(project_name: str):
    """Stop and remove containers, networks, and images for a project."""
    ComposeCapability().down(project_name)