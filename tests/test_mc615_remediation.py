"""Tests verifying the MC-6.15 remediation changes.

Covers:
1. Executor run IPC handler dispatches CAPABILITY_EXECUTE_SERVICE_UPDATE to update_handler.
2. Operator transport CLI wires ComposeService into compose_operator_service.
3. Frontend uses deterministic idempotency keys and exports loadServicePlan.
"""

from __future__ import annotations

import re
from pathlib import Path
from typer.testing import CliRunner

from aipm.control_plane.executor_ipc import (
    CAPABILITY_EXECUTE_SERVICE_UPDATE,
    CAPABILITY_EXECUTE_UPDATE_PLAN,
    ExecutionRequest,
    ExecutionResponse,
)


def test_executor_handler_routes_service_update_capability(tmp_path, monkeypatch):
    """CAPABILITY_EXECUTE_SERVICE_UPDATE is routed to the update_handler."""
    from aipm.cli import app as app_module
    from aipm.composition import executor_update as comp_module

    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    receipt_db = tmp_path / "receipts.db"

    captured_requests = []

    def _mock_update_handler(request):
        captured_requests.append(request)
        return ExecutionResponse(
            outcome="success",
            provider_code="ok",
            action_id=request.action_id,
            evidence_reference="rec-123",
        )

    monkeypatch.setattr(
        comp_module,
        "compose_executor_update_handler",
        lambda **kwargs: _mock_update_handler,
    )

    server_instances = []

    class _MockServer:
        def __init__(self, socket_path, handler, **kwargs):
            self.socket_path = socket_path
            self.handler = handler
            server_instances.append(self)

        def start(self):
            pass

        def stop(self):
            pass

        def wait_stopped(self):
            pass

        def serve_forever(self, *, stop_event=None):
            pass

    monkeypatch.setattr("aipm.control_plane.executor_ipc.ExecutorIPCServer", _MockServer)

    runner = CliRunner()
    result = runner.invoke(
        app_module.app,
        [
            "executor",
            "run",
            "--allowed-caller-uids", "1000",
            "--receipt-db", str(receipt_db),
            "--enable-update-plan",
            "--update-audit-dir", str(audit_dir),
            "--socket-path", str(tmp_path / "test.sock"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(server_instances) == 1

    srv = server_instances[0]
    # Test dispatching CAPABILITY_EXECUTE_SERVICE_UPDATE
    req = ExecutionRequest(
        action_id="act-test-service",
        capability_id=CAPABILITY_EXECUTE_SERVICE_UPDATE,
        target_id="searxng",
        contract_digest="c" * 64,
        fencing_token="f" * 16,
        lease_id="l" * 16,
        action_protocol="mc616d2-v1",
    )
    resp = srv.handler(req)
    assert resp.outcome == "success"
    assert resp.action_id == "act-test-service"
    assert len(captured_requests) == 1
    assert captured_requests[0].capability_id == CAPABILITY_EXECUTE_SERVICE_UPDATE


def test_serve_operator_transport_wires_compose_service(tmp_path, monkeypatch):
    """CLI serve-operator-transport constructs and passes ComposeService."""
    from aipm.cli import app as app_module
    from aipm.control_plane import composition as comp_module

    audit_dir = tmp_path / "audit"
    backup_dir = tmp_path / "backups"
    audit_dir.mkdir(parents=True)
    backup_dir.mkdir(parents=True)

    captured_kwargs = {}

    def _mock_serve(**kwargs):
        captured_kwargs.update(kwargs)
        return {"service": None}

    monkeypatch.setattr(comp_module, "serve_operator_transport", _mock_serve)

    runner = CliRunner()
    result = runner.invoke(
        app_module.app,
        [
            "serve-operator-transport",
            "--enable-update-plane",
            "--update-audit-dir", str(audit_dir),
            "--update-backup-dir", str(backup_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "compose_service" in captured_kwargs
    assert captured_kwargs["compose_service"] is not None
    assert "service_plan_port" in captured_kwargs
    assert "service_evidence_verifier" in captured_kwargs


def test_serve_operator_transport_disabled_update_plane_startup_path(monkeypatch):
    """CLI serve-operator-transport without --enable-update-plane succeeds with capability-off locals."""
    from aipm.cli import app as app_module
    from aipm.control_plane import composition as comp_module

    captured_kwargs = {}

    def _mock_serve(**kwargs):
        captured_kwargs.update(kwargs)
        return {"service": None}

    monkeypatch.setattr(comp_module, "serve_operator_transport", _mock_serve)

    runner = CliRunner()
    result = runner.invoke(app_module.app, ["serve-operator-transport"])
    assert result.exit_code == 0, result.output
    assert captured_kwargs["update_engine"] is None
    assert captured_kwargs["executor_ipc_client"] is None
    assert captured_kwargs["compose_service"] is None
    assert captured_kwargs["service_plan_port"] is None
    assert captured_kwargs["service_evidence_verifier"] is None


def test_frontend_idempotency_and_service_plan_wiring():
    """Static checks for mission-control-projects.js idempotency and service update controls."""
    js_path = Path("src/aipm/dashboard/static/mission-control-projects.js")
    code = js_path.read_text(encoding="utf-8")

    # Idempotency key must not use non-deterministic Date.now()
    assert "Date.now()" not in code

    # Deterministic idempotency key pattern must be present
    assert "dashboard-" in code
    assert "digest.slice" in code

    # Service plan loading and window exports
    assert "loadServicePlan" in code
    assert "window.loadServicePlan = loadServicePlan" in code
    assert "window.loadServicePlan" in code

    # Table contains Action header and Plan Update button trigger
    assert "<th>Action</th>" in code
    assert "Plan Update" in code
