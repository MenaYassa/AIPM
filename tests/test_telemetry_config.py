from pathlib import Path

import pytest

from aipm.core.config import ConfigManager
from aipm.core.exceptions import AIPMError
from aipm.models.config import DiscoveryConfig, EventConfig


def write_config(path: Path, telemetry: str) -> None:
    path.write_text(
        "logging: {}\ndiscovery:\n  search_paths: ['/tmp']\ntelemetry:\n" + telemetry,
        encoding="utf-8",
    )


def test_event_defaults_are_safe():
    config = EventConfig()
    assert config.enabled is True
    assert config.interval_seconds == 15
    assert config.event_retention_days == 30
    assert config.incident_retention_days == 180


def test_discovery_defaults_are_bounded():
    config = DiscoveryConfig()
    assert config.max_directories == 2000
    assert config.max_entries == 10000
    assert config.max_projects == 128
    assert config.max_git_enrichments == 128
    assert config.git_timeout_seconds == 5.0
    assert config.max_git_items == 100


def test_telemetry_defaults_are_safe(tmp_path):
    config = ConfigManager(tmp_path / "config.yaml").config
    assert config.telemetry.enabled is True
    assert config.telemetry.interval_seconds == 15
    assert config.telemetry.retention_days == 1
    assert config.telemetry.retention_interval_seconds == 900
    assert config.telemetry.database_path.endswith(".local/state/aipm/telemetry/mission_control.db")


@pytest.mark.parametrize(
    "telemetry",
    [
        "  interval_seconds: 0\n  retention_days: 1\n  database_path: /tmp/mc.db\n",
        "  interval_seconds: 15\n  retention_days: 0\n  database_path: /tmp/mc.db\n",
        "  interval_seconds: 15\n  retention_interval_seconds: 0\n  database_path: /tmp/mc.db\n",
        "  interval_seconds: 15\n  retention_days: 1\n  database_path: ''\n",
    ],
)
def test_invalid_telemetry_config_fails_clearly(tmp_path, telemetry):
    path = tmp_path / "config.yaml"
    write_config(path, telemetry)
    with pytest.raises(AIPMError, match="Failed to load configuration"):
        ConfigManager(path)


@pytest.mark.parametrize("field", ["max_directories", "max_entries", "max_projects", "max_git_enrichments", "max_git_items"])
def test_invalid_discovery_bounds_fail_clearly(tmp_path, field):
    path = tmp_path / "config.yaml"
    path.write_text(f"logging: {{}}\ndiscovery:\n  search_paths: ['/tmp']\n  {field}: 0\ntelemetry:\n  database_path: /tmp/mc.db\n", encoding="utf-8")
    with pytest.raises(AIPMError, match="Failed to load configuration"):
        ConfigManager(path)


def test_invalid_git_timeout_fails_clearly(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("logging: {}\ndiscovery:\n  search_paths: ['/tmp']\n  git_timeout_seconds: 0\ntelemetry:\n  database_path: /tmp/mc.db\n", encoding="utf-8")
    with pytest.raises(AIPMError, match="Failed to load configuration"):
        ConfigManager(path)


def test_invalid_event_config_fails_clearly(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("logging: {}\ndiscovery:\n  search_paths: ['/tmp']\nevents:\n  interval_seconds: 0\n", encoding="utf-8")
    with pytest.raises(AIPMError, match="Failed to load configuration"):
        ConfigManager(path)


def test_explicit_aipm_config_environment_selects_config_source(tmp_path, monkeypatch):
    path = tmp_path / "selected.yaml"
    path.write_text("logging: {}\ndiscovery:\n  search_paths: ['/srv/approved']\ntelemetry:\n  database_path: /tmp/mc.db\n", encoding="utf-8")
    monkeypatch.setenv("AIPM_CONFIG", str(path))
    assert ConfigManager().config.discovery.search_paths == ["/srv/approved"]


def test_telemetry_database_environment_override(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    write_config(path, "  interval_seconds: 15\n  retention_days: 1\n  database_path: /tmp/config.db\n")
    override = tmp_path / "override.db"
    monkeypatch.setenv("AIPM_TELEMETRY_DB", str(override))
    assert ConfigManager(path).config.telemetry.database_path == str(override)


def test_mc21_telemetry_defaults_are_split_and_bounded(tmp_path):
    config = ConfigManager(tmp_path / "config.yaml").config.telemetry
    assert config.sampling_mode == "split"
    assert config.resource_interval_seconds == 60
    assert config.resource_timeout_seconds == 15
    assert config.resource_stale_after_seconds == 180
    assert config.slow_task_max_concurrency == 1
    assert config.retention_interval_seconds == 900


@pytest.mark.parametrize("telemetry", [
    "  interval_seconds: 15\n  resource_interval_seconds: 0\n  database_path: /tmp/mc.db\n",
    "  interval_seconds: 15\n  resource_interval_seconds: 60\n  resource_timeout_seconds: 0\n  database_path: /tmp/mc.db\n",
    "  interval_seconds: 15\n  resource_interval_seconds: 60\n  resource_stale_after_seconds: 60\n  database_path: /tmp/mc.db\n",
    "  interval_seconds: 15\n  sampling_mode: invalid\n  database_path: /tmp/mc.db\n",
])
def test_invalid_mc21_telemetry_config_fails_clearly(tmp_path, telemetry):
    path = tmp_path / "config.yaml"
    write_config(path, telemetry)
    with pytest.raises(AIPMError, match="Failed to load configuration"):
        ConfigManager(path)
