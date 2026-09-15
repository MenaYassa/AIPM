import subprocess
import pytest
from aipm.providers.systemd_observation import SystemdObservationProvider, SystemdTrustError


def test_observe_unit_success():
    def mock_runner(cmd, **kwargs):
        assert cmd[0] == "systemctl"
        assert cmd[1] == "show"
        assert cmd[2] == "my-service.service"
        output = """Id=my-service.service
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=enabled
WorkingDirectory=/home/ubuntu/app
ExecStart={ path=/home/ubuntu/app/bin/start ; argv[]=/home/ubuntu/app/bin/start }
User=ubuntu
FragmentPath=/etc/systemd/system/my-service.service
InvocationID=abcd1234efgh5678
ActiveEnterTimestampMonotonic=123456789
NRestarts=2
"""
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=output, stderr="")

    provider = SystemdObservationProvider(runner=mock_runner)
    obs = provider.observe_unit("my-service.service")
    assert obs.unit_name == "my-service.service"
    assert obs.load_state == "loaded"
    assert obs.active_state == "active"
    assert obs.sub_state == "running"
    assert obs.working_directory == "/home/ubuntu/app"
    assert obs.invocation_id == "abcd1234efgh5678"
    assert obs.restart_count == 2


def test_observe_unit_alias_rejection():
    def mock_runner(cmd, **kwargs):
        output = "Id=other-name.service\nLoadState=loaded\nActiveState=active\nSubState=running\n"
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=output, stderr="")

    provider = SystemdObservationProvider(runner=mock_runner)
    with pytest.raises(SystemdTrustError, match="alias/id mismatch"):
        provider.observe_unit("my-alias.service")


def test_observe_unit_invalid_name():
    provider = SystemdObservationProvider()
    with pytest.raises(SystemdTrustError, match="Invalid systemd unit name"):
        provider.observe_unit("invalid;rm -rf /")
    with pytest.raises(SystemdTrustError, match="Invalid systemd unit name"):
        provider.observe_unit("template@.service")
    with pytest.raises(SystemdTrustError, match="Invalid systemd unit name"):
        provider.observe_unit("../evil.service")


def test_validate_trust_path_provenance():
    def mock_runner(cmd, **kwargs):
        output = """Id=test.service
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=enabled
WorkingDirectory=/home/ubuntu/my-project
"""
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=output, stderr="")

    provider = SystemdObservationProvider(runner=mock_runner)
    valid, err, obs = provider.validate_trust("test.service", "/home/ubuntu/my-project")
    assert valid is True
    assert err is None

    # Mismatched path
    valid, err, obs = provider.validate_trust("test.service", "/var/other/project")
    assert valid is False
    assert "provenance mismatch" in err


def test_validate_trust_masked():
    def mock_runner(cmd, **kwargs):
        output = """Id=test.service
LoadState=loaded
ActiveState=inactive
SubState=dead
UnitFileState=masked
WorkingDirectory=/home/ubuntu/my-project
"""
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=output, stderr="")

    provider = SystemdObservationProvider(runner=mock_runner)
    valid, err, obs = provider.validate_trust("test.service", "/home/ubuntu/my-project")
    assert valid is False
    assert "masked" in err


def test_validate_trust_not_loaded():
    def mock_runner(cmd, **kwargs):
        output = """Id=test.service
LoadState=not-found
ActiveState=inactive
SubState=dead
UnitFileState=bad
"""
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=output, stderr="")

    provider = SystemdObservationProvider(runner=mock_runner)
    valid, err, obs = provider.validate_trust("test.service", "/home/ubuntu/my-project")
    assert valid is False
    assert "not loaded" in err
