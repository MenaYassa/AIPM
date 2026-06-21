import typer
from rich import print

from aipm.version import VERSION

app = typer.Typer(
    help="AI Platform Manager"
)


@app.command()
def version():
    """Show version."""

    print(f"[green]AIPM[/green] v{VERSION}")


@app.command()
def hello():
    """Sanity check."""

    print("[cyan]Hello from AIPM[/cyan]")
