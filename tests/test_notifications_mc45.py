from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.models.events import EventType, ResourceRef, ResourceType
from aipm.models.finding import Severity
from aipm.models.incidents import IncidentStatus
from aipm.models.notifications import DeliveryResult, DeliveryStatus, IncidentTransition, NotificationChannel, NotificationFilter, NotificationPolicy, NotificationStatus, NotificationTrigger
from aipm.repositories.notifications.sqlite import SQLiteNotificationRepository, identity_key
from aipm.services.notifications.channels import ChannelRegistry
from aipm.services.notifications.worker import NotificationProjector, NotificationWorker


UTC = timezone.utc


def transition(transition_id=None, trigger=NotificationTrigger.INCIDENT_OPENED, occurred_at=None):
    return IncidentTransition(transition_id, 7, "incident:container:x", trigger, occurred_at or datetime.now(UTC), None, IncidentStatus.OPEN, None, Severity.CRITICAL, None, None, "corr", ResourceRef(ResourceType.CONTAINER, "container-x", "container-x"), EventType.CONTAINER_RESTARTING)


def repository(tmp_path: Path) -> SQLiteNotificationRepository:
    repo = SQLiteNotificationRepository(tmp_path / "mc.db")
    with repo._connection() as connection:
        connection.execute("INSERT INTO incidents (id, incident_key, title, severity, status, started_at, updated_at, resource_type, resource_id, correlation_key, summary) VALUES (7, 'incident:container:x', 'test', 'critical', 'open', 1, 1, 'container', 'container-x', 'corr', 'test')")
    return repo


def policy(*, cooldown=0, window=60, max_notifications=1, transitions=(NotificationTrigger.INCIDENT_OPENED,)):
    return NotificationPolicy("critical", "Critical", True, Severity.CRITICAL, (), (), (), transitions, False, True, True, cooldown, window, max_notifications, ("mock",))


class FailingAdapter:
    channel_type = "mock"

    def send(self, notification, context):
        return DeliveryResult(DeliveryStatus.FAILED, True, error_code="timeout", error_message="safe timeout")


class SentAdapter:
    channel_type = "mock"

    def send(self, notification, context):
        return DeliveryResult(DeliveryStatus.SENT, False, provider_message_id="mock-1")


def project(repo, pol=None, channel=None):
    channel = channel or NotificationChannel("mock", "Mock", "mock", True, max_attempts=2)
    pol = pol or policy(max_notifications=10)
    return NotificationProjector(repo, (pol,), (channel,)).project_once()


def test_max_attempts_reaches_terminal_failure(tmp_path):
    repo = repository(tmp_path)
    channel = NotificationChannel("mock", "Mock", "mock", True, max_attempts=2)
    repo.add_transition(transition())
    project(repo, channel=channel)
    worker = NotificationWorker(repo, ChannelRegistry({"mock": FailingAdapter()}), (channel,))
    assert worker.deliver_once() is True
    with repo._connection() as connection:
        connection.execute("UPDATE notification_deliveries SET next_attempt_at = 0 WHERE id = 1")
        connection.execute("UPDATE notifications SET next_attempt_at = 0 WHERE id = 1")
    assert worker.deliver_once() is True
    row = repo.get_notification(1)
    assert row is not None and row.status is NotificationStatus.FAILED
    with repo._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM notification_attempts WHERE delivery_id = 1").fetchone()[0] == 2
        assert connection.execute("SELECT retry_exhausted_at FROM notifications WHERE id = 1").fetchone()[0] is not None
    assert worker.deliver_once() is False


def test_cooldown_and_channel_rate_limit_suppress_15_second_cadence(tmp_path):
    repo = repository(tmp_path)
    pol = policy(cooldown=60, window=60, max_notifications=2)
    repo.add_transition(transition())
    repo.add_transition(transition())
    assert project(repo, pol=pol) == 2
    with repo._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM notifications WHERE status = 'pending'").fetchone()[0] == 1
        reasons = {row[0] for row in connection.execute("SELECT reason FROM notification_suppressions").fetchall()}
    assert "incident_cooldown" in reasons


