"""D4-A Change 2: the operator transport composition exposes the update plane.

Three proofs, one per seam:

1. ``serve_operator_transport`` (control_plane.composition) forwards
   ``update_engine`` + ``executor_ipc_client`` to
   ``compose_operator_service`` exactly once, sweep on (recorder); the
   default (no flags) forwards None/None — exact pre-D4-A behavior;
2. the CLI ``serve-operator-transport`` command wires the real seams under
   ``--enable-update-plane`` (stubbed engine class; composition seam
   patched to a recorder) with all-or-nothing flag validation and a
   fail-closed writability probe (exit 2, engine never built);
3. the engine-composed digest port speaks the canonical
   ``UpdatePlanIdentity`` space and the runtime crosses to the IPC client
   (service attrs + digest equality) — the runbook P0-gap requirement.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

from aipm.control_plane.composition import serve_operator_transport
from aipm.control_plane.executor_ipc import (
    EXECUTOR_SOCKET_PATH,
    ExecutorIPCClient,
)
from aipm.services.update.plan_identity import UpdatePlanIdentity
from tests.test_mc612_stage9_transport import NOW, SECRET, VERIFIER, _Clock

PROJECT_ID = "d4a-update-demo"


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubPlanEngine:
    """Minimal engine surface for the digest port (plan_update only)."""

    def __init__(self, plan):
        self._plan = plan
        self.plan_calls = []

    def plan_update(self, project, *, dry_run):
        self.plan_calls.append((project, dry_run))
        return self._plan


class _RecordingClient:
    """ExecutorIPCClient stand-in: records the sent request."""

    def __init__(self):
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        from aipm.control_plane.executor_ipc import ExecutionResponse

        return ExecutionResponse(
            outcome="succeeded",
            provider_code="update_ok",
            action_id="a" * 64,
            evidence_reference="update-audit:/tmp/audit/x.json",
        )


def _make_plan(dry_run: bool = False):
    from aipm.models.update import UpdatePlan, UpdateRisk

    return UpdatePlan(
        project=PROJECT_ID,
        project_path=str(Path("/srv") / PROJECT_ID),
        dry_run=dry_run,
        proceed=True,
        approval_required=True,
        risk=UpdateRisk.MEDIUM,
        reasons=("reason-1",),
        actions=("action-1",),
    )


# ---------------------------------------------------------------------------
# Seam 1: composition forwarding (recorder)
# ---------------------------------------------------------------------------


def _fake_compose_record(captured, tmp_path):
    def _compose(**kwargs):
        captured.update(kwargs)

        class _StubSessions:
            class _Timeout:
                @staticmethod
                def total_seconds():
                    return 0

            _inactivity_timeout = _Timeout()

        class _StubService:
            _sessions = _StubSessions()

        return {
            "database": None,
            "database_path": tmp_path / "cp.db",
            "ledger": None,
            "actions": None,
            "plans": None,
            "sessions": None,
            "kill_switches": None,
            "service": _StubService(),
            "sweep_result": None,
        }

    return _compose


def test_serve_operator_transport_forwards_update_plane(monkeypatch, tmp_path):
    from aipm.control_plane import composition as composition_module
    from aipm.control_plane import transport as transport_module

    captured: dict = {}
    monkeypatch.setattr(
        composition_module, "compose_operator_service", _fake_compose_record(captured, tmp_path)
    )
    monkeypatch.setattr(transport_module, "run_operator_transport", lambda *a, **k: None)

    engine = object()
    client = object()
    serve_operator_transport(
        database_path=tmp_path / "cp.db",
        verifier="stub",
        update_engine=engine,
        executor_ipc_client=client,
    )

    assert captured["update_engine"] is engine
    assert captured["executor_ipc_client"] is client
    assert captured["run_sweep"] is True


def test_serve_operator_transport_default_is_fail_closed(monkeypatch, tmp_path):
    from aipm.control_plane import composition as composition_module
    from aipm.control_plane import transport as transport_module

    captured: dict = {}
    monkeypatch.setattr(
        composition_module, "compose_operator_service", _fake_compose_record(captured, tmp_path)
    )
    monkeypatch.setattr(transport_module, "run_operator_transport", lambda *a, **k: None)

    serve_operator_transport(
        database_path=tmp_path / "cp.db",
        verifier="stub",
    )

    assert captured["update_engine"] is None
    assert captured["executor_ipc_client"] is None


# ---------------------------------------------------------------------------
# Seam 2: CLI command wiring
# ---------------------------------------------------------------------------


def test_cli_enable_update_plane_wires_engine_and_client(monkeypatch, tmp_path):
    """--enable-update-plane with dirs: the CLI builds UpdateEngine over the
    given dirs and ExecutorIPCClient over the given socket path, and hands
    them to the composition seam (patched to a recorder)."""
    from aipm.cli import app as app_module
    from aipm.control_plane import composition as composition_module
    from aipm.control_plane import transport as transport_module

    audit_dir = tmp_path / "audit"
    backup_dir = tmp_path / "backups"

    captured: dict = {}

    def _fake_serve(**kwargs):
        captured.update(kwargs)

        class _StubService:
            pass

        return {"service": _StubService(), "database_path": tmp_path / "cp.db"}

    engine_instances = []

    class _StubUpdateEngine:
        def __init__(self, audit_service=None, backup_engine=None):
            self.audit_service = audit_service
            self.backup_engine = backup_engine
            engine_instances.append(self)

    # The CLI imports serve_operator_transport from composition inside the
    # command body → patch the composition module attribute (call-time bind).
    monkeypatch.setattr(composition_module, "serve_operator_transport", _fake_serve)
    monkeypatch.setattr("aipm.services.update.engine.UpdateEngine", _StubUpdateEngine)

    socket_path = tmp_path / "executor.sock"
    result = CliRunner().invoke(
        app_module.app,
        [
            "serve-operator-transport",
            "--enable-update-plane",
            "--update-audit-dir", str(audit_dir),
            "--update-backup-dir", str(backup_dir),
            "--executor-socket-path", str(socket_path),
        ],
    )
    assert result.exit_code == 0, result.output

    assert len(engine_instances) == 1
    engine = engine_instances[0]
    assert str(engine.audit_service.audit_dir) == str(audit_dir)
    assert str(engine.backup_engine.backup_dir) == str(backup_dir)
    assert captured["update_engine"] is engine
    assert isinstance(captured["executor_ipc_client"], ExecutorIPCClient)
    assert captured["executor_ipc_client"]._socket_path == str(socket_path)
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8789


def test_cli_update_plane_defaults_socket_path(monkeypatch, tmp_path):
    """Omitted --executor-socket-path composes the canonical
    EXECUTOR_SOCKET_PATH client (production default)."""
    from aipm.cli import app as app_module
    from aipm.control_plane import composition as composition_module

    captured: dict = {}

    def _fake_serve(**kwargs):
        captured.update(kwargs)

        class _StubService:
            pass

        return {"service": _StubService(), "database_path": tmp_path / "cp.db"}

    class _StubUpdateEngine:
        def __init__(self, audit_service=None, backup_engine=None):
            pass

    monkeypatch.setattr(composition_module, "serve_operator_transport", _fake_serve)
    monkeypatch.setattr("aipm.services.update.engine.UpdateEngine", _StubUpdateEngine)

    result = CliRunner().invoke(
        app_module.app,
        [
            "serve-operator-transport",
            "--enable-update-plane",
            "--update-audit-dir", str(tmp_path / "audit"),
            "--update-backup-dir", str(tmp_path / "backups"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert isinstance(captured["executor_ipc_client"], ExecutorIPCClient)
    assert captured["executor_ipc_client"]._socket_path == EXECUTOR_SOCKET_PATH


def test_cli_default_refuses_partial_flags(tmp_path):
    """--update-audit-dir without --enable-update-plane → exit 2."""
    from aipm.cli.app import app

    result = CliRunner().invoke(
        app,
        ["serve-operator-transport", "--update-audit-dir", str(tmp_path / "audit")],
    )
    assert result.exit_code == 2
    assert "require --enable-update-plane" in result.output


def test_cli_enable_update_plane_requires_dirs(tmp_path):
    """--enable-update-plane without dirs → exit 2 before any engine build."""
    from aipm.cli.app import app

    result = CliRunner().invoke(
        app,
        ["serve-operator-transport", "--enable-update-plane"],
        env={"AIPM_OWNER_ARGON2ID_VERIFIER": SECRET},
    )
    assert result.exit_code == 2
    assert "requires both --update-audit-dir and --update-backup-dir" in result.output


def test_cli_probe_failure_refuses_startup(tmp_path, monkeypatch):
    """Unwritable audit dir → exit 2, engine never built, no composition."""
    import os

    from aipm.cli.app import app

    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    audit_dir.chmod(0o500)
    backup_dir = tmp_path / "backups"

    built = []
    monkeypatch.setattr(
        "aipm.services.update.engine.UpdateEngine",
        lambda **kwargs: built.append(kwargs) or object(),
    )

    try:
        result = CliRunner().invoke(
            app,
            [
                "serve-operator-transport",
                "--enable-update-plane",
                "--update-audit-dir", str(audit_dir),
                "--update-backup-dir", str(backup_dir),
            ],
            env={"AIPM_OWNER_ARGON2ID_VERIFIER": SECRET},
        )
    finally:
        audit_dir.chmod(0o700)

    assert result.exit_code == 2
    assert "not writable" in result.output
    assert built == []


# ---------------------------------------------------------------------------
# Seam 3: digest space + runtime crossing (runbook P0-gap requirement)
# ---------------------------------------------------------------------------


def test_engine_digest_port_speaks_updateplanidentity(tmp_path):
    """With an engine, current_plan_digest returns the canonical
    UpdatePlanIdentity digest over the engine's dry_run=False plan —
    NOT the ProjectPlan.canonical_digest space."""
    from aipm.control_plane.composition import compose_operator_service

    plan = _make_plan()
    engine = _StubPlanEngine(plan)
    composition = compose_operator_service(
        database_path=tmp_path / "control_plane.db",
        verifier=VERIFIER,
        clock=_Clock(NOW),
        allowed_targets=frozenset({PROJECT_ID}),
        run_sweep=False,
        update_engine=engine,
        executor_ipc_client=_RecordingClient(),
    )
    service = composition["service"]

    expected = UpdatePlanIdentity.from_plan(_make_plan(dry_run=False)).digest()
    assert service._current_plan_digest(PROJECT_ID) == expected

    # The digest port re-planned through the engine in execution mode.
    assert engine.plan_calls == [(PROJECT_ID, False)]


def test_service_attrs_bound_engine_and_client(tmp_path):
    """With engine + client composed, the service binds the
    UpdatePlanIdentity digest port, an update runtime, and the executor
    IPC client attr."""
    from aipm.control_plane.composition import compose_operator_service

    composition = compose_operator_service(
        database_path=tmp_path / "control_plane.db",
        verifier=VERIFIER,
        clock=_Clock(NOW),
        allowed_targets=frozenset({PROJECT_ID}),
        run_sweep=False,
        update_engine=_StubPlanEngine(_make_plan()),
        executor_ipc_client=_RecordingClient(),
    )
    service = composition["service"]

    assert callable(service._current_plan_digest)
    assert service._update_runtime is not None
    assert isinstance(service._executor_ipc_client, _RecordingClient)


def test_service_attrs_bound_engine_without_client(tmp_path):
    """Engine without client: digest port bound + in-process runtime bound,
    executor IPC client attr stays None (composition contract; the ipc-mode
    execution boundary still refuses without a client)."""
    from aipm.control_plane.composition import compose_operator_service

    composition = compose_operator_service(
        database_path=tmp_path / "control_plane.db",
        verifier=VERIFIER,
        clock=_Clock(NOW),
        allowed_targets=frozenset({PROJECT_ID}),
        run_sweep=False,
        update_engine=_StubPlanEngine(_make_plan()),
    )
    service = composition["service"]

    assert callable(service._current_plan_digest)
    assert service._update_runtime is not None
    assert service._executor_ipc_client is None


def test_no_engine_digest_port_reads_project_plan(tmp_path):
    """Without engine (default), the digest port reads the durable
    ProjectPlan.canonical_digest space and update_runtime stays None."""
    from aipm.control_plane.composition import compose_operator_service
    from aipm.control_plane.project_plan import Environment, ProjectPlan

    composition = compose_operator_service(
        database_path=tmp_path / "control_plane.db",
        verifier=VERIFIER,
        clock=_Clock(NOW),
        allowed_targets=frozenset({PROJECT_ID}),
        run_sweep=False,
    )
    service = composition["service"]
    plans = composition["plans"]

    plans.create(
        ProjectPlan.create(
            target_id=PROJECT_ID,
            environment=Environment.STAGING,
            title="Old title",
            objective="Objective",
            now=NOW,
        )
    )
    digest = service._current_plan_digest(PROJECT_ID)
    assert isinstance(digest, str) and len(digest) == 64

    expected = plans.read(PROJECT_ID).canonical_digest
    assert digest == expected
    assert service._update_runtime is None
    assert service._executor_ipc_client is None
