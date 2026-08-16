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
from aipm.dashboard.server import run as run_dashboard
from aipm.capabilities.telemetry.commands import run as run_telemetry, sample as sample_telemetry
from aipm.capabilities.events.commands import process as process_events, run as run_events

app = typer.Typer(
    help="AI Platform Manager"
)
telemetry_app = typer.Typer(help="Collect and query historical telemetry")
app.add_typer(telemetry_app, name="telemetry")
events_app = typer.Typer(help="Derive deterministic events and incidents")
app.add_typer(events_app, name="events")


@telemetry_app.command("sample")
def telemetry_sample():
    """Collect and persist one read-only telemetry sample."""
    sample_telemetry()


@telemetry_app.command("run")
def telemetry_run():
    """Run the dedicated read-only telemetry sampler until stopped."""
    run_telemetry()


@events_app.command("process")
def events_process(run_id: int | None = typer.Option(None, "--run-id", min=1, help="Process one persisted telemetry run; omit to process pending runs.")):
    """Process persisted telemetry into deterministic events and incidents."""
    process_events(run_id=run_id)


@events_app.command("run")
def events_run():
    """Run the dedicated deterministic event processor until stopped."""
    run_events()

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
def dashboard(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address. Keep loopback-only unless protected by a trusted proxy."),
    port: int = typer.Option(8787, "--port", min=1, max=65535, help="HTTP port for Mission Control."),
):
    """Launch the read-only Mission Control dashboard."""
    run_dashboard(host=host, port=port)


@app.command()
def update(
    project_name: str,
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan and make no state changes."),
    approve: bool = typer.Option(False, "--yes", help="Approve the planned state-changing operation."),
):
    """Plan and, when explicitly approved, execute a safe project update."""
    try:
        UpdateEngine().execute_update(project_name, dry_run=dry_run, approve=approve)
    except UpdateError as error:
        print(f"\n[bold red]Update stopped:[/bold red] {error}")
        raise typer.Exit(code=1) from error
    except ProviderError as error:
        print(f"\n[bold red]Configuration error:[/bold red] {error}")
        print("[cyan]Use 'aipm discover' to see configured project names.\n")
        raise typer.Exit(code=1) from error
    



    
if __name__ == "__main__":
    app()