def test_fixed_window_rollover_allows_new_delivery(tmp_path):
    repo = repository(tmp_path)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    first = transition(1, occurred_at=base)
    second = transition(2, occurred_at=base + timedelta(seconds=61))
    pol = policy(cooldown=0, window=60, max_notifications=1)
    repo.add_transition(first)
    repo.add_transition(second)
    first_key = identity_key(pol.id, "mock", first)
    second_key = identity_key(pol.id, "mock", second)
    assert repo.create_decision(identity_key_value=first_key, transition=first, policy_id=pol.id, channel_id="mock", title="a", body="a", cooldown_seconds=0, window_seconds=60, max_notifications=1, now=base)[0] == 1
    assert repo.create_decision(identity_key_value=second_key, transition=second, policy_id=pol.id, channel_id="mock", title="b", body="b", cooldown_seconds=0, window_seconds=60, max_notifications=1, now=base + timedelta(seconds=61))[0] == 2


def test_compare_and_set_claim_and_expired_lease_recovery(tmp_path):
    repo1 = repository(tmp_path)
    repo1.add_transition(transition())
    project(repo1)
    repo2 = SQLiteNotificationRepository(tmp_path / "mc.db")
    first = repo1.claim_due(datetime.now(UTC), 60)
    assert first is not None
    assert repo2.claim_due(datetime.now(UTC), 60) is None
    with repo1._connection() as connection:
        connection.execute("UPDATE notification_deliveries SET lease_until = 0 WHERE id = 1")
    assert repo2.claim_due(datetime.now(UTC), 60) is not None


def test_unknown_reconciliation_and_retry_are_explicit(tmp_path):
    repo = repository(tmp_path)
    repo.add_transition(transition())
    channel = NotificationChannel("mock", "Mock", "mock", True, max_attempts=2)
    project(repo, channel=channel)
    with repo._connection() as connection:
        connection.execute("UPDATE notifications SET status = 'unknown' WHERE id = 1")
        connection.execute("UPDATE notification_deliveries SET status = 'unknown' WHERE id = 1")
    with pytest.raises(ValueError):
        repo.retry_delivery(1)
    assert repo.reconcile_unknown(1, delivered=False) is True
    assert repo.retry_delivery(1) is True
    with repo._connection() as connection:
        actions = [row[0] for row in connection.execute("SELECT action FROM notification_actions WHERE notification_id = 1 ORDER BY id").fetchall()]
    assert actions == ["reconcile_not_delivered", "retry_requested"]


def test_retention_preserves_active_state_and_removes_old_terminal(tmp_path):
    repo = repository(tmp_path)
    old = datetime.now(UTC) - timedelta(days=365)
    repo.add_transition(transition(occurred_at=old))
    project(repo)
    with repo._connection() as connection:
        connection.execute("UPDATE notifications SET status = 'sent', created_at = ? WHERE id = 1", (int(old.timestamp()),))
        connection.execute("UPDATE notification_deliveries SET status = 'sent' WHERE notification_id = 1")
    result = repo.retain(datetime.now(UTC) - timedelta(days=180))
    assert result["notifications"] == 0
    with repo._connection() as connection:
        connection.execute("UPDATE incidents SET status = 'resolved' WHERE id = 7")
    result = repo.retain(datetime.now(UTC) - timedelta(days=180))
    assert result["notifications"] == 1


def test_metrics_and_schema_version(tmp_path):
    repo = repository(tmp_path)
    assert repo.schema_version() == 2
    repo.add_transition(transition())
    project(repo)
    metrics = repo.metrics()
    assert metrics["pending"] == 1
    assert metrics["oldest_pending_age_seconds"] is not None
    assert "channels" in metrics


def test_sqlite_backup_restore_preserves_notification_state(tmp_path):
    import shutil

    repo = repository(tmp_path)
    repo.add_transition(transition())
    project(repo)
    claimed = repo.claim_due(datetime.now(UTC), 60)
    assert claimed is not None
    source = tmp_path / "mc.db"
    backup = tmp_path / "mc.backup.db"
    shutil.copy2(source, backup)
    restored = SQLiteNotificationRepository(tmp_path / "restored.db")
    shutil.copy2(backup, restored.database_path)
    restored = SQLiteNotificationRepository(restored.database_path)
    item = restored.get_notification(1)
    assert item is not None and item.identity_key
    with restored._connection() as connection:
        delivery = connection.execute("SELECT status, lease_until FROM notification_deliveries WHERE notification_id = 1").fetchone()
        assert delivery["status"] == "sending"
        assert delivery["lease_until"] is not None
        assert connection.execute("SELECT COUNT(*) FROM notifications WHERE identity_key = ?", (item.identity_key,)).fetchone()[0] == 1


