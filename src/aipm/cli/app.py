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
from aipm.capabilities.update import UpdateCapability
from aipm.services.update.engine import UpdateEngine  # <-- Add import
from aipm.core.exceptions import UpdateError, ProviderError
from aipm.dashboard.server import run as run_dashboard
from aipm.capabilities.telemetry.commands import resource_sample as resource_sample_telemetry, run as run_telemetry, sample as sample_telemetry
from aipm.control_plane.executor_ipc import ExecutorIPCServer
from aipm.capabilities.events.commands import process as process_events, run as run_events
from aipm.capabilities.notifications.commands import list_notifications, metrics as notification_metrics, reconcile as reconcile_notification, retain as retain_notifications, retry as retry_notification, run as run_notifications, test_channel
from aipm.cli.mission_control import tui_app

app = typer.Typer(
    help="AI Platform Manager"
)
telemetry_app = typer.Typer(help="Collect and query historical telemetry")
app.add_typer(telemetry_app, name="telemetry")
events_app = typer.Typer(help="Derive deterministic events and incidents")
app.add_typer(events_app, name="events")
notifications_app = typer.Typer(help="Inspect and deliver incident notifications")
app.add_typer(notifications_app, name="notifications")
app.add_typer(tui_app, name="tui")


@telemetry_app.command("sample")
def telemetry_sample():
    """Collect and persist one read-only telemetry sample."""
    sample_telemetry()


@telemetry_app.command("resource-sample")
def telemetry_resource_sample():
    """Collect one bounded aggregate Docker resource sample."""
    resource_sample_telemetry()


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


@notifications_app.command("list")
def notifications_list():
    """List notification audit records without sending anything."""
    list_notifications()


@notifications_app.command("retry")
def notifications_retry(notification_id: int = typer.Argument(..., min=1), yes: bool = typer.Option(False, "--yes", help="Confirm the bounded operator retry.")):
    """Queue an eligible failed notification for a bounded operator retry."""
    retry_notification(notification_id, yes=yes)


@notifications_app.command("reconcile")
def notifications_reconcile(notification_id: int = typer.Argument(..., min=1), delivered: bool = typer.Option(..., "--delivered/--not-delivered", help="Record the confirmed provider outcome."), yes: bool = typer.Option(False, "--yes", help="Confirm the UNKNOWN reconciliation.")):
    """Reconcile an UNKNOWN delivery without blindly retrying it."""
    reconcile_notification(notification_id, delivered=delivered, yes=yes)


@notifications_app.command("retain")
def notifications_retain():
    """Apply configured timestamp-based notification retention."""
    retain_notifications()


@notifications_app.command("metrics")
def notifications_metrics():
    """Show safe notification delivery metrics."""
    notification_metrics()


@notifications_app.command("test")
def notifications_test(channel_id: str = typer.Argument(...), yes: bool = typer.Option(False, "--yes", help="Explicitly acknowledge a real external test.")):
    """Validate the channel test boundary without sending by default."""
    test_channel(channel_id, confirm=yes)


@notifications_app.command("run")
def notifications_run():
    """Run the dedicated notification projector and delivery worker until stopped."""
    run_notifications()

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

executor_app = typer.Typer(name="executor", help="Executor service operations")
app.add_typer(executor_app, name="executor")


@executor_app.command()
def run(
    socket_path: str = typer.Option("/run/aipm/executor.sock", "--socket-path", help="Unix socket path for the executor IPC server."),
    receipt_db: str = typer.Option("/var/lib/aipm-executor/state/receipts.db", "--receipt-db", help="Path to the executor mutation receipt database."),
    unit_name: str = typer.Option("aipm-telemetry.service", "--unit", help="The canonical systemd unit name."),
    unit_id: str = typer.Option("aipm-telemetry", "--unit-id", help="The unit identifier for the allow-list."),
    target_id: str = typer.Option("aipm-telemetry", "--target-id", help="The target identifier."),
):
    """Run the standalone executor service.

    Listens on a Unix domain socket for execution requests from the
    control plane. The executor does NOT require access to the
    control-plane database. It validates requests structurally and
    performs the exact authorized mutation.
    """
    import selectors
    import signal
    import threading
    from datetime import datetime, timedelta, timezone

    from aipm.control_plane.executor_ipc import ExecutorIPCServer
    from aipm.control_plane.mutation_receipt import MutationReceiptStore
    from aipm.control_plane.systemd_provider import SystemdRestartPolicy, SystemdRestartProvider
    from aipm.control_plane.standalone_executor import StandaloneSystemdExecutor, ExecutionEnvelope

    policy = SystemdRestartPolicy(
        environment="staging",
        target_id=target_id,
        unit_id=unit_id,
        canonical_unit_name=unit_name,
        policy_version="policy-v1",
    )
    provider = SystemdRestartProvider(policies=[policy])
    receipts = MutationReceiptStore(receipt_db)

    def handler(request):
        """Bridge IPC request to the standalone executor."""
        envelope = ExecutionEnvelope(
            protocol_version="mc612-execution-envelope-v1",
            action_id=request.action_id,
            action_version=1,
            capability_id=request.capability_id,
            capability_version="1",
            target_id=request.target_id,
            environment="staging",
            unit_name=unit_name,
            contract_digest=request.contract_digest,
            fencing_token=request.fencing_token,
            lease_id=request.lease_id,
            issued_at=datetime.now(timezone.utc).isoformat(),
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )
        executor = StandaloneSystemdExecutor(
            provider=provider, policy=policy, receipts=receipts)
        result = executor.execute_restart(envelope)
        from aipm.control_plane.executor_ipc import ExecutionResponse
        return ExecutionResponse(
            outcome=result.outcome,
            provider_code=result.provider_code,
            action_id=result.action_id,
            evidence_reference=result.evidence_reference,
        )

    server = ExecutorIPCServer(socket_path=socket_path, handler=handler)
    stop_event = threading.Event()

    def _signal_handler(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    server.start()
    typer.echo(f"Executor service listening on {socket_path}", err=True)
    try:
        server.serve_forever(stop_event=stop_event)
    finally:
        server.stop()
        typer.echo("Executor service stopped.", err=True)



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
        engine = UpdateEngine()  # UpdateEngine().execute_update is the only legacy CLI entry
        UpdateCapability(engine=engine).run(project_name, dry_run=dry_run, approve=approve)
    except UpdateError as error:
        print(f"\n[bold red]Update stopped:[/bold red] {error}")
        raise typer.Exit(code=1) from error
    except ProviderError as error:
        print(f"\n[bold red]Configuration error:[/bold red] {error}")
        print("[cyan]Use 'aipm discover' to see configured project names.\n")
        raise typer.Exit(code=1) from error
    



    
if __name__ == "__main__":
    app()