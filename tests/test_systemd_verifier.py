from unittest.mock import MagicMock
import pytest
from aipm.providers.systemd_observation import SystemdObservationProvider, SystemdUnitObservation
from aipm.services.update.systemd_verifier import (
    SystemdVerificationStatus,
    SystemdVerifier,
)


def test_systemd_verifier_success():
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
        invocation_id="old-invocation-id",
        active_enter_timestamp="100",
        restart_count=1,
    )
    post_obs = SystemdUnitObservation(
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
        invocation_id="new-invocation-id",  # Changed!
        active_enter_timestamp="200",
        restart_count=2,
    )
    mock_obs_provider = MagicMock(spec=SystemdObservationProvider)
    mock_obs_provider.observe_unit.return_value = post_obs

    # Mock HTTP 200 response
    mock_http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None
    mock_http.return_value = mock_resp

    verifier = SystemdVerifier(
        observation_provider=mock_obs_provider,
        http_client=mock_http,
    )
    result = verifier.verify_systemd_update(
        unit_name="aipm-dashboard.service",
        pre_observation=pre_obs,
        health_probe_contract="http:http://127.0.0.1:8787/healthz",
        supervisor_timeout_seconds=2.0,
        probe_timeout_seconds=2.0,
    )
    assert result.status == SystemdVerificationStatus.SUCCESS
    assert result.passed is True
    assert result.supervisor_passed is True
    assert result.probe_passed is True


def test_systemd_verifier_fake_restart_detection():
    """If InvocationID did not change, verifier detects fake restart and fails."""
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
        invocation_id="same-invocation-id",
        active_enter_timestamp="100",
        restart_count=1,
    )
    post_obs = SystemdUnitObservation(
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
        invocation_id="same-invocation-id",  # Unchanged!
        active_enter_timestamp="100",
        restart_count=1,
    )
    mock_obs_provider = MagicMock(spec=SystemdObservationProvider)
    mock_obs_provider.observe_unit.return_value = post_obs

    verifier = SystemdVerifier(observation_provider=mock_obs_provider)
    result = verifier.verify_systemd_update(
        unit_name="aipm-dashboard.service",
        pre_observation=pre_obs,
        supervisor_timeout_seconds=0.5,
    )
    assert result.status == SystemdVerificationStatus.SUPERVISOR_FAILURE
    assert result.passed is False
    assert "InvocationID did not change" in result.error


def test_systemd_verifier_crash_loop_detection():
    """If restart count jumps excessively, crash loop is flagged immediately."""
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
        invocation_id="id-1",
        active_enter_timestamp="100",
        restart_count=1,
    )
    crash_obs = SystemdUnitObservation(
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
        invocation_id="id-5",
        active_enter_timestamp="100",
        restart_count=5,  # Jumped from 1 to 5
    )
    mock_obs_provider = MagicMock(spec=SystemdObservationProvider)
    mock_obs_provider.observe_unit.return_value = crash_obs

    verifier = SystemdVerifier(observation_provider=mock_obs_provider)
    result = verifier.verify_systemd_update(
        unit_name="aipm-dashboard.service",
        pre_observation=pre_obs,
        supervisor_timeout_seconds=0.5,
    )
    assert result.status == SystemdVerificationStatus.SUPERVISOR_FAILURE
    assert "Crash loop detected" in result.error


def test_systemd_verifier_probe_failure():
    """Supervisor passes but application health probe fails -> APPLICATION_PROBE_FAILURE."""
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
        invocation_id="old-id",
        active_enter_timestamp="100",
        restart_count=0,
    )
    post_obs = SystemdUnitObservation(
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
        invocation_id="new-id",
        active_enter_timestamp="200",
        restart_count=1,
    )
    mock_obs_provider = MagicMock(spec=SystemdObservationProvider)
    mock_obs_provider.observe_unit.return_value = post_obs

    # Mock HTTP 500 failure
    mock_http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None
    mock_http.return_value = mock_resp

    verifier = SystemdVerifier(
        observation_provider=mock_obs_provider,
        http_client=mock_http,
    )
    result = verifier.verify_systemd_update(
        unit_name="aipm-dashboard.service",
        pre_observation=pre_obs,
        health_probe_contract="http:http://127.0.0.1:8787/healthz",
        supervisor_timeout_seconds=2.0,
        probe_timeout_seconds=0.5,
    )
    assert result.status == SystemdVerificationStatus.APPLICATION_PROBE_FAILURE
    assert result.supervisor_passed is True
    assert result.probe_passed is False
    assert "Application health probe timed out" in result.error