def test_stale_worker_cannot_finish_reclaimed_delivery(tmp_path):
    repo = repository(tmp_path)
    repo.add_transition(transition())
    project(repo)
    first = repo.claim_due(datetime.now(UTC), 60)
    assert first is not None
    with repo._connection() as connection:
        connection.execute("UPDATE notification_deliveries SET lease_until = 0 WHERE id = 1")
    second = repo.claim_due(datetime.now(UTC), 60)
    assert second is not None
    repo.finish_delivery(1, DeliveryStatus.SENT, retryable=False, max_attempts=2, lease_token=first[1].lease_token, provider_message_id="stale")
    with repo._connection() as connection:
        row = connection.execute("SELECT status, provider_message_id FROM notification_deliveries WHERE id = 1").fetchone()
        attempts = connection.execute("SELECT COUNT(*) FROM notification_attempts WHERE delivery_id = 1").fetchone()[0]
    assert row["status"] == "sending"
    assert row["provider_message_id"] is None
    assert attempts == 0


def test_configuration_rejects_unsupported_enabled_channel():
    from aipm.core.config import ConfigManager
    from aipm.models.config import AIPMConfig, NotificationChannelConfig, NotificationConfig

    config = AIPMConfig(notifications=NotificationConfig(channels=[NotificationChannelConfig("email", "Email", "email", True, destination_ref="DEST")]))
    with pytest.raises(ValueError, match="unsupported"):
        ConfigManager._validate(config.logging, config.discovery, config.telemetry, config.events, config.notifications)


def test_configuration_rejects_enabled_channel_without_destination():
    from aipm.core.config import ConfigManager
    from aipm.models.config import AIPMConfig, NotificationChannelConfig, NotificationConfig

    config = AIPMConfig(notifications=NotificationConfig(channels=[NotificationChannelConfig("http", "HTTP", "http", True)]))
    with pytest.raises(ValueError, match="destination_ref"):
        ConfigManager._validate(config.logging, config.discovery, config.telemetry, config.events, config.notifications)


def test_metrics_include_suppression_audit(tmp_path):
    repo = repository(tmp_path)
    pol = policy(cooldown=60, max_notifications=10)
    first = transition(1)
    second = transition(2)
    repo.add_transition(first)
    repo.add_transition(second)
    first_key = identity_key(pol.id, "mock", first)
    second_key = identity_key(pol.id, "mock", second)
    repo.create_decision(identity_key_value=first_key, transition=first, policy_id=pol.id, channel_id="mock", title="a", body="a", cooldown_seconds=60, window_seconds=60, max_notifications=10)
    repo.create_decision(identity_key_value=second_key, transition=second, policy_id=pol.id, channel_id="mock", title="b", body="b", cooldown_seconds=60, window_seconds=60, max_notifications=10)
    assert repo.metrics()["suppressed"] == 1


