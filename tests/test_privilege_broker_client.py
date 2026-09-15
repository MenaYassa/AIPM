import subprocess
import pytest
from aipm.services.update.privilege_broker import PrivilegeBrokerClient, PrivilegeBrokerError


def test_privilege_broker_client_args():
    calls = []

    def mock_runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode=0, stdout="ok", stderr="")

    client = PrivilegeBrokerClient(broker_path="/test/broker", runner=mock_runner)
    result = client.restart_unit("aipm-dashboard.service", verb="try-restart")
    assert result.success is True
    assert result.returncode == 0
    assert calls == [["/test/broker", "--unit=aipm-dashboard.service", "--verb=try-restart"]]


def test_privilege_broker_client_invalid_unit():
    client = PrivilegeBrokerClient()
    with pytest.raises(PrivilegeBrokerError, match="Invalid unit name"):
        client.restart_unit("bad;unit.service")
    with pytest.raises(PrivilegeBrokerError, match="Invalid unit name"):
        client.restart_unit("template@.service")
    with pytest.raises(PrivilegeBrokerError, match="Invalid unit name"):
        client.restart_unit("../traversal.service")


def test_privilege_broker_client_invalid_verb():
    client = PrivilegeBrokerClient()
    with pytest.raises(PrivilegeBrokerError, match="Prohibited verb"):
        client.restart_unit("aipm-dashboard.service", verb="stop")


def test_privilege_broker_missing_binary():
    client = PrivilegeBrokerClient(broker_path="/nonexistent/path/aipm-systemd-restart")
    result = client.restart_unit("aipm-dashboard.service")
    assert result.success is False
    assert result.returncode == 127
    assert "not found" in result.error
