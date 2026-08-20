"""
MC-6.10 focused tests: Settings & Notification Posture.

Coverage targets per approved test plan:
A. Basic posture response
B. GET-only behavior
C. Safe enums
D. Bounded counts
E. Unknown/unavailable states
F. Disabled notifications
G. Provider non-instantiation
H. No credential lookup
I. No network calls
J. No raw configuration serialization
K. No secret references
L. No destination values
M. No environment-variable names
N. No notification body/title
O. No filesystem paths
P. No provider IDs
Q. No lease tokens
R. Notification metrics from read-only repository
S. SQLite remains read-only
T. No database writes
U. No schema mutation
V. No checkpoint
W. Frontend /static module routing
X. Exactly one centralized scheduler resource
Y. No settings mutation controls
Z. No provider/delivery controls
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aipm.capabilities.dashboard.settings_api import DashboardSettingsApi
from aipm.mappers.settings import SettingsResponseMapper
from aipm.models.settings import (
    NotificationAuditAvailability,
    NotificationProviderState,
    PostureState,
    SettingsPosture,
    bounded_count,
    bounded_interval,
    bounded_latency,
    bounded_optional_age,
)
from aipm.repositories.notifications.sqlite import SQLiteNotificationRepository


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    *,
    notification_enabled: bool = False,
    channels: list[Any] | None = None,
    policies: list[Any] | None = None,
    telemetry_enabled: bool = True,
    telemetry_interval: int = 15,
    events_enabled: bool = True,
    events_interval: int = 15,
) -> Any:
    channel_list = channels or []
    policy_list = policies or []
    return SimpleNamespace(
        notifications=SimpleNamespace(
            enabled=notification_enabled,
            channels=channel_list,
            policies=policy_list,
        ),
        telemetry=SimpleNamespace(
            enabled=telemetry_enabled,
            interval_seconds=telemetry_interval,
            database_path="/tmp/aipm_mc610_test_nonexistent.db",
        ),
        events=SimpleNamespace(
            enabled=events_enabled,
            interval_seconds=events_interval,
        ),
    )


def _make_channel(enabled: bool = False) -> Any:
    return SimpleNamespace(enabled=enabled, secret_ref=None, destination_ref=None)


def _make_policy(enabled: bool = False) -> Any:
    return SimpleNamespace(enabled=enabled)


def _make_application(config: Any = None) -> Any:
    return SimpleNamespace(
        config=config or _make_config(),
        logger=SimpleNamespace(exception=lambda *args, **kwargs: None),
    )


def _make_api(
    *,
    config: Any = None,
    repository: SQLiteNotificationRepository | None = None,
    service_health_api: Any = None,
) -> DashboardSettingsApi:
    app = _make_application(config or _make_config())
    return DashboardSettingsApi(
        application=app,
        repository=repository,
        service_health_api=service_health_api,
    )


def _protect_database(database_path: Path) -> None:
    """Apply the local equivalent of the MC-5.1.2 filesystem boundary."""
    for sidecar in (database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm")):
        if sidecar.exists():
            sidecar.chmod(0o444)
    database_path.parent.chmod(0o555)


def _make_notification_db(tmp_path: Path) -> SQLiteNotificationRepository:
    """Create a minimal notification database for read-only tests."""
    db_path = tmp_path / "test.db"
    SQLiteNotificationRepository(db_path)
    _protect_database(db_path)
    return SQLiteNotificationRepository(db_path, read_only=True)


# ---------------------------------------------------------------------------
# A. Basic posture response
# ---------------------------------------------------------------------------

def test_posture_response_is_available():
    api = _make_api()
    result = api.posture()
    assert result["available"] is True
    assert result["status"] == "ok"
    assert result["error"] is None
    assert "generated_at" in result


def test_posture_response_contains_required_sections():
    api = _make_api()
    result = api.posture()
    assert "application" in result
    assert "deployment" in result
    assert "read_only" in result
    assert "telemetry" in result
    assert "mc3" in result
    assert "notifications" in result
    assert "capabilities" in result


# ---------------------------------------------------------------------------
# B. GET-only behavior
# ---------------------------------------------------------------------------

def test_settings_route_is_get_only():
    from fastapi.testclient import TestClient
    from aipm.dashboard.server import create_app

    api = _make_api()
    app = create_app(settings_api=api)
    client = TestClient(app)
    assert client.get("/api/settings/posture").status_code == 200
    assert client.post("/api/settings/posture").status_code == 405
    assert client.put("/api/settings/posture").status_code == 405
    assert client.delete("/api/settings/posture").status_code == 405
    assert client.patch("/api/settings/posture").status_code == 405


# ---------------------------------------------------------------------------
# C. Safe enums
# ---------------------------------------------------------------------------

def test_posture_state_enum_values_are_safe():
    for state in PostureState:
        assert state.value.replace("_", "").isalpha() or state.value in {"not_observed", "not_instantiated", "never_sampled"}


def test_notification_provider_state_enum_values_are_safe():
    for state in NotificationProviderState:
        assert state.value.replace("_", "").isalpha()


def test_posture_response_status_is_valid_enum():
    api = _make_api()
    result = api.posture()
    assert result["status"] in {state.value for state in PostureState}


def test_notification_provider_state_is_valid_enum():
    api = _make_api()
    result = api.posture()
    assert result["notifications"]["provider_state"] in {state.value for state in NotificationProviderState}


# ---------------------------------------------------------------------------
# D. Bounded counts
# ---------------------------------------------------------------------------

def test_bounded_count_helpers():
    assert bounded_count(None) == 0
    assert bounded_count(-1) == 0
    assert bounded_count(5) == 5
    assert bounded_count(2_000_000) == 1_000_000
    assert bounded_count("bad") == 0


def test_bounded_interval_helpers():
    assert bounded_interval(None) == 0
    assert bounded_interval(-1) == 0
    assert bounded_interval(15) == 15
    assert bounded_interval(100_000) == 86_400


def test_bounded_latency_helpers():
    assert bounded_latency(None) is None
    assert bounded_latency(-1.0) == 0.0
    assert bounded_latency(1.5) == 1.5
    assert bounded_latency(100_000.0) == 86_400.0


def test_bounded_optional_age_helpers():
    assert bounded_optional_age(None) is None
    assert bounded_optional_age(0) == 0
    assert bounded_optional_age(60) == 60


def test_posture_channel_counts_are_bounded():
    channels = [_make_channel(enabled=True), _make_channel(enabled=False)]
    policies = [_make_policy(enabled=True)]
    api = _make_api(config=_make_config(channels=channels, policies=policies))
    result = api.posture()
    n = result["notifications"]
    assert n["configured_channel_count"] == 2
    assert n["enabled_channel_count"] == 1
    assert n["configured_policy_count"] == 1
    assert n["enabled_policy_count"] == 1


# ---------------------------------------------------------------------------
# E. Unknown/unavailable states
# ---------------------------------------------------------------------------

def test_unavailable_posture_is_fail_closed():
    posture = SettingsPosture.unavailable(generated_at="2026-01-01T00:00:00+00:00")
    assert posture.available is False
    assert posture.status == PostureState.UNAVAILABLE


def test_posture_with_no_repository_reports_unavailable_audit():
    api = _make_api(repository=None)
    result = api.posture()
    audit = result["notifications"]["audit"]
    assert audit["availability"] == NotificationAuditAvailability.UNAVAILABLE.value
    assert audit["pending"] is None
    assert audit["sent"] is None
    assert audit["schema_version"] is None


# ---------------------------------------------------------------------------
# F. Disabled notifications
# ---------------------------------------------------------------------------

def test_notifications_disabled_by_default():
    api = _make_api(config=_make_config(notification_enabled=False))
    result = api.posture()
    assert result["notifications"]["enabled"] is False


def test_notifications_disabled_provider_state():
    api = _make_api(config=_make_config(notification_enabled=False))
    result = api.posture()
    assert result["notifications"]["provider_state"] == NotificationProviderState.DISABLED.value


# ---------------------------------------------------------------------------
# G. Provider non-instantiation
# ---------------------------------------------------------------------------

def test_no_channel_registry_in_settings_module():
    import aipm.capabilities.dashboard.settings_api as module
    src = Path(module.__file__).read_text()
    assert "ChannelRegistry" not in src
    assert "NotificationProjector" not in src
    assert "NotificationWorker" not in src
    assert "NotificationRunner" not in src
    assert "HttpAdapter" not in src
    assert "TelegramAdapter" not in src


def test_no_delivery_context_in_settings_module():
    import aipm.capabilities.dashboard.settings_api as module
    src = Path(module.__file__).read_text()
    assert "DeliveryContext" not in src
    assert "ChannelRegistry" not in src


# ---------------------------------------------------------------------------
# H. No credential lookup
# ---------------------------------------------------------------------------

def test_no_env_var_inspection_in_settings_module():
    import aipm.capabilities.dashboard.settings_api as module
    src = Path(module.__file__).read_text()
    assert "os.environ" not in src
    assert "os.getenv" not in src
    assert "getenv" not in src


def test_no_secret_ref_access_in_settings_module():
    import aipm.capabilities.dashboard.settings_api as module
    src = Path(module.__file__).read_text()
    assert "secret_ref" not in src
    assert "destination_ref" not in src


# ---------------------------------------------------------------------------
# I. No network calls
# ---------------------------------------------------------------------------

def test_no_network_in_settings_module():
    import aipm.capabilities.dashboard.settings_api as module
    src = Path(module.__file__).read_text()
    assert "urlopen" not in src
    assert "requests" not in src
    assert "httpx" not in src
    assert "aiohttp" not in src
    assert "socket" not in src


def test_no_network_in_settings_mapper():
    import aipm.mappers.settings as module
    src = Path(module.__file__).read_text()
    assert "urlopen" not in src
    assert "requests" not in src


# ---------------------------------------------------------------------------
# J. No raw configuration serialization
# ---------------------------------------------------------------------------

def test_posture_response_does_not_contain_raw_config_keys():
    api = _make_api()
    result = api.posture()
    serialized = str(result)
    assert "database_path" not in serialized
    assert "config_path" not in serialized
    assert "discovery" not in serialized
    assert "max_depth" not in serialized
    assert "backup_count" not in serialized


# ---------------------------------------------------------------------------
# K. No secret references
# ---------------------------------------------------------------------------

def test_posture_response_does_not_expose_secret_ref():
    channels = [SimpleNamespace(enabled=True, secret_ref="MY_TELEGRAM_TOKEN", destination_ref="MY_CHAT_ID")]
    api = _make_api(config=_make_config(channels=channels))
    result = api.posture()
    serialized = str(result)
    assert "MY_TELEGRAM_TOKEN" not in serialized
    assert "secret_ref" not in serialized


# ---------------------------------------------------------------------------
# L. No destination values
# ---------------------------------------------------------------------------

def test_posture_response_does_not_expose_destination():
    channels = [SimpleNamespace(enabled=True, secret_ref=None, destination_ref="MY_WEBHOOK_URL")]
    api = _make_api(config=_make_config(channels=channels))
    result = api.posture()
    serialized = str(result)
    assert "MY_WEBHOOK_URL" not in serialized
    assert "destination_ref" not in serialized
    assert "destination" not in serialized


# ---------------------------------------------------------------------------
# M. No environment-variable names
# ---------------------------------------------------------------------------

def test_posture_response_does_not_expose_env_var_names():
    channels = [SimpleNamespace(enabled=True, secret_ref="AIPM_SECRET", destination_ref="AIPM_DEST")]
    api = _make_api(config=_make_config(channels=channels))
    result = api.posture()
    serialized = str(result)
    assert "AIPM_SECRET" not in serialized
    assert "AIPM_DEST" not in serialized


# ---------------------------------------------------------------------------
# N. No notification body/title
# ---------------------------------------------------------------------------

def test_posture_response_does_not_expose_notification_body_or_title(tmp_path):
    db_path = tmp_path / "test.db"
    from aipm.repositories.incidents.sqlite import SQLiteIncidentRepository
    SQLiteIncidentRepository(db_path).initialize()
    repo = SQLiteNotificationRepository(db_path)
    repo.initialize()
    _protect_database(db_path)
    ro_repo = SQLiteNotificationRepository(db_path, read_only=True)
    api = _make_api(repository=ro_repo)
    result = api.posture()
    serialized = str(result)
    assert "body" not in serialized
    assert "title" not in serialized


# ---------------------------------------------------------------------------
# O. No filesystem paths
# ---------------------------------------------------------------------------

def test_posture_response_does_not_expose_filesystem_paths():
    api = _make_api()
    result = api.posture()
    serialized = str(result)
    assert "/home" not in serialized
    assert "/var" not in serialized
    assert "/opt" not in serialized
    assert "/tmp" not in serialized
    assert "database_path" not in serialized


# ---------------------------------------------------------------------------
# P. No provider IDs / Q. No lease tokens
# ---------------------------------------------------------------------------

def test_posture_response_does_not_expose_provider_ids_or_lease_tokens(tmp_path):
    db_path = tmp_path / "test.db"
    from aipm.repositories.incidents.sqlite import SQLiteIncidentRepository
    SQLiteIncidentRepository(db_path).initialize()
    repo = SQLiteNotificationRepository(db_path)
    repo.initialize()
    _protect_database(db_path)
    ro_repo = SQLiteNotificationRepository(db_path, read_only=True)
    api = _make_api(repository=ro_repo)
    result = api.posture()
    serialized = str(result)
    assert "provider_message_id" not in serialized
    assert "provider_request_key" not in serialized
    assert "lease_token" not in serialized
    assert "identity_key" not in serialized


# ---------------------------------------------------------------------------
# R. Notification metrics from read-only repository
# ---------------------------------------------------------------------------

def test_notification_metrics_come_from_read_only_repository():
    class ReadOnlyMetricsSpy:
        read_only = True

        def __init__(self):
            self.metrics_calls = 0
            self.schema_calls = 0

        def metrics(self, *, now):
            self.metrics_calls += 1
            return {"pending": 3, "sent": 4, "failed": 1}

        def schema_version(self):
            self.schema_calls += 1
            return 2

    repository = ReadOnlyMetricsSpy()
    result = _make_api(repository=repository).posture()
    audit = result["notifications"]["audit"]
    assert audit["availability"] == NotificationAuditAvailability.OBSERVED.value
    assert audit["pending"] == 3
    assert audit["sent"] == 4
    assert audit["failed"] == 1
    assert audit["schema_version"] == 2
    assert repository.metrics_calls == 1
    assert repository.schema_calls == 1


def test_notification_metrics_observed_zero_values_remain_zero():
    class ZeroMetricsRepository:
        read_only = True

        def metrics(self, *, now):
            return {"pending": 0, "sent": 0, "failed": 0, "unknown": 0, "suppressed": 0, "retry_exhaustion_count": 0, "lease_expiry_count": 0}

        def schema_version(self):
            return 2

    audit = _make_api(repository=ZeroMetricsRepository()).posture()["notifications"]["audit"]
    assert audit["availability"] == NotificationAuditAvailability.OBSERVED.value
    assert audit["pending"] == 0
    assert audit["sent"] == 0
    assert audit["failed"] == 0


def test_notification_metrics_query_failure_reports_unavailable_without_raw_error():
    class FailingMetricsRepository:
        read_only = True

        def metrics(self, *, now):
            raise RuntimeError("PRIVATE notification database details")

        def schema_version(self):
            raise AssertionError("schema_version must not be reached after metrics failure")

    result = _make_api(repository=FailingMetricsRepository()).posture()
    audit = result["notifications"]["audit"]
    assert audit["availability"] == NotificationAuditAvailability.UNAVAILABLE.value
    assert audit["pending"] is None
    assert audit["sent"] is None
    assert "PRIVATE notification database details" not in str(result)


# ---------------------------------------------------------------------------
# S. SQLite remains read-only / T. No database writes / U. No schema mutation / V. No checkpoint
# ---------------------------------------------------------------------------

def test_settings_posture_does_not_write_database(tmp_path):
    db_path = tmp_path / "test.db"
    from aipm.repositories.incidents.sqlite import SQLiteIncidentRepository
    SQLiteIncidentRepository(db_path).initialize()
    SQLiteNotificationRepository(db_path).initialize()
    _protect_database(db_path)
    before = db_path.stat()
    ro_repo = SQLiteNotificationRepository(db_path, read_only=True)
    api = _make_api(repository=ro_repo)
    api.posture()
    after = db_path.stat()
    assert before.st_mtime == after.st_mtime, "Database mtime changed after settings posture read"


def test_settings_repository_rejects_writes(tmp_path):
    db_path = tmp_path / "test.db"
    from aipm.repositories.incidents.sqlite import SQLiteIncidentRepository
    SQLiteIncidentRepository(db_path).initialize()
    SQLiteNotificationRepository(db_path).initialize()
    _protect_database(db_path)
    ro_repo = SQLiteNotificationRepository(db_path, read_only=True)
    with pytest.raises(Exception):
        ro_repo.initialize()


# ---------------------------------------------------------------------------
# W. Frontend /static module routing
# ---------------------------------------------------------------------------

def test_settings_module_served_from_static_mount():
    from fastapi.testclient import TestClient
    from aipm.dashboard.server import create_app
    client = TestClient(create_app(settings_api=_make_api()))
    response = client.get("/static/mission-control-settings.js")
    assert response.status_code == 200
    assert "createSettingsController" in response.text


def test_settings_module_import_uses_static_prefix():
    html = Path("src/aipm/dashboard/static/index.html").read_text()
    assert "'/static/mission-control-settings.js'" in html


def test_settings_posture_root_connects_success_render_path():
    html = Path("src/aipm/dashboard/static/index.html").read_text()
    controller = Path("src/aipm/dashboard/static/mission-control-settings.js").read_text()
    assert 'id="settingsPosture"' in html
    assert "getElementById('settingsPosture')" in controller
    assert "settingsApplication" in html and "settingsApplication" in controller
    assert "settingsNotifications" in html and "settingsNotifications" in controller
    assert "if (!root) return" in controller


def test_settings_unavailable_audit_rendering_preserves_observed_zero_contract():
    controller = Path("src/aipm/dashboard/static/mission-control-settings.js").read_text()
    assert "audit.availability === 'observed'" in controller
    assert "auditAvailable ? audit.pending : null" in controller
    assert "['Audit status', auditAvailable ? 'Observed' : 'Unavailable']" in controller


# ---------------------------------------------------------------------------
# X. Exactly one centralized scheduler resource
# ---------------------------------------------------------------------------

def test_settings_posture_has_exactly_one_scheduler_resource():
    html = Path("src/aipm/dashboard/static/index.html").read_text()
    count = html.count("settings-posture")
    assert count == 1, f"Expected exactly 1 settings-posture scheduler registration, found {count}"


def test_settings_posture_scheduler_interval_is_60s():
    html = Path("src/aipm/dashboard/static/index.html").read_text()
    assert "scheduler.register('settings-posture',settingsController.load,{intervalMs:60000})" in html


# ---------------------------------------------------------------------------
# Y. No settings mutation controls
# ---------------------------------------------------------------------------

def test_settings_module_has_no_mutation_controls():
    src = Path("src/aipm/dashboard/static/mission-control-settings.js").read_text()
    for forbidden in ("POST", "PUT", "PATCH", "DELETE", "fetch('/api/settings'", "form", "submit", "input type=\"text\"", "input type=\"password\""):
        assert forbidden not in src, f"Mutation control found in settings module: {forbidden!r}"


# ---------------------------------------------------------------------------
# Z. No provider/delivery controls
# ---------------------------------------------------------------------------

def test_settings_module_has_no_delivery_controls():
    src = Path("src/aipm/dashboard/static/mission-control-settings.js").read_text()
    for forbidden in ("send(", "retry(", "reconcile(", "deliver(", "activate(", "POST", "PUT", "PATCH", "DELETE"):
        assert forbidden not in src, f"Delivery control found in settings module: {forbidden!r}"


def test_settings_api_has_no_delivery_imports():
    import aipm.capabilities.dashboard.settings_api as module
    src = Path(module.__file__).read_text()
    for forbidden in ("ChannelRegistry", "NotificationProjector", "NotificationWorker", "NotificationRunner", "HttpAdapter", "TelegramAdapter", "urlopen", "DeliveryContext"):
        assert forbidden not in src, f"Delivery import found in settings API: {forbidden!r}"


# ---------------------------------------------------------------------------
# Mapper safety
# ---------------------------------------------------------------------------

def test_mapper_does_not_expose_raw_config():
    mapper = SettingsResponseMapper()
    posture = SettingsPosture.unavailable(generated_at="2026-01-01T00:00:00+00:00")
    result = mapper.to_response(posture)
    serialized = str(result)
    assert "database_path" not in serialized
    assert "secret_ref" not in serialized
    assert "destination_ref" not in serialized


def test_mapper_unavailable_response_is_safe():
    mapper = SettingsResponseMapper()
    result = mapper.unavailable()
    assert result["available"] is False
    assert result["status"] == "unavailable"
    assert "database_path" not in str(result)
