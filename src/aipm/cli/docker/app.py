# src/aipm/cli/docker/app.py

import typer
from aipm.capabilities.docker.ps import PsCapability
from aipm.capabilities.docker.inspect import InspectCapability
from aipm.capabilities.docker.system import SystemCapability

app = typer.Typer()

@app.command("ps")
def ps():
    PsCapability().ps()

@app.command()
def inspect(name: str, raw: bool = typer.Option(False, "--raw", help="Show raw JSON output")):
    """Inspect a container."""
    InspectCapability().inspect(name, raw=raw)

@app.command()
def stop(name: str):
    """Stop a container."""
    from aipm.core.app import Application
    app_instance = Application.create()
    app_instance.docker.stop(name)
    print(f"Container '{name}' stopped.")

@app.command()
def restart(name: str):
    """Restart a container."""
    from aipm.core.app import Application
    app_instance = Application.create()
    app_instance.docker.restart(name)
    print(f"Container '{name}' restarted.")

@app.command()
def start(name: str):
    """Start a container."""
    from aipm.core.app import Application
    app_instance = Application.create()
    app_instance.docker.start(name)
    print(f"Container '{name}' started.")

@app.command()
def logs(name: str, tail: int = typer.Option(100, "--tail", "-t", help="Number of lines to show")):
    """Fetch logs for a container."""
    SystemCapability().logs(name, tail)

@app.command()
def images():
    """List Docker images."""
    SystemCapability().images()

@app.command()
def volumes():
    """List Docker volumes."""
    SystemCapability().volumes()

@app.command()
def networks():
    """List Docker networks."""
    SystemCapability().networks()