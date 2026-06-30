import typer
from rich import print
from aipm.capabilities.doctor.capability import DoctorCapability
from aipm.version import VERSION
from aipm.capabilities.project.management import ProjectCapability
from aipm.cli.compose import compose_app
from aipm.cli.git import git_app
from aipm.cli.docker.app import app as docker_app
from aipm.capabilities.health.diagnostics import HealthCapability
from aipm.capabilities.backup.snapshots import BackupCapability
from aipm.services.update.engine import UpdateEngine  # <-- Add import
from aipm.core.exceptions import UpdateError, ProviderError

app = typer.Typer(
    help="AI Platform Manager"
)

app.add_typer(
    docker_app,
    name="docker",
)

 # Attach the sub-routers (the branches)
app.add_typer(
    compose_app,
    name="compose"
)

app.add_typer(
    git_app,
    name="git"
)

@app.command()
def version():
    """Show version."""

    print(f"[green]AIPM[/green] v{VERSION}")


@app.command()
def hello():
    """Sanity check."""

    print("[cyan]Hello from AIPM[/cyan]")

@app.command()
def doctor():

    DoctorCapability().run()

@app.command()
def discover():
    """Discover all AI projects on the host machine."""
    ProjectCapability().discover()

@app.command()
def health(project_name: str):
    """Run a health diagnostic check on a specific project."""
    HealthCapability().check_health(project_name)

@app.command()
def backup(project_name: str):
    """Create a localized safety-net snapshot of a project configuration."""
    BackupCapability().snapshot(project_name)


@app.command()
def update(project_name: str):
    """Execute a fully transactional, safe update on a managed repository."""
    try:
        UpdateEngine().execute_update(project_name)
    except UpdateError as ue:
        print(f"\n[bold red]❌ TRANSACTION FAILURE:[/bold red] {ue}")
        print("[bold yellow]⚠️ No files were modified. System safely held at backup snapshot.[/bold yellow]\n")
        raise typer.Exit(code=1)
    except ProviderError as pe:
        print(f"\n[bold red]❌ CONFIGURATION ERROR:[/bold red] {pe}")
        print("[bold cyan]💡 Advice:[/bold cyan] Use [bold white]aipm discover[/bold white] to see your valid directory stack names.\n")
        raise typer.Exit(code=1)
    



    
if __name__ == "__main__":
    app()