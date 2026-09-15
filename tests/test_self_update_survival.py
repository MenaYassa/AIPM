import urllib.error
from unittest.mock import MagicMock
import pytest
from aipm.providers.systemd_observation import SystemdObservationProvider, SystemdUnitObservation
from aipm.services.update.systemd_verifier import SystemdVerificationStatus, SystemdVerifier


def test_dashboard_self_update_survival_and_recovery():
    """Simulates aipm-dashboard.service restarting mid-verification.

    The executor/verifier remains alive, observes the intermediate transition
    (activating -> active with new InvocationID), tolerates temporary connection
    refusal while the new process binds port 8787, and finally verifies healthz.
    """
    pre_obs = SystemdUnitObservation(
        unit_name="aipm-dashboard.service",
        load_state="loaded",
        active_state="active",
        sub_state="running",
        unit_file_state="enabled",
        working_directory="/home/ubuntu/aipm",
        exec_start=None,
        user="aipm",
        fragment_path=None,
        primary_id="aipm-dashboard.service",
        invocation_id="dashboard-pid1-old",
        active_enter_timestamp="1000",
        restart_count=3,
    )

    # Supervisor states sampled across polling intervals
    state_samples = [
        # 1. Old process still terminating
        SystemdUnitObservation(
            unit_name="aipm-dashboard.service",
            load_state="loaded",
            active_state="deactivating",
            sub_state="stop-sigterm",
            unit_file_state="enabled",
            working_directory="/home/ubuntu/aipm",
            exec_start=None,
            user="aipm",
            fragment_path=None,
            primary_id="aipm-dashboard.service",
            invocation_id="dashboard-pid1-old",
            active_enter_timestamp="1000",
            restart_count=3,
        ),
        # 2. New process activating
        SystemdUnitObservation(
            unit_name="aipm-dashboard.service",
            load_state="loaded",
            active_state="activating",
            sub_state="start",
            unit_file_state="enabled",
            working_directory="/home/ubuntu/aipm",
            exec_start=None,
            user="aipm",
            fragment_path=None,
            primary_id="aipm-dashboard.service",
            invocation_id="dashboard-pid1-new",
            active_enter_timestamp="2000",
            restart_count=4,
        ),
        # 3. New process running
        SystemdUnitObservation(
            unit_name="aipm-dashboard.service",
            load_state="loaded",
            active_state="active",
            sub_state="running",
            unit_file_state="enabled",
            working_directory="/home/ubuntu/aipm",
            exec_start=None,
            user="aipm",
            fragment_path=None,
            primary_id="aipm-dashboard.service",
            invocation_id="dashboard-pid1-new",
            active_enter_timestamp="2000",
            restart_count=4,
        ),
    ]

    mock_obs_provider = MagicMock(spec=SystemdObservationProvider)
    mock_obs_provider.observe_unit.side_effect = state_samples

    # HTTP probe samples: connection refused first (port not bound yet), then HTTP 200 OK
    http_attempts = [
        urllib.error.URLError(ConnectionRefusedError("Connection refused")),
        MagicMock(status=200, __enter__=lambda s: s, __exit__=lambda s, *a: None),
    ]

    def mock_http(req, **kwargs):
        res = http_attempts.pop(0)
        if isinstance(res, Exception):
            raise res
        return res

    verifier = SystemdVerifier(
        observation_provider=mock_obs_provider,
        http_client=mock_http,
    )

    result = verifier.verify_systemd_update(
        unit_name="aipm-dashboard.service",
        pre_observation=pre_obs,
        health_probe_contract="http:http://127.0.0.1:8787/healthz",
        supervisor_timeout_seconds=5.0,
        probe_timeout_seconds=5.0,
    )

    assert result.status == SystemdVerificationStatus.SUCCESS
    assert result.passed is True
    assert result.supervisor_passed is True
    assert result.probe_passed is True
    assert any("InvocationID" in d for d in result.details)


def test_dashboard_self_update_fails_closed_when_disappeared():
    """If target dashboard service disappears or fails, outcome is failure, never success."""
    pre_obs = SystemdUnitObservation(
        unit_name="aipm-dashboard.service",
        load_state="loaded",
        active_state="active",
        sub_state="running",
        unit_file_state="enabled",
        working_directory="/home/ubuntu/aipm",
        exec_start=None,
        user="aipm",
        fragment_path=None,
        primary_id="aipm-dashboard.service",
        invocation_id="dashboard-pid1-old",
        active_enter_timestamp="1000",
        restart_count=3,
    )

    # Unit crashed after restart
    failed_obs = SystemdUnitObservation(
        unit_name="aipm-dashboard.service",
        load_state="loaded",
        active_state="failed",
        sub_state="failed",
        unit_file_state="enabled",
        working_directory="/home/ubuntu/aipm",
        exec_start=None,
        user="aipm",
        fragment_path=None,
        primary_id="aipm-dashboard.service",
        invocation_id="dashboard-pid1-new",
        active_enter_timestamp="2000",
        restart_count=4,
    )

    mock_obs_provider = MagicMock(spec=SystemdObservationProvider)
    mock_obs_provider.observe_unit.return_value = failed_obs

    verifier = SystemdVerifier(observation_provider=mock_obs_provider)
    result = verifier.verify_systemd_update(
        unit_name="aipm-dashboard.service",
        pre_observation=pre_obs,
        supervisor_timeout_seconds=2.0,
    )

    assert result.status == SystemdVerificationStatus.SUPERVISOR_FAILURE
    assert result.passed is False
    assert "entered failed state" in result.error
