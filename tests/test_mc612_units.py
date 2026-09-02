"""MC-6.12: systemd units allow AF_NETLINK for psutil network telemetry."""
from __future__ import annotations

from pathlib import Path

UNITS_DIR = Path(__file__).resolve().parents[1] / "ops" / "systemd"


def _unit(name: str) -> str:
    return (UNITS_DIR / name).read_text(encoding="utf-8")


def test_dashboard_unit_allows_af_netlink():
    line = next(line for line in _unit("aipm-dashboard.service").splitlines() if line.startswith("RestrictAddressFamilies="))
    assert "AF_NETLINK" in line.split("=", 1)[1].split()


def test_telemetry_unit_allows_af_netlink():
    line = next(line for line in _unit("aipm-telemetry.service").splitlines() if line.startswith("RestrictAddressFamilies="))
    assert "AF_NETLINK" in line.split("=", 1)[1].split()


def test_telemetry_unit_retains_docker_group_and_log_env():
    text = _unit("aipm-telemetry.service")
    assert "SupplementaryGroups=docker" in text
    assert "Environment=AIPM_LOG_FILE=/var/lib/aipm/logs/aipm.log" in text


def test_dashboard_unit_retains_env_and_readonly_bind():
    text = _unit("aipm-dashboard.service")
    assert "Environment=AIPM_CONFIG=/home/ubuntu/aipm/config/aipm.yaml" in text
    assert "Environment=AIPM_LOG_FILE=/var/lib/aipm/logs/aipm.log" in text
    assert "BindReadOnlyPaths=/var/lib/aipm/state/telemetry" in text


def test_units_share_canonical_config_env():
    expected = "Environment=AIPM_CONFIG=/home/ubuntu/aipm/config/aipm.yaml"
    assert expected in _unit("aipm-dashboard.service")
    assert expected in _unit("aipm-telemetry.service")
