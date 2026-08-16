from pathlib import Path

import pytest

from aipm.core.config import ConfigManager
from aipm.core.exceptions import AIPMError


def write_config(path: Path, telemetry: str) -> None:
    path.write_text(
        "logging: {}\ndiscovery:\n  search_paths: ['/tmp']\ntelemetry:\n" + telemetry,
        encoding="utf-8",
    )


def test_telemetry_defaults_are_safe(tmp_path):
    config = ConfigManager(tmp_path / "config.yaml").config
    assert config.telemetry.enabled is True
    assert config.telemetry.interval_seconds == 15
    assert config.telemetry.retention_days == 1
    assert config.telemetry.database_path.endswith(".local/state/aipm/telemetry/mission_control.db")


@pytest.mark.parametrize(
    "telemetry",
    [
        "  interval_seconds: 0\n  retention_days: 1\n  database_path: /tmp/mc.db\n",
        "  interval_seconds: 15\n  retention_days: 0\n  database_path: /tmp/mc.db\n",
        "  interval_seconds: 15\n  retention_days: 1\n  database_path: ''\n",
    ],
)
def test_invalid_telemetry_config_fails_clearly(tmp_path, telemetry):
    path = tmp_path / "config.yaml"
    write_config(path, telemetry)
    with pytest.raises(AIPMError, match="Failed to load configuration"):
        ConfigManager(path)


def test_telemetry_database_environment_override(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    write_config(path, "  interval_seconds: 15\n  retention_days: 1\n  database_path: /tmp/config.db\n")
    override = tmp_path / "override.db"
    monkeypatch.setenv("AIPM_TELEMETRY_DB", str(override))
    assert ConfigManager(path).config.telemetry.database_path == str(override)
