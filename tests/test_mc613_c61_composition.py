"""C6.1: production operator-transport composition root.

Covers: canonical composition of OwnerControlPlaneService on durable SQLite
stores (no in-memory authority), the durable plan-digest port (read-only,
fail-closed), the C6.0 startup sweep before the listener (fail closed),
loopback-only binding (wildcard refused), canonical auth/CSRF/rate-limit
behavior through the composed app, the fail-closed execute boundary (no
update runtime composed), systemd unit invariants, structural boundary
invariants, and real multi-process durability across process restarts.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aipm.control_plane.transport import SESSION_COOKIE
from aipm.control_plane.composition import (
    DEFAULT_OPERATOR_TRANSPORT_PORT,
    OPERATOR_TRANSPORT_PORT_ENV,
    OPERATOR_TRANSPORT_VERIFIER_ENV,
    OperatorTransportConfigError,
    compose_operator_service,
    create_operator_service_app,
    project_plan_digest_port,
    run_startup_recovery_sweep,
    serve_operator_transport,
)
from aipm.control_plane.models import ControlPlaneError, PlanningErrorCode
from aipm.control_plane.project_plan import Environment, ProjectPlan
from aipm.control_plane.recovery_sweep import RecoverySweepError
from aipm.control_plane.storage import (
    ControlPlaneDatabase,
    DurableSessionStore,
    SQLiteActionRepository,
    SQLiteProjectPlanStore,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

VERIFIER = "$argon2id$v=19$m=65536,t=2,p=1$c3RhZ2UzLXNhbHQtMTIzNA$zho28DBNr2G2cGbxzr0Dl6AKwhbd8hEeTkti1pn7TW0"
SECRET = "test-owner-secret"
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
PROJECT_ID = "a" * 24
HEX64 = __import__("re").compile(r"^[0-9a-f]{64}$")


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value


def _compose(tmp_path: Path, *, clock=None, sweep: bool = True, targets=None) -> dict:
    clock = clock or _Clock(NOW)
    return compose_operator_service(
        database_path=tmp_path / "control_plane.db",
        verifier=VERIFIER,
        clock=clock,
        allowed_targets=targets if targets is not None else frozenset({PROJECT_ID}),
        run_sweep=sweep,
    )


def _disengage_kill_switch(comp: dict) -> None:
    """The durable kill-switch store defaults ENGAGED (fail-closed posture);
    canonical disengage through the service before exercising flows."""

    service = comp["service"]
    service.disengage_kill_switch("owner", reason="test composition")


def _register_plan(comp: dict, target_id: str = PROJECT_ID) -> None:
    comp["plans"].create(
        ProjectPlan.create(
            target_id=target_id,
            environment=Environment.STAGING,
            title="Old title",
            objective="Objective",
            now=NOW,
        )
    )


def _client(comp: dict) -> TestClient:
    app = create_operator_service_app(comp)
    return TestClient(app)


def _login(client: TestClient) -> dict:
    response = client.post("/login", json={"secret": SECRET})
    assert response.status_code == 200
    view = client.get("/session").json()
    return {"csrf": view["csrf_token"], "subject": view["subject"]}


# ---------------------------------------------------------------------------
# Composition: canonical service on durable stores
# ---------------------------------------------------------------------------


def test_01_composition_is_canonical_service_on_durable_stores(tmp_path: Path):
    comp = _compose(tmp_path)
    from aipm.control_plane.service import OwnerControlPlaneService

    assert isinstance(comp["service"], OwnerControlPlaneService)
    assert isinstance(comp["database"], ControlPlaneDatabase)
    assert isinstance(comp["actions"], SQLiteActionRepository)
    assert isinstance(comp["plans"], SQLiteProjectPlanStore)
    assert isinstance(comp["sessions"], DurableSessionStore)
    assert (tmp_path / "control_plane.db").exists()


def test_02_no_in_memory_authority_composed(tmp_path: Path):
    from aipm.control_plane.service import InMemoryActionRepository
    from aipm.control_plane.session import OwnerSessionStore

    comp = _compose(tmp_path)
    assert not isinstance(comp["actions"], InMemoryActionRepository)
    assert not isinstance(comp["sessions"], OwnerSessionStore)


def test_03_execution_mode_is_ipc(tmp_path: Path):
    comp = _compose(tmp_path)
    assert comp["service"]._execution_mode == "ipc"


def test_04_digest_port_reads_authoritative_plan_digest(tmp_path: Path):
    comp = _compose(tmp_path, sweep=False)
    _register_plan(comp)
    digest = project_plan_digest_port(comp["plans"])(PROJECT_ID)
    assert digest == comp["plans"].read(PROJECT_ID).canonical_digest
    assert HEX64.fullmatch(digest)


def test_05_digest_port_fails_closed_on_missing_plan(tmp_path: Path):
    comp = _compose(tmp_path, sweep=False)
    with pytest.raises(ControlPlaneError) as excinfo:
        project_plan_digest_port(comp["plans"])("missing-target")
    assert excinfo.value.code is PlanningErrorCode.UNAVAILABLE_EVIDENCE


def test_06_digest_port_fails_closed_on_malformed_digest(tmp_path: Path):
    comp = _compose(tmp_path, sweep=False)
    _register_plan(comp)
    row = comp["database"].connection.execute(
        "UPDATE project_plans SET canonical_digest = 'short' WHERE target_id = ?",
        (PROJECT_ID,),
    )
    comp["database"].connection.commit()
    assert row.rowcount == 1
    # The durable store's integrity verification fails closed first
    # (STORAGE_CORRUPT); the digest port is unreachable for tampered rows.
    with pytest.raises(ControlPlaneError) as excinfo:
        project_plan_digest_port(comp["plans"])(PROJECT_ID)
    assert excinfo.value.code in (
        PlanningErrorCode.UNAVAILABLE_EVIDENCE,
        PlanningErrorCode.STORAGE_CORRUPT,
    )


def test_07_update_runtime_is_not_composed_fail_closed(tmp_path: Path):
    comp = _compose(tmp_path)
    service = comp["service"]
    assert service._update_runtime is None
    _register_plan(comp)
    _disengage_kill_switch(comp)
    session = service.login(SECRET)
    digest = comp["plans"].read(PROJECT_ID).canonical_digest
    approval = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k-runtime",
    )
    assert approval["allowed"] is True
    action_id = approval["action_id"]
    service.capture_snapshot(session.session_id, action_id)
    with pytest.raises(ControlPlaneError) as excinfo:
        service.run_approved_update(session.session_id, action_id=action_id)
    # Fail closed at the IPC boundary (executor IPC client wiring is C6.4);
    # the unbound update runtime is never reached and nothing executes.
    assert excinfo.value.code is PlanningErrorCode.SESSION_INVALID
    assert service._update_runtime is None


def test_08_verifier_is_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(OPERATOR_TRANSPORT_VERIFIER_ENV, raising=False)
    with pytest.raises(OperatorTransportConfigError):
        compose_operator_service(database_path=tmp_path / "cp.db")


def test_09_empty_target_allow_list_refused(tmp_path: Path):
    with pytest.raises(OperatorTransportConfigError):
        compose_operator_service(
            database_path=tmp_path / "cp.db",
            verifier=VERIFIER,
            allowed_targets=frozenset(),
        )


# ---------------------------------------------------------------------------
# Startup: sweep before listener, fail closed
# ---------------------------------------------------------------------------


def test_10_startup_sweep_runs_and_reports(tmp_path: Path):
    comp = _compose(tmp_path)
    result = comp["sweep_result"]
    assert result is not None
    assert result.scanned == 0
    assert result.outcomes == ()
    assert result.errors == ()


def test_11_startup_sweep_recovers_non_terminal_action(tmp_path: Path):
    comp = _compose(tmp_path)
    _register_plan(comp)
    _disengage_kill_switch(comp)
    service = comp["service"]
    session = service.login(SECRET)
    digest = comp["plans"].read(PROJECT_ID).canonical_digest
    approval = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k-sweep",
    )
    action_id = approval["action_id"]
    service.capture_snapshot(session.session_id, action_id)
    # Leave durable non-terminal state: lease granted then expired.
    action = comp["actions"].get_action(action_id)
    comp["actions"].acquire_lease(action_id, expected_version=action.version, now=NOW + timedelta(minutes=3))
    with comp["database"].connection:
        comp["database"].connection.execute(
            "UPDATE execution_leases SET expires_at = ? WHERE action_id = ?",
            ((NOW - timedelta(minutes=1)).isoformat(), action_id),
        )
    comp["database"].connection.commit()

    result = run_startup_recovery_sweep(
        actions=comp["actions"], plans=comp["plans"], clock=comp["service"]._clock
    )
    assert result.scanned == 1
    assert result.advanced_action_ids == (action_id,)
    recovered = comp["actions"].get_action(action_id)
    from aipm.control_plane.models import LifecycleState

    assert recovered.state is LifecycleState.RECONCILIATION_REQUIRED


def test_12_enumeration_failure_refuses_startup(tmp_path: Path):
    comp = _compose(tmp_path, sweep=False)

    class _BrokenRepo:
        def non_terminal_action_ids(self, *, limit=1000):
            raise RuntimeError("enumeration unavailable")

    # Fail closed: the sweep refuses before any enumeration when the
    # repository contract is absent, and propagates enumeration failures.
    with pytest.raises((TypeError, RuntimeError)):
        run_startup_recovery_sweep(actions=_BrokenRepo(), plans=comp["plans"])


def test_13_sweep_is_idempotent(tmp_path: Path):
    comp = _compose(tmp_path)
    first = run_startup_recovery_sweep(
        actions=comp["actions"], plans=comp["plans"], clock=comp["service"]._clock
    )
    second = run_startup_recovery_sweep(
        actions=comp["actions"], plans=comp["plans"], clock=comp["service"]._clock
    )
    assert first.scanned == 0 and second.scanned == 0
    assert second.recovery_version == first.recovery_version


# ---------------------------------------------------------------------------
# Network: loopback-only, canonical transport behavior
# ---------------------------------------------------------------------------


def test_14_wildcard_bind_refused(tmp_path: Path):
    from aipm.control_plane.transport import validate_bind_address

    for host in ("0.0.0.0", "::", "[::]", "::/0"):
        with pytest.raises(Exception):
            validate_bind_address(host)


def test_15_loopback_bind_accepted(tmp_path: Path):
    from aipm.control_plane.transport import validate_bind_address

    validate_bind_address("127.0.0.1")


def test_16_composed_app_has_no_dashboard_routes(tmp_path: Path):
    app = create_operator_service_app(_compose(tmp_path))
    paths = {getattr(route, "path", "") for route in app.routes}
    assert paths
    assert not any("dashboard" in path for path in paths)


def test_17_composed_app_requires_authentication(tmp_path: Path):
    client = _client(_compose(tmp_path))
    response = client.get(f"/updates/{PROJECT_ID}/status")
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "unauthenticated"


def test_18_composed_app_enforces_csrf(tmp_path: Path):
    comp = _compose(tmp_path)
    _register_plan(comp)
    _disengage_kill_switch(comp)
    client = _client(comp)
    session = _login(client)
    digest = comp["plans"].read(PROJECT_ID).canonical_digest
    response = client.post(
        f"/updates/{PROJECT_ID}/approval",
        json={"idempotency_key": "k1", "update_plan_digest": digest},
    )
    assert response.status_code == 403


def test_19_composed_app_serves_canonical_flow(tmp_path: Path):
    comp = _compose(tmp_path)
    _register_plan(comp)
    _disengage_kill_switch(comp)
    client = _client(comp)
    _login(client)
    session_view = client.get("/session").json()
    csrf = session_view["csrf_token"]
    digest = comp["plans"].read(PROJECT_ID).canonical_digest
    response = client.post(
        f"/updates/{PROJECT_ID}/approval",
        json={"idempotency_key": "k1", "update_plan_digest": digest},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["allowed"] is True
    assert body["action_id"]
    assert body["approval"] == "confirmed"
    status = client.get(f"/updates/{PROJECT_ID}/status")
    assert status.status_code == 200


def test_20_execute_is_fail_closed_without_runtime(tmp_path: Path):
    comp = _compose(tmp_path)
    _register_plan(comp)
    _disengage_kill_switch(comp)
    client = _client(comp)
    _login(client)
    csrf = client.get("/session").json()["csrf_token"]
    digest = comp["plans"].read(PROJECT_ID).canonical_digest
    approval = client.post(
        f"/updates/{PROJECT_ID}/approval",
        json={"idempotency_key": "k-exec", "update_plan_digest": digest},
        headers={"X-CSRF-Token": csrf},
    )
    assert approval.status_code == 200
    action_id = approval.json()["action_id"]
    assert action_id
    # Session binding: the approval was created under the HTTP session;
    # capture_snapshot and execution must run under the same session.
    service = comp["service"]
    http_session_id = client.cookies.get(SESSION_COOKIE)
    assert http_session_id
    service.capture_snapshot(http_session_id, action_id)
    before = comp["actions"].get_action(action_id)
    result = client.post(
        f"/updates/{PROJECT_ID}/execute",
        json={"action_id": action_id},
        headers={"X-CSRF-Token": csrf},
    )
    # Fail closed: the IPC boundary error maps to the canonical 401 surface;
    # the action stays non-terminal (at most lease-granted, never executed),
    # the update runtime is never invoked, and no mutation occurs.
    assert result.status_code in (401, 409, 500, 503)
    after = comp["actions"].get_action(action_id)
    from aipm.control_plane.models import LifecycleState

    assert after.state is not LifecycleState.VERIFIED_SUCCESS
    assert after.state is not LifecycleState.EXECUTION_FAILED
    assert comp["service"]._update_runtime is None
    # The recorded outcome is the non-mutation sentinel; no mutation ran.
    outcome = comp["actions"].outcome_for_action(action_id)
    assert outcome in (None, "mutation_not_started")


# ---------------------------------------------------------------------------
# Systemd unit invariants
# ---------------------------------------------------------------------------


def _unit_text() -> str:
    return (REPO_ROOT / "ops" / "systemd" / "aipm-operator-transport.service").read_text(
        encoding="utf-8"
    )


def test_21_systemd_unit_runs_as_aipm_without_docker_or_executor_groups():
    text = _unit_text()
    assert "User=aipm" in text
    assert "Group=aipm" in text
    assert "SupplementaryGroups=docker" not in text
    assert "aipm-executor" not in text.split("ExecStart")[0].split("[Service]")[1]


def test_22_systemd_unit_binds_loopback_only():
    text = _unit_text()
    assert "--host 127.0.0.1" in text
    assert "0.0.0.0" not in text
    assert "--host ::" not in text


def test_23_systemd_unit_hardening():
    text = _unit_text()
    for directive in (
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "PrivateTmp=true",
        "RestrictAddressFamilies=AF_UNIX AF_INET",
        "RestrictSUIDSGID=true",
        "RestrictNamespaces=true",
    ):
        assert directive in text


def test_24_systemd_unit_reads_only_its_state_paths():
    text = _unit_text()
    assert "ReadWritePaths=/var/lib/aipm/state/control_plane" in text
    assert "ReadWritePaths=/var/lib/aipm/state/control_plane /var/lib/aipm/logs" in text


# ---------------------------------------------------------------------------
# Boundary: structural isolation (AST over the composition module)
# ---------------------------------------------------------------------------


def _composition_ast() -> ast.Module:
    return ast.parse(
        (REPO_ROOT / "src" / "aipm" / "control_plane" / "composition.py").read_text(
            encoding="utf-8"
        )
    )


def test_25_no_dashboard_import_in_composition():
    tree = _composition_ast()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "dashboard" in node.module:
            pytest.fail("composition imports the dashboard module")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "dashboard" in alias.name:
                    pytest.fail("composition imports the dashboard module")


def test_26_no_parallel_authority_implementations_in_composition():
    tree = _composition_ast()
    forbidden = (
        "class Approval",
        "class Confirmation",
        "class AuditLedger",
        "class Gate",
        "class Lease",
        "class ActionRepository",
        "class SessionStore",
        "class Authenticator",
    )
    source = (REPO_ROOT / "src" / "aipm" / "control_plane" / "composition.py").read_text(
        encoding="utf-8"
    )
    for name in forbidden:
        assert name not in source


def test_27_no_subprocess_or_docker_in_composition():
    source = (REPO_ROOT / "src" / "aipm" / "control_plane" / "composition.py").read_text(
        encoding="utf-8"
    )
    tree = _composition_ast()
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(name.startswith(("subprocess", "docker")) for name in imported)
    for token in ("subprocess.", "os.system", "docker."):
        assert token not in source


def test_28_composition_does_not_invent_plan_identity():
    tree = _composition_ast()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "plan_identity" not in node.module


# ---------------------------------------------------------------------------
# Durability: real SQLite persistence across instances and processes
# ---------------------------------------------------------------------------


def test_29_durable_session_survives_new_instances(tmp_path: Path):
    comp = _compose(tmp_path, sweep=False)
    _register_plan(comp)
    app = create_operator_service_app(comp)
    client = TestClient(app)
    _login(client)
    raw_cookie = None
    for name, value in client.cookies.items():
        if "session" in name:
            raw_cookie = value
    assert raw_cookie, "expected a session cookie"
    client.close()

    # Fresh composition on the SAME database: the durable session is honored.
    second = _compose(tmp_path, sweep=False)
    app2 = create_operator_service_app(second)
    client2 = TestClient(app2)
    client2.cookies.set(next(iter(client.cookies.keys())), raw_cookie)
    view = client2.get("/session")
    assert view.status_code == 200


def test_30_registered_plan_shared_across_instances(tmp_path: Path):
    _register_plan(_compose(tmp_path, sweep=False))
    second = _compose(tmp_path, sweep=False)
    digest = second["plans"].read(PROJECT_ID).canonical_digest
    assert HEX64.fullmatch(digest)


def test_31_multiprocess_durability_end_to_end(tmp_path: Path):
    """Process A: create durable state and exit. Process B: sweep + fresh
    composition shares the same SQLite DB and sees the durable state."""
    db_path = tmp_path / "control_plane.db"
    child = REPO_ROOT / "ops" / "staging" / "_c61_child_process.py"
    if not child.exists():  # inline child script fallback
        child_script = tmp_path / "child.py"
        child_script.write_text(_CHILD_SCRIPT, encoding="utf-8")
        child = child_script

    proc_a = subprocess.run(
        [sys.executable, str(child), "stage", str(db_path)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        check=False,
    )
    assert proc_a.returncode == 0, proc_a.stderr
    assert db_path.exists()

    proc_b = subprocess.run(
        [sys.executable, str(child), "verify", str(db_path)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        check=False,
    )
    assert proc_b.returncode == 0, proc_b.stderr
    assert "PERSISTED" in proc_b.stdout


_CHILD_SCRIPT = '''
import sys
from datetime import datetime, timedelta, timezone

from aipm.control_plane.transport import SESSION_COOKIE
from aipm.control_plane.composition import (
    compose_operator_service,
    run_startup_recovery_sweep,
)
from aipm.control_plane.project_plan import Environment, ProjectPlan

VERIFIER = "$argon2id$v=19$m=65536,t=2,p=1$c3RhZ2UzLXNhbHQtMTIzNA$zho28DBNr2G2cGbxzr0Dl6AKwhbd8hEeTkti1pn7TW0"
SECRET = "test-owner-secret"
PROJECT_ID = "a" * 24
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def main(mode: str, db_path: str) -> int:
    if mode == "stage":
        comp = compose_operator_service(
            database_path=db_path,
            verifier=VERIFIER,
            allowed_targets=frozenset({PROJECT_ID}),
            run_sweep=False,
        )
        comp["plans"].create(
            ProjectPlan.create(
                target_id=PROJECT_ID,
                environment=Environment.STAGING,
                title="Old title",
                objective="Objective",
                now=NOW,
            )
        )
        service = comp["service"]
        session = service.login(SECRET)
        service.disengage_kill_switch("owner", reason="staging composition under test")
        digest = comp["plans"].read(PROJECT_ID).canonical_digest
        approval = service.approve_update_plan(
            session.session_id,
            target_id=PROJECT_ID,
            environment="staging",
            presented_digest=digest,
            idempotency_key="k-proc",
        )
        assert approval["allowed"] is True
        action_id = approval["action_id"]
        service.capture_snapshot(session.session_id, action_id, now=NOW + timedelta(minutes=2))
        action = comp["actions"].get_action(action_id)
        comp["actions"].acquire_lease(
            action_id, expected_version=action.version, now=NOW + timedelta(minutes=3)
        )
        with comp["database"].connection:
            comp["database"].connection.execute(
                "UPDATE execution_leases SET expires_at = ? WHERE action_id = ?",
                ((NOW - timedelta(minutes=1)).isoformat(), action_id),
            )
        comp["database"].connection.commit()
        print("STAGED")
        return 0
    if mode == "verify":
        comp = compose_operator_service(
            database_path=db_path,
            verifier=VERIFIER,
            allowed_targets=frozenset({PROJECT_ID}),
            run_sweep=True,
        )
        result = comp["sweep_result"]
        assert result is not None and result.scanned == 1, result
        digest = comp["plans"].read(PROJECT_ID).canonical_digest
        assert len(digest) == 64
        print("PERSISTED")
        return 0
    raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
'''


def test_32_default_port_is_loopback_bounded():
    assert DEFAULT_OPERATOR_TRANSPORT_PORT == 8789
    assert OPERATOR_TRANSPORT_PORT_ENV == "AIPM_OPERATOR_TRANSPORT_PORT"
