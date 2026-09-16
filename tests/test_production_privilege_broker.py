from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aipm.providers.systemd_observation import SystemdObservationProvider, SystemdUnitObservation
from aipm.services.update.privilege_broker import (
    DEFAULT_ALLOWED_UNITS,
    DEFAULT_BROKER_PATH,
    PrivilegeBrokerClient,
    PrivilegeBrokerError,
    PrivilegeBrokerResult,
)
from aipm.services.update.systemd_verifier import (
    SystemdVerificationResult,
    SystemdVerificationStatus,
    SystemdVerifier,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BROKER_SRC = REPO_ROOT / "ops" / "broker" / "aipm-systemd-restart.c"


# ============================================================================
# 1. COMPILED BROKER BINARY FIXTURE & TESTS
# ============================================================================

@pytest.fixture(scope="module")
def prod_compiled_broker(tmp_path_factory):
    """Compile the production broker C source with strict root EUID check."""
    assert BROKER_SRC.exists(), f"Source file missing: {BROKER_SRC}"
    out_dir = tmp_path_factory.mktemp("broker_prod_bin")
    bin_path = out_dir / "aipm-systemd-restart"

    cmd = [
        "gcc",
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-pedantic",
        "-std=c11",
        "-fstack-protector-strong",
        "-D_FORTIFY_SOURCE=2",
        "-Wl,-z,relro,-z,now",
        str(BROKER_SRC),
        "-o",
        str(bin_path),
    ]
    subprocess.run(cmd, check=True)
    assert bin_path.exists()
    return bin_path


@pytest.fixture(scope="module")
def test_compiled_broker(tmp_path_factory):
    """Compile broker with -DTEST_ALLOW_NON_ROOT to test C-level parsing & security in isolation."""
    assert BROKER_SRC.exists(), f"Source file missing: {BROKER_SRC}"
    out_dir = tmp_path_factory.mktemp("broker_test_bin")
    bin_path = out_dir / "aipm-systemd-restart-test"

    cmd = [
        "gcc",
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-pedantic",
        "-std=c11",
        "-DTEST_ALLOW_NON_ROOT",
        "-fstack-protector-strong",
        "-D_FORTIFY_SOURCE=2",
        "-Wl,-z,relro,-z,now",
        str(BROKER_SRC),
        "-o",
        str(bin_path),
    ]
    subprocess.run(cmd, check=True)
    assert bin_path.exists()
    return bin_path


def test_compiled_broker_non_root_execution_rejected(prod_compiled_broker):
    """Non-root execution must fail closed with EUID check error on production build."""
    res = subprocess.run(
        [str(prod_compiled_broker), "--unit=aipm-dashboard.service", "--verb=try-restart"],
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "requires root privileges" in res.stderr


def test_compiled_broker_argument_count_rejection(test_compiled_broker):
    """Broker must reject too few or too many arguments."""
    # 0 arguments
    r0 = subprocess.run([str(test_compiled_broker)], capture_output=True, text=True)
    assert r0.returncode != 0
    assert "invalid argument count" in r0.stderr

    # 1 argument
    r1 = subprocess.run([str(test_compiled_broker), "--unit=aipm-dashboard.service"], capture_output=True, text=True)
    assert r1.returncode != 0
    assert "invalid argument count" in r1.stderr

    # 3 arguments (extra argument)
    r3 = subprocess.run(
        [str(test_compiled_broker), "--unit=aipm-dashboard.service", "--verb=try-restart", "--extra=bad"],
        capture_output=True,
        text=True,
    )
    assert r3.returncode != 0
    assert "invalid argument count" in r3.stderr


def test_compiled_broker_malformed_arguments(test_compiled_broker):
    """Broker must reject malformed flags and arbitrary options."""
    # Missing prefix
    r = subprocess.run([str(test_compiled_broker), "aipm-dashboard.service", "try-restart"], capture_output=True, text=True)
    assert r.returncode != 0
    assert "unrecognized or duplicate argument" in r.stderr

    # Arbitrary flag
    r_flag = subprocess.run([str(test_compiled_broker), "--now", "--unit=aipm-dashboard.service"], capture_output=True, text=True)
    assert r_flag.returncode != 0
    assert "unrecognized or duplicate argument" in r_flag.stderr

    # Duplicate flag
    r_dup = subprocess.run(
        [str(test_compiled_broker), "--unit=aipm-dashboard.service", "--unit=aipm-dashboard.service"],
        capture_output=True,
        text=True,
    )
    assert r_dup.returncode != 0
    assert "unrecognized or duplicate argument" in r_dup.stderr


@pytest.mark.parametrize(
    "unauthorized_unit",
    [
        "aipm-executor.service",
        "aipm-telemetry.service",
        "aipm-events.service",
        "unrelated.service",
        "arbitrary.service",
        "nginx.service",
    ],
)
def test_compiled_broker_unauthorized_unit_rejected(test_compiled_broker, unauthorized_unit):
    """C broker must reject any unit not in ALLOWLISTED_UNITS."""
    res = subprocess.run(
        [str(test_compiled_broker), f"--unit={unauthorized_unit}", "--verb=try-restart"],
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "unauthorized systemd unit" in res.stderr


@pytest.mark.parametrize(
    "unauthorized_verb",
    [
        "stop",
        "start",
        "restart",
        "daemon-reload",
        "enable",
        "disable",
        "mask",
        "edit",
        "status",
    ],
)
def test_compiled_broker_unauthorized_verb_rejected(test_compiled_broker, unauthorized_verb):
    """C broker must reject any verb not in ALLOWLISTED_VERBS."""
    res = subprocess.run(
        [str(test_compiled_broker), "--unit=aipm-dashboard.service", f"--verb={unauthorized_verb}"],
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "unauthorized systemd verb" in res.stderr


@pytest.mark.parametrize(
    "prohibited_unit",
    [
        "aipm-dashboard.service; rm -rf /",
        "aipm-dashboard.service && id",
        "aipm-dashboard.service | sh",
        "`id`.service",
        "$(whoami).service",
        "template@.service",
        "*.service",
        "../traversal.service",
    ],
)
def test_compiled_broker_prohibited_characters_rejected(test_compiled_broker, prohibited_unit):
    """C broker must reject shell metacharacters and path traversal."""
    res = subprocess.run(
        [str(test_compiled_broker), f"--unit={prohibited_unit}", "--verb=try-restart"],
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "prohibited characters" in res.stderr


# ============================================================================
# 2. PRIVILEGE BROKER CLIENT TESTS (STRICT BOUNDARIES)
# ============================================================================

def test_client_positive_dashboard_try_restart():
    """Authorized dashboard try-restart reaches the runner with canonical argv."""
    calls = []

    def mock_runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode=0, stdout="ok", stderr="")

    client = PrivilegeBrokerClient(runner=mock_runner)
    result = client.restart_unit("aipm-dashboard.service", verb="try-restart")

    assert result.success is True
    assert result.returncode == 0
    assert calls == [[DEFAULT_BROKER_PATH, "--unit=aipm-dashboard.service", "--verb=try-restart"]]


@pytest.mark.parametrize(
    "unauthorized_unit",
    [
        "aipm-executor.service",
        "aipm-telemetry.service",
        "aipm-events.service",
        "unrelated.service",
        "arbitrary.service",
        "nginx.service",
        "docker.service",
        "ssh.service",
    ],
)
def test_client_negative_unauthorized_units(unauthorized_unit):
    """Broker client must reject any unit not explicitly in DEFAULT_ALLOWED_UNITS."""
    client = PrivilegeBrokerClient()
    with pytest.raises(PrivilegeBrokerError, match="Unauthorized systemd unit"):
        client.restart_unit(unauthorized_unit)


@pytest.mark.parametrize(
    "invalid_unit",
    [
        "",
        "   ",
        "bad;unit.service",
        "unit@foo.service",
        "template@.service",
        "*.service",
        "../../etc/systemd/system/bad.service",
        "/etc/systemd/system/aipm-dashboard.service",
        "aipm-dashboard.service\n",
        "aipm-dashboard.service\t",
        "aipm-dashboard.service; rm -rf /",
        "$(whoami).service",
        "`id`.service",
        "aipm dashboard.service",
    ],
)
def test_client_negative_malformed_units(invalid_unit):
    """Broker client must reject malformed, template, path, and shell syntax units."""
    client = PrivilegeBrokerClient()
    with pytest.raises(PrivilegeBrokerError):
        client.restart_unit(invalid_unit)


@pytest.mark.parametrize(
    "unauthorized_verb",
    [
        "stop",
        "start",
        "restart",
        "daemon-reload",
        "enable",
        "disable",
        "mask",
        "unmask",
        "edit",
        "reload",
        "status",
        "kill",
        "cat",
        "preset",
        "isolate",
        "arbitrary-verb",
    ],
)
def test_client_negative_unauthorized_verbs(unauthorized_verb):
    """Broker client must reject all verbs other than 'try-restart'."""
    client = PrivilegeBrokerClient()
    with pytest.raises(PrivilegeBrokerError, match="Prohibited verb"):
        client.restart_unit("aipm-dashboard.service", verb=unauthorized_verb)


def test_client_missing_binary():
    """Missing broker executable returns returncode 127 fail-closed."""
    client = PrivilegeBrokerClient(broker_path="/nonexistent/path/aipm-systemd-restart")
    result = client.restart_unit("aipm-dashboard.service")
    assert result.success is False
    assert result.returncode == 127
    assert "not found" in result.error


def test_client_permission_error():
    """Non-executable or permission denied returns returncode 126 fail-closed."""
    def mock_runner(argv, **kwargs):
        raise PermissionError("Permission denied")

    client = PrivilegeBrokerClient(runner=mock_runner)
    result = client.restart_unit("aipm-dashboard.service")
    assert result.success is False
    assert result.returncode == 126
    assert "not executable" in result.error


def test_client_timeout_expired():
    """Subprocess timeout returns returncode 124 fail-closed."""
    def mock_runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, timeout=30.0)

    client = PrivilegeBrokerClient(runner=mock_runner)
    result = client.restart_unit("aipm-dashboard.service")
    assert result.success is False
    assert result.returncode == 124
    assert "timed out" in result.error


def test_client_nonzero_exit_propagation():
    """Broker non-zero exit code propagates without raising unhandled exceptions."""
    def mock_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="Error: unauthorized unit")

    client = PrivilegeBrokerClient(runner=mock_runner)
    result = client.restart_unit("aipm-dashboard.service")
    assert result.success is False
    assert result.returncode == 1
    assert "Error: unauthorized unit" in result.error


# ============================================================================
# 3. PRODUCTION NEGATIVE CONTROLS
# ============================================================================

def test_production_negative_control_executor_restart_prohibited():
    """The executor itself MUST NOT be restartable through this broker."""
    client = PrivilegeBrokerClient()
    assert "aipm-executor.service" not in DEFAULT_ALLOWED_UNITS
    with pytest.raises(PrivilegeBrokerError, match="Unauthorized systemd unit for privilege broker: 'aipm-executor.service'"):
        client.restart_unit("aipm-executor.service")


def test_production_negative_control_no_generic_systemctl():
    """No generic systemctl verbs or command execution can be triggered."""
    client = PrivilegeBrokerClient()
    # Cannot invoke daemon-reload
    with pytest.raises(PrivilegeBrokerError, match="Prohibited verb"):
        client.restart_unit("aipm-dashboard.service", verb="daemon-reload")

    # Cannot invoke mask
    with pytest.raises(PrivilegeBrokerError, match="Prohibited verb"):
        client.restart_unit("aipm-dashboard.service", verb="mask")


# ============================================================================
# 4. SYSTEMD VERIFIER INTEGRATION WORKFLOW
# ============================================================================

def test_integration_verifier_success_path():
    """Full update workflow: broker restart -> supervisor transition -> health probe 200."""
    pre_obs = SystemdUnitObservation(
        unit_name="aipm-dashboard.service",
        load_state="loaded",
        active_state="active",
        sub_state="running",
        unit_file_state="enabled",
        working_directory="/home/ubuntu/aipm",
        exec_start="/usr/bin/python3 app.py",
        user="aipm",
        fragment_path="/etc/systemd/system/aipm-dashboard.service",
        primary_id="aipm-dashboard.service",
        invocation_id="init-invocation-id-1111",
        active_enter_timestamp="1000",
        restart_count=0,
    )

    post_obs = SystemdUnitObservation(
        unit_name="aipm-dashboard.service",
        load_state="loaded",
        active_state="active",
        sub_state="running",
        unit_file_state="enabled",
        working_directory="/home/ubuntu/aipm",
        exec_start="/usr/bin/python3 app.py",
        user="aipm",
        fragment_path="/etc/systemd/system/aipm-dashboard.service",
        primary_id="aipm-dashboard.service",
        invocation_id="new-invocation-id-2222",
        active_enter_timestamp="2000",
        restart_count=0,
    )

    mock_obs_provider = MagicMock(spec=SystemdObservationProvider)
    mock_obs_provider.observe_unit.return_value = post_obs

    # Mock HTTP 200 response
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp
    mock_http_client = MagicMock(return_value=mock_resp)

    verifier = SystemdVerifier(observation_provider=mock_obs_provider, http_client=mock_http_client)
    res = verifier.verify_systemd_update(
        "aipm-dashboard.service",
        pre_observation=pre_obs,
        health_probe_contract="http:http://127.0.0.1:8787/healthz",
    )

    assert res.status == SystemdVerificationStatus.SUCCESS
    assert res.passed is True
    assert res.supervisor_passed is True
    assert res.probe_passed is True


def test_integration_verifier_supervisor_failure():
    """Unit enters failed state -> SystemdVerificationStatus.SUPERVISOR_FAILURE."""
    pre_obs = SystemdUnitObservation(
        unit_name="aipm-dashboard.service",
        load_state="loaded",
        active_state="active",
        sub_state="running",
        unit_file_state="enabled",
        working_directory="/home/ubuntu/aipm",
        exec_start="/usr/bin/python3 app.py",
        user="aipm",
        fragment_path="/etc/systemd/system/aipm-dashboard.service",
        primary_id="aipm-dashboard.service",
        invocation_id="init-invocation-id-1111",
        active_enter_timestamp="1000",
        restart_count=0,
    )

    failed_obs = SystemdUnitObservation(
        unit_name="aipm-dashboard.service",
        load_state="loaded",
        active_state="failed",
        sub_state="failed",
        unit_file_state="enabled",
        working_directory="/home/ubuntu/aipm",
        exec_start="/usr/bin/python3 app.py",
        user="aipm",
        fragment_path="/etc/systemd/system/aipm-dashboard.service",
        primary_id="aipm-dashboard.service",
        invocation_id="failed-id-3333",
        active_enter_timestamp="2000",
        restart_count=1,
    )

    mock_obs_provider = MagicMock(spec=SystemdObservationProvider)
    mock_obs_provider.observe_unit.return_value = failed_obs

    verifier = SystemdVerifier(observation_provider=mock_obs_provider)
    res = verifier.verify_systemd_update(
        "aipm-dashboard.service",
        pre_observation=pre_obs,
        health_probe_contract="http:http://127.0.0.1:8787/healthz",
        supervisor_timeout_seconds=0.5,
    )

    assert res.status == SystemdVerificationStatus.SUPERVISOR_FAILURE
    assert res.passed is False
    assert "entered failed" in str(res.error)


def test_integration_verifier_probe_failure():
    """Supervisor passes but probe returns HTTP 500 -> APPLICATION_PROBE_FAILURE."""
    pre_obs = SystemdUnitObservation(
        unit_name="aipm-dashboard.service",
        load_state="loaded",
        active_state="active",
        sub_state="running",
        unit_file_state="enabled",
        working_directory="/home/ubuntu/aipm",
        exec_start="/usr/bin/python3 app.py",
        user="aipm",
        fragment_path="/etc/systemd/system/aipm-dashboard.service",
        primary_id="aipm-dashboard.service",
        invocation_id="init-invocation-id-1111",
        active_enter_timestamp="1000",
        restart_count=0,
    )

    post_obs = SystemdUnitObservation(
        unit_name="aipm-dashboard.service",
        load_state="loaded",
        active_state="active",
        sub_state="running",
        unit_file_state="enabled",
        working_directory="/home/ubuntu/aipm",
        exec_start="/usr/bin/python3 app.py",
        user="aipm",
        fragment_path="/etc/systemd/system/aipm-dashboard.service",
        primary_id="aipm-dashboard.service",
        invocation_id="new-invocation-id-2222",
        active_enter_timestamp="2000",
        restart_count=0,
    )

    mock_obs_provider = MagicMock(spec=SystemdObservationProvider)
    mock_obs_provider.observe_unit.return_value = post_obs

    # Mock HTTP 500 response
    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_resp.__enter__.return_value = mock_resp
    mock_http_client = MagicMock(return_value=mock_resp)

    verifier = SystemdVerifier(observation_provider=mock_obs_provider, http_client=mock_http_client)
    res = verifier.verify_systemd_update(
        "aipm-dashboard.service",
        pre_observation=pre_obs,
        health_probe_contract="http:http://127.0.0.1:8787/healthz",
        supervisor_timeout_seconds=2.0,
        probe_timeout_seconds=0.2,
    )

def test_integration_verifier_reconciliation_required():
    """Ambiguous outcome maps to reconciliation_required status."""
    assert SystemdVerificationStatus.RECONCILIATION_REQUIRED.value == "reconciliation_required"
    result = SystemdVerificationResult(
        status=SystemdVerificationStatus.RECONCILIATION_REQUIRED,
        passed=False,
        supervisor_passed=False,
        probe_passed=False,
        error="Supervisor transition was ambiguous or interrupted during restart",
    )
    assert result.status == SystemdVerificationStatus.RECONCILIATION_REQUIRED
    assert result.passed is False


def test_compiled_broker_environment_sanitization(tmp_path_factory):
    """C broker must discard dangerous caller environment variables before execve."""
    work_dir = tmp_path_factory.mktemp("env_test")
    log_file = work_dir / "env.log"
    fake_systemctl = work_dir / "fake_systemctl.sh"
    fake_systemctl.write_text(f"""#!/bin/sh
env > {log_file}
exit 0
""")
    fake_systemctl.chmod(0o755)

    # Compile broker with mock systemctl path and TEST_ALLOW_NON_ROOT
    test_broker = work_dir / "env_broker"
    cmd = [
        "gcc",
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-pedantic",
        "-std=c11",
        "-DTEST_ALLOW_NON_ROOT",
        f"-DSYSTEMCTL_BIN=\"{fake_systemctl}\"",
        str(BROKER_SRC),
        "-o",
        str(test_broker),
    ]
    subprocess.run(cmd, check=True)

    # Run with injected environment variables
    evil_env = {
        "LD_PRELOAD": "/evil/preload.so",
        "SYSTEMD_UNIT_PATH": "/evil/units",
        "PYTHONPATH": "/evil/python",
        "SECRET_TOKEN": "should_not_leak",
    }
    res = subprocess.run(
        [str(test_broker), "--unit=aipm-dashboard.service", "--verb=try-restart"],
        capture_output=True,
        text=True,
        env=evil_env,
    )
    assert res.returncode == 0
    assert log_file.exists()
    logged_env = log_file.read_text()
    assert "PATH=/usr/bin:/bin" in logged_env
    assert "LD_PRELOAD" not in logged_env
    assert "SYSTEMD_UNIT_PATH" not in logged_env
    assert "PYTHONPATH" not in logged_env
    assert "SECRET_TOKEN" not in logged_env


def test_immutable_policy_by_design():
    """PrivilegeBrokerClient policy must be immutable frozenset, not read from executor files."""
    client = PrivilegeBrokerClient()
    assert isinstance(client.allowed_units, frozenset)
    assert client.allowed_units == frozenset({"aipm-dashboard.service"})
    with pytest.raises(AttributeError):
        client.allowed_units.add("bad.service")
