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
    allowed_caller_uids: str = typer.Option(..., "--allowed-caller-uids", help="Comma-separated UIDs allowed to connect (SO_PEERCRED). Required; refuse to start unset."),
    enable_update_plan: bool = typer.Option(False, "--enable-update-plan", help="Also serve the execute_update_plan capability (engine-backed). Without it the capability is refused."),
    update_audit_dir: str = typer.Option(None, "--update-audit-dir", help="Engine audit directory for execute_update_plan (default: <receipt-db dir>/audit)."),
):
    """Run the standalone executor service.

    Listens on a Unix domain socket for execution requests from the
    control plane. The executor does NOT require access to the
    control-plane database. It validates requests structurally and
    performs the exact authorized mutation.

    Fail-closed startup: an explicit caller UID allow-list is REQUIRED
    (a wildcard listen would accept any local uid across the privilege
    boundary). The update capability is opt-in and refused unless
    explicitly enabled with a writable engine audit directory.
    """
    import selectors
    import signal
    import threading
    from datetime import datetime, timedelta, timezone

    from aipm.control_plane.executor_ipc import (
        CAPABILITY_EXECUTE_SERVICE_UPDATE,
        CAPABILITY_EXECUTE_UPDATE_PLAN,
        ExecutorIPCServer,
    )
    from aipm.control_plane.mutation_receipt import MutationReceiptStore
    from aipm.control_plane.systemd_provider import SystemdRestartPolicy, SystemdRestartProvider
    from aipm.control_plane.standalone_executor import StandaloneSystemdExecutor, ExecutionEnvelope

    try:
        uids = {int(item.strip()) for item in allowed_caller_uids.split(",") if item.strip()}
    except ValueError as exc:
        typer.echo("Invalid --allowed-caller-uids: expected comma-separated integers", err=True)
        raise typer.Exit(code=2) from exc
    if not uids or any(uid < 0 for uid in uids):
        typer.echo("Invalid --allowed-caller-uids: at least one non-negative uid is required", err=True)
        raise typer.Exit(code=2)

    policy = SystemdRestartPolicy(
        environment="staging",
        target_id=target_id,
        unit_id=unit_id,
        canonical_unit_name=unit_name,
        policy_version="policy-v1",
    )
    provider = SystemdRestartProvider(policies=[policy])
    receipts = MutationReceiptStore(receipt_db)

    # C6.5-B: read-only receipt evidence is always served. This is
    # observation over the executor's own receipts database (SELECT-only),
    # not the update capability: capability gating is unchanged and the
    # engine-backed handler remains opt-in via --enable-update-plan.
    from aipm.composition.executor_update import compose_receipt_query_handler

    query_handler = compose_receipt_query_handler(receipts=receipts)

    update_handler = None
    if enable_update_plan:
        from pathlib import Path as _Path

        from aipm.composition.executor_update import compose_executor_update_handler
        from aipm.services.update.engine import UpdateEngine

        audit_dir = update_audit_dir
        if audit_dir is None:
            audit_dir = str(_Path(receipt_db).resolve().parent / "audit")
        audit_path = _Path(audit_dir)
        # Writability probe BEFORE binding the listener: the engine's audit
        # service must be able to persist evidence there, or startup refuses.
        # No permission is widened to make it pass.
        try:
            audit_path.mkdir(parents=True, exist_ok=True)
            probe = audit_path / ".aipm-audit-probe"
            probe.write_text("probe", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            typer.echo(f"Update audit directory is not writable: {audit_dir} ({exc})", err=True)
            raise typer.Exit(code=2) from exc
        engine = UpdateEngine(audit_service=_make_audit_service(audit_dir))
        compose_provider = getattr(engine, "compose_provider", None)
        if compose_provider is not None:
            from aipm.services.compose.execution_adapter import ComposeExecutionAdapter
            from aipm.services.compose.intelligence import ComposeIntelligenceService

            compose_intel = ComposeIntelligenceService(compose_provider=compose_provider)

            def _service_inspector(project_name: str, service_name: str):
                try:
                    proj = engine.project_service.get_project(project_name)
                    obs = compose_intel.observe(proj, query_registries=False)
                    return obs.services.get(service_name)
                except Exception:
                    return None

            compose_adapter = ComposeExecutionAdapter(
                project_resolver=engine.project_service.get_project,
                runner=engine.runner,
                inspector=_service_inspector,
            )
        else:
            compose_adapter = None
        update_handler = compose_executor_update_handler(
            engine=engine,
            compose_adapter=compose_adapter,
            receipts=receipts,
        )

    def handler(request):
        """Bridge IPC requests to the capability-backed executors."""
        if request.capability_id in (CAPABILITY_EXECUTE_UPDATE_PLAN, CAPABILITY_EXECUTE_SERVICE_UPDATE):
            if update_handler is None:
                from aipm.control_plane.executor_ipc import ExecutionResponse
                return ExecutionResponse(outcome="refused", provider_code="capability_not_enabled", action_id=request.action_id, evidence_reference="")
            return update_handler(request)
        # Legacy systemd-restart capability (default; deployment compatible).
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

    server = ExecutorIPCServer(socket_path=socket_path, handler=handler, allowed_caller_uids=uids, query_handler=query_handler)
    stop_event = threading.Event()

    def _signal_handler(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    server.start()
    typer.echo(f"Executor service listening on {socket_path} (caller uids: {sorted(uids)})", err=True)
    try:
        server.serve_forever(stop_event=stop_event)
    finally:
        server.stop()
        typer.echo("Executor service stopped.", err=True)


def _make_audit_service(audit_dir: str):
    from aipm.services.update.audit import AuditService

    return AuditService(audit_dir=audit_dir)



@app.command()
def version():
    """Show version."""

    print(f"[green]AIPM[/green] v{VERSION}")


@app.command()
def serve_operator_transport(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address. Loopback-only; non-loopback binds are refused."),
    port: int = typer.Option(8789, "--port", min=1, max=65535, help="HTTP port for the operator transport."),
    enable_update_plane: bool = typer.Option(False, "--enable-update-plane", help="Compose the update plane (engine-backed digest port + executor IPC runtime). Default off: fail-closed execution boundary."),
    executor_socket_path: str = typer.Option(None, "--executor-socket-path", help="Executor Unix socket path (default /run/aipm/executor.sock). Requires --enable-update-plane."),
    update_audit_dir: str = typer.Option(None, "--update-audit-dir", help="Writable audit directory for the update engine. Requires --enable-update-plane."),
    update_backup_dir: str = typer.Option(None, "--update-backup-dir", help="Writable backup directory for the update engine. Requires --enable-update-plane."),
):
    """Compose the durable control plane and serve the operator transport.

    Startup order is fail closed: open the dedicated control-plane SQLite
    store, compose the durable stores, run the startup recovery sweep, and
    only then bind the loopback listener. Any composition or sweep failure
    exits before the listener accepts traffic.

    By default the update runtime is deliberately not composed (fail-closed
    execution boundary). With ``--enable-update-plane`` the transport
    composes the update engine (audit + backup directories must be passed
    and are probed for writability BEFORE binding — no permission is
    widened to make the probe pass) and the executor IPC client, so the
    digest port speaks the canonical UpdatePlanIdentity space and the
    post-verification runtime crosses to the executor socket. The flags
    are all-or-nothing: partial flag sets are refused.
    """

    if not enable_update_plane:
        if (
            update_audit_dir is not None
            or update_backup_dir is not None
            or executor_socket_path is not None
        ):
            typer.echo(
                "--update-audit-dir/--update-backup-dir/--executor-socket-path require --enable-update-plane",
                err=True,
            )
            raise typer.Exit(code=2)

    update_engine = None
    executor_ipc_client = None
    compose_service = None
    if enable_update_plane:
        if update_audit_dir is None or update_backup_dir is None:
            typer.echo(
                "--enable-update-plane requires both --update-audit-dir and --update-backup-dir",
                err=True,
            )
            raise typer.Exit(code=2)
        from pathlib import Path as _Path

        from aipm.services.backup.engine import BackupEngine
        from aipm.services.compose.service import ComposeService
        from aipm.services.update.engine import UpdateEngine

        for label, dir_path, probe_name in (
            ("audit", update_audit_dir, ".aipm-audit-probe"),
            ("backup", update_backup_dir, ".aipm-backup-probe"),
        ):
            # Writability probe BEFORE composing: the engine's audit and
            # backup services must be able to persist evidence there, or
            # startup refuses. No permission is widened to make it pass.
            try:
                probe_dir = _Path(dir_path)
                probe_dir.mkdir(parents=True, exist_ok=True)
                probe = probe_dir / probe_name
                probe.write_text("probe", encoding="utf-8")
                probe.unlink()
            except OSError as exc:
                typer.echo(f"Update {label} directory is not writable: {dir_path} ({exc})", err=True)
                raise typer.Exit(code=2) from exc

        update_engine = UpdateEngine(
            audit_service=_make_audit_service(update_audit_dir),
            backup_engine=BackupEngine(update_backup_dir),
        )
        compose_provider = getattr(update_engine, "compose_provider", None)
        service_evidence_verifier = None
        service_plan_port = None
        if compose_provider is not None:
            from aipm.composition.service_evidence import compose_service_evidence_verifier

            compose_service = ComposeService(provider=compose_provider)
            project_service = getattr(update_engine, "project_service", None)
            if project_service is not None:
                def _resolve_project(target):
                    return project_service.get_project(target) if isinstance(target, str) else target

                service_evidence_verifier = compose_service_evidence_verifier(
                    compose_service,
                    project_resolver=_resolve_project,
                )
                service_plan_port = lambda target, svc: compose_service.plan_service_update(
                    _resolve_project(target),
                    svc,
                    query_registries=True,
                )
        if executor_socket_path is None:
            from aipm.control_plane.executor_ipc import EXECUTOR_SOCKET_PATH

            executor_socket_path = EXECUTOR_SOCKET_PATH
        from aipm.control_plane.executor_ipc import ExecutorIPCClient

        executor_ipc_client = ExecutorIPCClient(socket_path=executor_socket_path)

    from aipm.control_plane.composition import serve_operator_transport

    try:
        serve_operator_transport(
            host=host,
            port=port,
            update_engine=update_engine,
            executor_ipc_client=executor_ipc_client,
            compose_service=compose_service,
            service_plan_port=service_plan_port,
            service_evidence_verifier=service_evidence_verifier,
        )
    except Exception as exc:
        typer.echo(f"Operator transport refused to start: {exc}", err=True)
        raise typer.Exit(code=1)


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


registration_app = typer.Typer(name="registration", help="Production registration management")
app.add_typer(registration_app, name="registration")


@registration_app.command("register")
def register_project(
    target_id: str = typer.Option(..., "--target-id", help="Target identifier for the project."),
    project_path: str = typer.Option(..., "--path", help="Absolute path to the project directory."),
    environment: str = typer.Option(..., "--environment", help="Environment (staging or production)."),
    runtime_mode: str = typer.Option(..., "--runtime-mode", help="Runtime mode (compose or systemd)."),
    reason: str = typer.Option(..., "--reason", help="Audit reason for registration."),
    registered_by: str = typer.Option("operator", "--registered-by", help="Operator identifier."),
):
    """Register a project for production execution.

    This is a host-authoritative operation that requires operator approval.
    The registration is audited and persists across restarts.
    """
    from aipm.control_plane.registration_service import RegistrationService, RegistrationValidator
    from aipm.control_plane.storage.sqlite_store import (
        ControlPlaneDatabase,
        SQLiteProjectRegistrationStore,
        default_database_path,
    )
    from aipm.providers.compose.identity import resolve_compose_project_name

    validator = RegistrationValidator(compose_identity_resolver=resolve_compose_project_name)
    service = RegistrationService(validator=validator)
    registration, validation_result = service.create_registration(
        target_id=target_id,
        project_path=project_path,
        environment=environment,
        runtime_mode=runtime_mode,
        registered_by=registered_by,
    )

    if not validation_result.valid:
        typer.echo(f"Registration validation failed: {validation_result.error_message}", err=True)
        raise typer.Exit(code=2)

    db_path = default_database_path()
    db = ControlPlaneDatabase(db_path)
    store = SQLiteProjectRegistrationStore(db)

    try:
        store.save(registration)
        typer.echo(f"✓ Registered {target_id} ({environment}) at {validation_result.canonical_path}")
        typer.echo(f"  Registration digest: {registration.registration_digest}")
        typer.echo(f"  Reason: {reason}")
    except Exception as exc:
        typer.echo(f"Registration failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@registration_app.command("revoke")
def revoke_project(
    target_id: str = typer.Option(..., "--target-id", help="Target identifier for the project."),
    environment: str = typer.Option(..., "--environment", help="Environment (staging or production)."),
    reason: str = typer.Option(..., "--reason", help="Audit reason for revocation."),
    revoked_by: str = typer.Option("operator", "--revoked-by", help="Operator identifier."),
    yes: bool = typer.Option(False, "--yes", help="Confirm revocation without prompt."),
):
    """Revoke a production registration.

    This is a host-authoritative operation that requires operator approval.
    Revocation is permanent and audited.
    """
    from aipm.control_plane.registration import RegistrationStatus
    from aipm.control_plane.storage.sqlite_store import (
        ControlPlaneDatabase,
        SQLiteProjectRegistrationStore,
        default_database_path,
    )

    if not yes:
        typer.echo(f"Revoking registration for {target_id} ({environment})")
        typer.echo(f"Reason: {reason}")
        confirm = typer.confirm("Proceed with revocation?")
        if not confirm:
            typer.echo("Revocation cancelled.")
            raise typer.Exit(code=0)

    db_path = default_database_path()
    db = ControlPlaneDatabase(db_path)
    store = SQLiteProjectRegistrationStore(db)

    try:
        updated = store.update_status(
            target_id=target_id,
            environment=environment,
            new_status=RegistrationStatus.REVOKED,
            actor_subject=revoked_by,
            reason=reason,
        )
        if updated is None:
            typer.echo(f"Registration not found: {target_id} ({environment})", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"✓ Revoked {target_id} ({environment})")
        typer.echo(f"  Reason: {reason}")
    except Exception as exc:
        typer.echo(f"Revocation failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@registration_app.command("list")
def list_registrations(
    environment: str = typer.Option(None, "--environment", help="Filter by environment."),
    status: str = typer.Option(None, "--status", help="Filter by status (REGISTERED, DISABLED, REVOKED)."),
):
    """List all project registrations."""
    from aipm.control_plane.registration import RegistrationStatus
    from aipm.control_plane.storage.sqlite_store import (
        ControlPlaneDatabase,
        SQLiteProjectRegistrationStore,
        default_database_path,
    )

    status_filter = None
    if status:
        try:
            status_filter = RegistrationStatus[status.upper()]
        except KeyError:
            typer.echo(f"Invalid status: {status}. Must be one of: REGISTERED, DISABLED, REVOKED", err=True)
            raise typer.Exit(code=2)

    db_path = default_database_path()
    db = ControlPlaneDatabase(db_path)
    store = SQLiteProjectRegistrationStore(db)

    try:
        registrations = store.list_registrations(environment=environment, status=status_filter)
        if not registrations:
            typer.echo("No registrations found.")
            return

        for reg in registrations:
            typer.echo(f"{reg.target_id} ({reg.environment}) - {reg.status.value}")
            typer.echo(f"  Path: {reg.canonical_project_path}")
            typer.echo(f"  Runtime: {reg.runtime_mode}")
            typer.echo(f"  Registered: {reg.registered_at.isoformat()}")
            if reg.revoked_at:
                typer.echo(f"  Revoked: {reg.revoked_at.isoformat()}")
                typer.echo(f"  Revocation reason: {reg.revocation_reason}")
            typer.echo("")
    except Exception as exc:
        typer.echo(f"List failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc



if __name__ == "__main__":
    app()