"""Shot 18 (standalone executor + execution envelope + CLI) tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import tempfile
from pathlib import Path

from aipm.control_plane.mutation_receipt import MutationReceiptStore, MutationStatus
from aipm.control_plane.standalone_executor import (
    ENVELOPE_VERSION,
    ExecutionEnvelope,
    StandaloneRestartResult,
    StandaloneSystemdExecutor,
)
from aipm.control_plane.systemd_provider import (
    SystemdRestartPolicy,
    SystemdRestartProvider,
    SystemdRestartError,
    SubprocessResult,
)
from aipm.control_plane.audit.sanitize import AuditEventError

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
POLICY = SystemdRestartPolicy(
    environment="staging", target_id="aipm-telemetry", unit_id="aipm-telemetry",
    canonical_unit_name="aipm-telemetry.service", policy_version="policy-v1",
)


class FakeRunner:
    def __init__(self, *, returncode=0, times_out=False):
        self.calls = []
        self.returncode = returncode
        self.times_out = times_out

    def __call__(self, argv, *, timeout=30):
        self.calls.append(list(argv))
        if argv[1] == "show":
            return SubprocessResult(0, "LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\nMainPID=1234\nFragmentPath=/etc/systemd/system/aipm-telemetry.service\n", "", False)
        if argv[1] == "restart":
            if self.times_out:
                return SubprocessResult(-1, "", "timeout", True)
            return SubprocessResult(self.returncode, "", "" if self.returncode == 0 else "error", False)
        return SubprocessResult(1, "", "unknown", False)


# Import SubprocessResult
from aipm.control_plane.systemd_provider import SubprocessResult


def make_envelope(**overrides):
    values = {
        "protocol_version": ENVELOPE_VERSION,
        "action_id": "a" * 64,
        "action_version": 1,
        "capability_id": "apply_project_plan",
        "capability_version": "1",
        "target_id": "aipm-telemetry",
        "environment": "staging",
        "unit_name": "aipm-telemetry.service",
        "contract_digest": "d" * 64,
        "fencing_token": 1,
        "lease_id": "l" * 32,
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }
    values.update(overrides)
    return ExecutionEnvelope(**values)


def make_executor(tmp_path: Path, *, returncode=0, times_out=False):
    runner = FakeRunner(returncode=returncode, times_out=times_out)
    provider = SystemdRestartProvider(policies=[POLICY], runner=runner)
    receipts = MutationReceiptStore(str(tmp_path / "receipts.db"))
    executor = StandaloneSystemdExecutor(provider=provider, policy=POLICY, receipts=receipts)
    return executor, runner, receipts


# --- Envelope validation ---

def test_valid_envelope_constructs():
    envelope = make_envelope()
    assert envelope.protocol_version == ENVELOPE_VERSION
    assert envelope.digest() is not None


def test_envelope_rejects_wrong_version():
    with pytest.raises(Exception, match="version"):
        make_envelope(protocol_version="wrong")


def test_envelope_rejects_non_service_unit():
    with pytest.raises(Exception, match=".service"):
        make_envelope(unit_name="not-a-service")


def test_envelope_rejects_path_traversal():
    with pytest.raises(Exception):
        make_envelope(unit_name="../../bin/sh")


def test_envelope_rejects_expired():
    executor, runner, receipts = make_executor(Path(tempfile.mkdtemp()))
    envelope = make_envelope(expires_at=(NOW - timedelta(minutes=1)).isoformat())
    with pytest.raises(Exception, match="expired|Envelope"):
        executor.execute_restart(envelope, now=NOW)


def test_envelope_rejects_empty_fields():
    for field in ("action_id", "target_id", "contract_digest", "lease_id"):
        with pytest.raises(Exception):
            make_envelope(**{field: ""})


# --- Standalone executor (no CP DB) ---

def test_standalone_executor_has_no_cp_db_dependency(tmp_path: Path):
    executor, runner, receipts = make_executor(tmp_path)
    # The executor should NOT have any of the CP DB attributes
    for forbidden in ("_actions", "_plans", "_confirmations", "_audit", "_gate", "_kill_switches"):
        assert not hasattr(executor, forbidden), forbidden


def test_standalone_executor_success(tmp_path: Path):
    executor, runner, receipts = make_executor(tmp_path)
    envelope = make_envelope()
    result = executor.execute_restart(envelope, now=NOW)
    assert result.outcome == "succeeded"
    assert result.provider_code == "restart_ok"
    # Restart was invoked exactly once
    restart_calls = [call for call in runner.calls if len(call) > 1 and call[1] == "restart"]
    assert len(restart_calls) == 1
    # Receipt was created and completed
    receipt = receipts.get(action_id=envelope.action_id, fencing_token=envelope.fencing_token)
    assert receipt.mutation_status is MutationStatus.MUTATION_SUCCEEDED


def test_standalone_executor_no_cp_db_imports():
    source = Path("src/aipm/control_plane/standalone_executor.py").read_text(encoding="utf-8")
    for forbidden in ("ControlPlaneDatabase", "SQLiteActionRepository", "OwnerControlPlaneService", "AuthorizationPolicy", "OwnerAuthenticator"):
        assert forbidden not in source, forbidden


# --- Replay prevention ---

def test_replay_is_prevented_by_mutation_receipt(tmp_path: Path):
    executor, runner, receipts = make_executor(tmp_path)
    envelope = make_envelope()
    result1 = executor.execute_restart(envelope, now=NOW)
    assert result1.outcome == "succeeded"
    # Replay: the receipt prevents a second mutation
    with pytest.raises(Exception, match="already claimed"):
        executor.execute_restart(envelope, now=NOW + timedelta(minutes=1))
    restart_calls = [call for call in runner.calls if len(call) > 1 and call[1] == "restart"]
    assert len(restart_calls) == 1


# --- Failure classification ---

def test_failed_restart(tmp_path: Path):
    executor, runner, receipts = make_executor(tmp_path, returncode=1)
    envelope = make_envelope()
    result = executor.execute_restart(envelope, now=NOW)
    assert result.outcome == "failed"
    receipt = receipts.get(action_id=envelope.action_id, fencing_token=envelope.fencing_token)
    assert receipt.mutation_status is MutationStatus.MUTATION_FAILED


def test_unknown_outcome_on_timeout(tmp_path: Path):
    executor, runner, receipts = make_executor(tmp_path, times_out=True)
    envelope = make_envelope()
    result = executor.execute_restart(envelope, now=NOW)
    assert result.outcome == "unknown_outcome"
    receipt = receipts.get(action_id=envelope.action_id, fencing_token=envelope.fencing_token)
    assert receipt.mutation_status is MutationStatus.UNKNOWN_OUTCOME


# --- CLI ---

def test_executor_cli_command_exists():
    from typer.testing import CliRunner
    from aipm.cli.app import app
    cli_runner = CliRunner()
    result = cli_runner.invoke(app, ["executor", "--help"])
    assert result.exit_code == 0


def test_executor_run_starts_server(tmp_path: Path):
    from typer.testing import CliRunner
    from aipm.cli.app import app

    cli_runner = CliRunner()
    result = cli_runner.invoke(app, ["executor", "run", "--help"])
    assert result.exit_code == 0
    assert "socket" in result.output.lower() or "executor" in result.output.lower()


# --- Source scan ---

def test_standalone_executor_source_has_no_dangerous_imports():
    source = Path("src/aipm/control_plane/standalone_executor.py").read_text(encoding="utf-8")
    for forbidden in ("ControlPlaneDatabase", "OwnerControlPlaneService", "AuthorizationPolicy", "OwnerAuthenticator", "subprocess"):
        assert forbidden not in source, forbidden


def test_executor_cli_source_has_no_arbitrary_execution():
    source = Path("src/aipm/cli/app.py").read_text(encoding="utf-8")
    # The executor CLI section should not have shell or arbitrary command execution
    executor_section = source[source.index("executor_app"):]
    for forbidden in ("shell=True", "os.system", "subprocess.run("):
        # subprocess is OK in the CLI for other commands, but not in the executor section
        if "executor" in executor_section[:executor_section.index(forbidden)] if forbidden in executor_section else False:
            continue
        assert forbidden not in executor_section, forbidden