def test_dashboard_metrics_and_channels_do_not_expose_secret_values(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from aipm.capabilities.dashboard.notifications_api import DashboardNotificationsApi
    from aipm.models.config import NotificationChannelConfig, NotificationConfig

    repo = repository(tmp_path)
    repo.add_transition(transition())
    project(repo)
    monkeypatch.setenv("MC45_SECRET", "super-secret-value")
    monkeypatch.setenv("MC45_DESTINATION", "safe-destination")
    app = SimpleNamespace(config=SimpleNamespace(notifications=NotificationConfig(channels=[NotificationChannelConfig("http", "HTTP", "http", True, "MC45_SECRET", "MC45_DESTINATION")], policies=[])), logger=SimpleNamespace(exception=lambda *args, **kwargs: None))
    api = DashboardNotificationsApi(repo, app)
    channels = api.channels()
    metrics = api.metrics()
    serialized = str(channels) + str(metrics)
    assert channels["channels"][0]["configured"] is True
    assert "super-secret-value" not in serialized
    assert "MC45_SECRET" not in serialized
    assert metrics["metrics"]["pending"] == 1


def test_legacy_notification_schema_migrates_with_identity_preserved(tmp_path):
    import sqlite3

    database = tmp_path / "legacy.db"
    base = SQLiteNotificationRepository(database)
    with base._connection() as connection:
        connection.execute("INSERT INTO incidents (id, incident_key, title, severity, status, started_at, updated_at, resource_type, resource_id, correlation_key, summary) VALUES (7, 'incident:container:x', 'test', 'critical', 'open', 1, 1, 'container', 'container-x', 'corr', 'test')")
        source = transition()
        connection.execute("INSERT INTO incident_transitions (id, incident_id, incident_key, transition, occurred_at, current_status, current_severity, correlation_key, resource_type, resource_id, event_type) VALUES (1, 7, 'incident:container:x', 'incident_opened', 1, 'open', 'critical', 'corr', 'container', 'container-x', 'container_restarting')")
        connection.execute("DROP TABLE notification_schema_meta")
        connection.execute("DROP TABLE notification_projection_runs")
        connection.execute("DROP TABLE notifications")
        connection.execute("DROP TABLE notification_deliveries")
        connection.execute("DROP TABLE notification_attempts")
        connection.execute("DROP TABLE notification_dedup")
        connection.execute("CREATE TABLE notification_projection_runs (transition_id INTEGER PRIMARY KEY, projected_at INTEGER NOT NULL, status TEXT NOT NULL)")
        connection.execute("CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, identity_key TEXT NOT NULL UNIQUE, transition_id INTEGER NOT NULL, incident_id INTEGER NOT NULL, event_id INTEGER, policy_id TEXT NOT NULL, channel_id TEXT NOT NULL, trigger TEXT NOT NULL, status TEXT NOT NULL, severity TEXT NOT NULL, resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, resource_name TEXT, project_path TEXT, title TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL, next_attempt_at INTEGER, attempt_count INTEGER NOT NULL DEFAULT 0, suppressed_reason TEXT)")
        connection.execute("CREATE TABLE notification_deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, notification_id INTEGER NOT NULL, channel_id TEXT NOT NULL, status TEXT NOT NULL, provider_request_key TEXT NOT NULL UNIQUE, provider_message_id TEXT, created_at INTEGER NOT NULL, last_attempt_at INTEGER, next_attempt_at INTEGER, lease_until INTEGER, attempt_count INTEGER NOT NULL DEFAULT 0)")
        connection.execute("CREATE TABLE notification_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id INTEGER NOT NULL, attempt_number INTEGER NOT NULL, started_at INTEGER NOT NULL, finished_at INTEGER, outcome TEXT NOT NULL, retryable INTEGER NOT NULL, provider_status_code INTEGER, error_code TEXT, error_message TEXT, UNIQUE(delivery_id, attempt_number))")
        connection.execute("CREATE TABLE notification_dedup (scope_key TEXT PRIMARY KEY, last_notified_at INTEGER, window_started_at INTEGER, window_count INTEGER NOT NULL DEFAULT 0, suppressed_count INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL)")
        connection.execute("INSERT INTO notifications (id, identity_key, transition_id, incident_id, policy_id, channel_id, trigger, status, severity, resource_type, resource_id, title, body, created_at) VALUES (1, 'legacy-identity', 1, 7, 'p', 'mock', 'incident_opened', 'pending', 'critical', 'container', 'container-x', 'title', 'body', 1)")
        connection.execute("INSERT INTO notification_deliveries (id, notification_id, channel_id, status, provider_request_key, created_at) VALUES (1, 1, 'mock', 'pending', 'legacy-provider-key', 1)")
    migrated = SQLiteNotificationRepository(database)
    assert migrated.schema_version() == 2
    assert migrated.get_notification(1).identity_key == "legacy-identity"
    with migrated._connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM pragma_foreign_key_list(\'notifications\') WHERE "table" IN (\'incidents\', \'incident_transitions\', \'events\')').fetchone()[0] == 3


def test_concurrent_projection_creates_one_outbox_identity(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    repo = repository(tmp_path)
    repo.add_transition(transition())
    channel = NotificationChannel("mock", "Mock", "mock", True)
    pol = policy(max_notifications=10)

    def project_once():
        local_repo = SQLiteNotificationRepository(tmp_path / "mc.db")
        return NotificationProjector(local_repo, (pol,), (channel,)).project_once()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: project_once(), range(2)))
    with repo._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 1
    assert sum(results) >= 1


def test_concurrent_delivery_claims_have_single_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    repo = repository(tmp_path)
    repo.add_transition(transition())
    project(repo)

    def claim_once():
        return SQLiteNotificationRepository(tmp_path / "mc.db").claim_due(datetime.now(UTC), 60) is not None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim_once(), range(2)))
    assert sum(results) == 1
