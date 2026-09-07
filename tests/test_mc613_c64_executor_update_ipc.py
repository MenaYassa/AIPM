"""C6.4 — Executor IPC production composition tests.

Covers the production composition of the executor update capability:

* wire contract: optional engine binding fields, capability-gated
  validation, legacy byte-compatibility;
* the IPC-backed update runtime: bounded outcome mapping, transport
  failures never propagate past the terminal boundary;
* the executor-side update handler: exactly-once receipt keying, bounded
  classification (succeeded / failed / unknown_outcome / refused),
  duplicate refusal, receipt-store failure fail-closed, no-engine
  fail-closed, receipt durability across a process restart;
* CLI executor-run hardening: required UID allow-list, disabled-by-default
  update capability over a LIVE socket (real subprocess), audit-dir
  writability probe;
* one disposable full-path integration: canonical production composition
  (execution_mode="ipc") → gated execution → REAL unix socket → real
  receipt-gated handler → real update engine on a disposable repo.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aipm.control_plane.executor_ipc import (
    CAPABILITY_EXECUTE_UPDATE_PLAN,
    CAPABILITY_LEGACY_RESTART,
    ExecutionRequest,
    ExecutorIPCClient,
    ExecutorIPCServer,
)
from aipm.control_plane.mutation_receipt import MutationReceiptStore, MutationStatus
from aipm.composition.executor_update import (
    compose_executor_update_handler,
    compose_ipc_update_runtime,
)

from tests.test_mc612_stage9_transport import NOW, SECRET, VERIFIER, _Clock
from tests.update_fixtures import MARKER_RUNTIME_SCRIPT, make_repo

PROJECT_ID = "c64-update-demo"


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubClient:
    """ExecutorIPCClient stand-in: records the sent request."""

    def __init__(self, response=None, error=None):
        self.requests = []
        self.response = response
        self.error = error

    def send(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def _response(outcome="succeeded", provider_code="update_ok"):
    from aipm.control_plane.executor_ipc import ExecutionResponse

    return ExecutionResponse(
        outcome=outcome,
        provider_code=provider_code,
        action_id="a" * 64,
        evidence_reference="update-audit:/tmp/audit/x.json",
    )


def _binding():
    from aipm.control_plane.models import UpdateExecutionBinding

    return UpdateExecutionBinding(
        project_name=PROJECT_ID,
        plan_digest="b" * 64,
        confirmation_id="d" * 32,
        action_id="a" * 64,
        contract_digest="c" * 64,
        lease_id="e" * 32,
        fencing_token=7,
    )


def _stub_engine(fn):
    """A canonical UpdateEngine instance (isinstance gate passes) whose
    execute_update is the given callable, without the heavy default
    service construction."""

    engine = UpdateEngine.__new__(UpdateEngine)
    engine.execute_update = fn  # type: ignore[method-assign]
    return engine


from aipm.services.update.engine import UpdateEngine  # noqa: E402


def _request(**overrides):
    values = {
        "action_id": "a" * 64,
        "capability_id": CAPABILITY_EXECUTE_UPDATE_PLAN,
        "target_id": PROJECT_ID,
        "contract_digest": "c" * 64,
        "lease_id": "l" * 32,
        "fencing_token": 1,
        "plan_digest": "b" * 64,
        "confirmation_id": "d" * 32,
    }
    values.update(overrides)
    return ExecutionRequest(**values)


def _success_audit(audit_dir: Path):
    from types import SimpleNamespace

    return SimpleNamespace(outcome="success", audit_path=audit_dir / "audit-1.json")


# ---------------------------------------------------------------------------
# Wire contract
# ---------------------------------------------------------------------------


def test_update_plan_request_roundtrips_binding_fields():
    parsed = ExecutionRequest.from_json(_request().to_json())
    assert parsed.plan_digest == "b" * 64
    assert parsed.confirmation_id == "d" * 32


def test_legacy_request_frame_omits_binding_fields():
    legacy = _request(capability_id=CAPABILITY_LEGACY_RESTART, plan_digest=None, confirmation_id=None)
    import json

    payload = json.loads(legacy.to_json())
    assert "plan_digest" not in payload
    assert "confirmation_id" not in payload
    parsed = ExecutionRequest.from_json(legacy.to_json())
    assert parsed.plan_digest is None and parsed.confirmation_id is None


def test_update_plan_requires_plan_digest_on_the_wire():
    import json

    payload = json.loads(_request().to_json())
    del payload["plan_digest"]
    with pytest.raises(Exception, match="plan_digest"):
        ExecutionRequest.from_json(json.dumps(payload).encode())


def test_update_plan_requires_confirmation_id_on_the_wire():
    import json

    payload = json.loads(_request().to_json())
    del payload["confirmation_id"]
    with pytest.raises(Exception, match="confirmation_id"):
        ExecutionRequest.from_json(json.dumps(payload).encode())


def test_binding_fields_are_hex_bounded():
    import json

    payload = json.loads(_request().to_json())
    payload["plan_digest"] = "Z" * 64
    with pytest.raises(Exception, match="plan_digest"):
        ExecutionRequest.from_json(json.dumps(payload).encode())
    payload = json.loads(_request().to_json())
    payload["confirmation_id"] = "zzzz" + "0" * 28
    with pytest.raises(Exception, match="confirmation_id"):
        ExecutionRequest.from_json(json.dumps(payload).encode())


# ---------------------------------------------------------------------------
# IPC-backed update runtime (control-plane side)
# ---------------------------------------------------------------------------


def test_runtime_sends_execute_update_plan_with_binding_evidence():
    client = _StubClient(response=_response())
    runtime = compose_ipc_update_runtime(client)
    result = runtime(_binding())
    assert len(client.requests) == 1
    sent = client.requests[0]
    assert sent.capability_id == CAPABILITY_EXECUTE_UPDATE_PLAN
    assert sent.action_id == "a" * 64
    assert sent.target_id == PROJECT_ID
    assert sent.contract_digest == "c" * 64
    assert sent.lease_id == "e" * 32
    assert sent.fencing_token == 7
    assert sent.plan_digest == "b" * 64
    assert sent.confirmation_id == "d" * 32
    assert result == {
        "outcome": "succeeded",
        "provider_code": "update_ok",
        "action_id": "a" * 64,
        "evidence_reference": "update-audit:/tmp/audit/x.json",
    }


def test_runtime_preserves_unknown_outcome():
    runtime = compose_ipc_update_runtime(
        _StubClient(response=_response(outcome="unknown_outcome", provider_code="timeout"))
    )
    result = runtime(_binding())
    assert result["outcome"] == "unknown_outcome"
    assert result["provider_code"] == "timeout"


def test_runtime_maps_transport_failure_to_unknown_outcome():
    for error in (ConnectionRefusedError("refused"), TimeoutError(), OSError("reset"), RuntimeError("boom")):
        runtime = compose_ipc_update_runtime(_StubClient(error=error))
        result = runtime(_binding())
        assert result["outcome"] == "unknown_outcome"
        assert result["provider_code"] == "transport_failure"
        assert result["action_id"] == "a" * 64


def test_runtime_maps_failed_outcome_verbatim():
    runtime = compose_ipc_update_runtime(
        _StubClient(response=_response(outcome="failed", provider_code="update_failed"))
    )
    result = runtime(_binding())
    assert result["outcome"] == "failed"
    assert result["provider_code"] == "update_failed"


# ---------------------------------------------------------------------------
# Executor-side update handler
# ---------------------------------------------------------------------------


def test_handler_requires_canonical_engine():
    with pytest.raises(TypeError, match="engine"):
        compose_executor_update_handler(engine=None, receipts=MutationReceiptStore(":memory:"))
    with pytest.raises(TypeError, match="engine"):
        compose_executor_update_handler(engine=object(), receipts=MutationReceiptStore(":memory:"))


def test_handler_requires_receipt_store():
    with pytest.raises(TypeError, match="receipts"):
        compose_executor_update_handler(engine=_stub_engine(lambda *a, **k: None), receipts=None)


def test_handler_refuses_unsupported_capability(tmp_path):
    store = MutationReceiptStore(str(tmp_path / "receipts.db"))
    handler = compose_executor_update_handler(
        engine=_stub_engine(lambda *a, **k: pytest.fail("engine must not run")), receipts=store
    )
    response = handler(_request(capability_id=CAPABILITY_LEGACY_RESTART, plan_digest=None, confirmation_id=None))
    assert response.outcome == "refused"
    assert response.provider_code == "unsupported_capability"
    assert store.count() == 0


def test_handler_refuses_malformed_binding_without_receipt(tmp_path):
    store = MutationReceiptStore(str(tmp_path / "receipts.db"))
    handler = compose_executor_update_handler(
        engine=_stub_engine(lambda *a, **k: pytest.fail("engine must not run")), receipts=store
    )
    bad_digest = handler(_request(plan_digest="z" * 64))
    assert bad_digest.outcome == "refused"
    assert bad_digest.provider_code == "invalid_plan_digest"
    bad_confirmation = handler(_request(confirmation_id="z" * 32))
    assert bad_confirmation.outcome == "refused"
    assert bad_confirmation.provider_code == "invalid_confirmation_id"
    assert store.count() == 0


def test_handler_happy_path_claims_and_completes_receipt(tmp_path):
    store = MutationReceiptStore(str(tmp_path / "receipts.db"))
    engine = _stub_engine(
        lambda project, **kwargs: (
            pytest.fail("engine must receive approve + contract") if kwargs.get("approve") is not True else _success_audit(tmp_path / "audit")
        )
    )
    handler = compose_executor_update_handler(engine=engine, receipts=store)
    response = handler(_request())
    assert response.outcome == "succeeded"
    assert response.provider_code == "update_ok"
    assert response.evidence_reference == f"update-audit:{tmp_path / 'audit' / 'audit-1.json'}"
    receipt = store.get(action_id="a" * 64, fencing_token=1)
    assert receipt.mutation_status is MutationStatus.MUTATION_SUCCEEDED

    # Duplicate delivery is refused without touching the engine again.
    counting = {"n": 0}

    def _counting(project, **kwargs):
        counting["n"] += 1
        return _success_audit(tmp_path / "audit")

    handler2 = compose_executor_update_handler(engine=_stub_engine(_counting), receipts=store)
    response2 = handler2(_request())
    assert response2.outcome == "refused"
    assert response2.provider_code == "already_claimed:mutation_succeeded"
    assert counting["n"] == 0


def test_handler_engine_failure_records_mutation_failed(tmp_path):
    from aipm.core.exceptions import UpdateError

    store = MutationReceiptStore(str(tmp_path / "receipts.db"))
    engine = _stub_engine(lambda project, **kwargs: pytest.fail("engine must not run") or (_ for _ in ()).throw(UpdateError("verification failed")))

    def _fail(project, **kwargs):
        raise UpdateError("verification failed")

    engine = _stub_engine(_fail)
    handler = compose_executor_update_handler(engine=engine, receipts=store)
    response = handler(_request())
    assert response.outcome == "failed"
    assert response.provider_code == "update_failed"
    receipt = store.get(action_id="a" * 64, fencing_token=1)
    assert receipt.mutation_status is MutationStatus.MUTATION_FAILED
    # A replay of the same (action, fence) is refused, never retried.
    response2 = handler(_request())
    assert response2.outcome == "refused"
    assert response2.provider_code == "already_claimed:mutation_failed"


def test_handler_engine_crash_records_unknown_outcome(tmp_path):
    store = MutationReceiptStore(str(tmp_path / "receipts.db"))

    def _crash(project, **kwargs):
        raise RuntimeError("mid-flight crash")

    handler = compose_executor_update_handler(engine=_stub_engine(_crash), receipts=store)
    response = handler(_request())
    assert response.outcome == "unknown_outcome"
    assert response.provider_code == "update_interrupted"
    receipt = store.get(action_id="a" * 64, fencing_token=1)
    assert receipt.mutation_status is MutationStatus.UNKNOWN_OUTCOME
    # UNKNOWN is never retried on replay.
    response2 = handler(_request())
    assert response2.outcome == "refused"
    assert response2.provider_code == "already_claimed:unknown_outcome"


def test_handler_non_success_engine_outcome_records_failed(tmp_path):
    from types import SimpleNamespace

    store = MutationReceiptStore(str(tmp_path / "receipts.db"))

    def _blocked(project, **kwargs):
        return SimpleNamespace(outcome="blocked", audit_path=None)

    handler = compose_executor_update_handler(engine=_stub_engine(_blocked), receipts=store)
    response = handler(_request())
    assert response.outcome == "failed"
    assert response.provider_code == "engine_outcome:blocked"
    receipt = store.get(action_id="a" * 64, fencing_token=1)
    assert receipt.mutation_status is MutationStatus.MUTATION_FAILED


def test_handler_receipt_store_failure_fails_closed_without_engine(tmp_path):
    class _BrokenStore:
        def claim(self, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        def complete(self, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        def get(self, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

    engine_ran = {"n": 0}

    def _must_not_run(project, **kwargs):
        engine_ran["n"] += 1
        return _success_audit(tmp_path / "audit")

    handler = compose_executor_update_handler(engine=_stub_engine(_must_not_run), receipts=_BrokenStore())
    response = handler(_request())
    assert response.outcome == "refused"
    assert response.provider_code == "receipt_store_unavailable"
    assert engine_ran["n"] == 0


def test_handler_classifies_every_engine_exception(tmp_path):
    store = MutationReceiptStore(str(tmp_path / "receipts.db"))

    def _boom(project, **kwargs):
        raise ValueError("unexpected engine bug")

    handler = compose_executor_update_handler(engine=_stub_engine(_boom), receipts=store)
    response = handler(_request())
    # Any Exception (not only UpdateError) must be classified, never
    # propagate past the single accept loop.
    assert response.outcome in {"failed", "unknown_outcome"}


def test_receipt_survives_executor_restart(tmp_path):
    db = str(tmp_path / "receipts.db")

    def _ok(project, **kwargs):
        return _success_audit(tmp_path / "audit")

    first = compose_executor_update_handler(engine=_stub_engine(_ok), receipts=MutationReceiptStore(db))
    response = first(_request())
    assert response.outcome == "succeeded"
    # A new process re-opens the same receipt database: the claim persists.
    reborn = compose_executor_update_handler(engine=_stub_engine(_ok), receipts=MutationReceiptStore(db))
    response2 = reborn(_request())
    assert response2.outcome == "refused"
    assert response2.provider_code == "already_claimed:mutation_succeeded"


# ---------------------------------------------------------------------------
# CLI executor run hardening
# ---------------------------------------------------------------------------


def _cli_runner():
    from typer.testing import CliRunner

    return CliRunner(env={"_TYPER_FORCE_DISABLE_TERMINAL": "1"})


def test_cli_executor_run_requires_caller_uids():
    from aipm.cli.app import app

    result = _cli_runner().invoke(
        app,
        ["executor", "run", "--socket-path", "/tmp/never-c64.sock", "--receipt-db", "/tmp/never-c64/receipts.db"],
    )
    assert result.exit_code != 0


def test_cli_executor_run_rejects_non_numeric_uids(tmp_path):
    from aipm.cli.app import app

    result = _cli_runner().invoke(
        app,
        ["executor", "run", "--socket-path", str(tmp_path / "x.sock"), "--receipt-db", str(tmp_path / "receipts.db"),
         "--allowed-caller-uids", "not-a-number"],
    )
    assert result.exit_code == 2


def test_cli_executor_run_rejects_negative_uids(tmp_path):
    from aipm.cli.app import app

    result = _cli_runner().invoke(
        app,
        ["executor", "run", "--socket-path", str(tmp_path / "x.sock"), "--receipt-db", str(tmp_path / "receipts.db"),
         "--allowed-caller-uids", "-5"],
    )
    assert result.exit_code == 2


def _launch_cli(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "from aipm.cli.app import app; app()", *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_cli_update_capability_refused_over_live_socket_when_disabled(tmp_path):
    """Deployment compatibility + fail-closed dispatch: a REAL executor
    process started without --enable-update-plan refuses an
    execute_update_plan request over the wire with capability_not_enabled."""

    sock = tmp_path / "cap.sock"
    proc = _launch_cli([
        "executor", "run",
        "--socket-path", str(sock),
        "--receipt-db", str(tmp_path / "receipts.db"),
        "--allowed-caller-uids", str(os.getuid()),
    ])
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if sock.exists():
                break
            if proc.poll() is not None:
                pytest.fail(f"executor CLI exited early with {proc.returncode}")
            time.sleep(0.05)
        else:
            pytest.fail("executor socket never appeared")
        client = ExecutorIPCClient(socket_path=str(sock))
        response = client.send(_request())
        assert response.outcome == "refused"
        assert response.provider_code == "capability_not_enabled"
    finally:
        proc.terminate()
        proc.wait(timeout=15)


@pytest.mark.skipif(os.geteuid() == 0, reason="writability probe cannot fail for root")
def test_cli_update_capability_startup_fails_closed_on_unwritable_audit_dir(tmp_path):
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    ro_dir.chmod(0o500)
    try:
        proc = _launch_cli([
            "executor", "run",
            "--socket-path", str(tmp_path / "s.sock"),
            "--receipt-db", str(tmp_path / "receipts.db"),
            "--allowed-caller-uids", str(os.getuid()),
            "--enable-update-plan",
            "--update-audit-dir", str(ro_dir / "audit"),
        ])
        rc = proc.wait(timeout=30)
        assert rc == 2
        assert not (tmp_path / "s.sock").exists()
    finally:
        ro_dir.chmod(0o700)


# ---------------------------------------------------------------------------
# Full disposable integration: canonical CP → gated execution → IPC → engine
# ---------------------------------------------------------------------------


def _build_engine(tmp_path: Path, project):
    from aipm.services.backup.engine import BackupEngine
    from aipm.services.update.audit import AuditService
    from aipm.services.update.rollback import RollbackManager
    from aipm.services.update.verifier import UpdateVerifier

    from tests.update_fixtures import FixedProjectService, GitService, GuardCompose, hermetic_health_engine

    git_service = GitService()
    return UpdateEngine(
        project_service=FixedProjectService(project, git_service),
        git_service=git_service,
        backup_engine=BackupEngine(tmp_path / "backups"),
        compose_provider=GuardCompose(),
        health_engine=hermetic_health_engine(),
        audit_service=AuditService(tmp_path / "executor-audit"),
        rollback_manager=RollbackManager(),
        verifier=UpdateVerifier(),
    )


def test_full_path_disposable_integration(tmp_path, monkeypatch):
    """One approved update runs end to end through the production
    composition: canonical gated execution reaches VERIFIED_SUCCESS, the
    IPC-backed runtime sends the durable binding over a REAL unix socket
    to the receipt-gated handler, and the real update engine executes
    against a disposable repo (marker written by the runtime subprocess
    OUTSIDE the worktree)."""

    from aipm.control_plane.composition import compose_operator_service
    from aipm.control_plane.project_plan import Environment, ProjectPlan
    from aipm.services.update.plan_identity import UpdatePlanIdentity

    clock = _Clock(NOW)
    project = make_repo(tmp_path, name=PROJECT_ID, runtime_script=MARKER_RUNTIME_SCRIPT)
    engine = _build_engine(tmp_path, project)

    # Executor side: real socket + real receipt store + the real engine.
    receipts = MutationReceiptStore(str(tmp_path / "executor" / "receipts.db"))
    handler = compose_executor_update_handler(engine=engine, receipts=receipts)
    sock_path = tmp_path / "executor.sock"
    server = ExecutorIPCServer(
        socket_path=str(sock_path), handler=handler, allowed_caller_uids={os.getuid()}
    )
    server.start()
    stop_event = threading.Event()
    serve_thread = threading.Thread(
        target=server.serve_forever, kwargs={"stop_event": stop_event}, daemon=True
    )
    serve_thread.start()

    # Control-plane side: canonical production composition (ipc mode) with
    # the IPC-backed runtime bound to the SAME engine for digest alignment.
    composition = compose_operator_service(
        database_path=tmp_path / "control_plane.db",
        verifier=VERIFIER,
        clock=clock,
        allowed_targets=frozenset({PROJECT_ID}),
        run_sweep=False,
        update_engine=engine,
        executor_ipc_client=ExecutorIPCClient(socket_path=str(sock_path)),
    )
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
    composition["kill_switches"].disengage(Environment.STAGING, reason="test window", now=NOW)
    service = composition["service"]

    session = service.login(SECRET)
    presented = UpdatePlanIdentity.from_plan(engine.plan_update(PROJECT_ID, dry_run=False)).digest()
    approval = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=presented,
        idempotency_key="k-c64-1",
    )
    assert approval["allowed"] is True

    monkeypatch.setenv("AIPM_INTEGRATION_MARKER", str(tmp_path / "runtime-marker.txt"))
    result = service.run_approved_update(session.session_id, action_id=approval["action_id"])
    assert result["executed"] is True
    assert result["lifecycle_state"] == "verified_success"

    # The real engine executed over IPC exactly once (marker written by the
    # runtime subprocess), and the executor receipt proves it durably.
    assert (tmp_path / "runtime-marker.txt").read_text(encoding="utf-8") == "started"
    lease = composition["actions"].last_lease(approval["action_id"])
    assert lease is not None
    receipt = receipts.get(action_id=approval["action_id"], fencing_token=lease.fencing_token)
    assert receipt is not None
    assert receipt.mutation_status is MutationStatus.MUTATION_SUCCEEDED

    # Canonical control-plane mutation: exactly one revision advance;
    # exactly-once confirmation consumption; lease released at terminal.
    assert plans.read(PROJECT_ID).revision == 2
    confirmation = service._confirmations.store.get(approval["confirmation_id"])
    assert confirmation.state.value == "consumed"
    assert service._actions.active_lease(approval["action_id"], now=clock()) is None

    stop_event.set()
    serve_thread.join(timeout=10)
    assert not serve_thread.is_alive()
    server.stop()
