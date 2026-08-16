from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rich import print

from aipm.core.app import Application
from aipm.models.notifications import NotificationFilter, NotificationStatus
from aipm.repositories.notifications.sqlite import SQLiteNotificationRepository
from aipm.services.notifications.channels import ChannelRegistry
from aipm.services.notifications.policy import channel_from_config, policy_from_config
from aipm.services.notifications.runner import NotificationRunner
from aipm.services.notifications.worker import NotificationProjector, NotificationWorker


def build(application: Application):
    config = application.config.notifications
    repository = SQLiteNotificationRepository(application.config.telemetry.database_path)
    channels = tuple(channel_from_config(item) for item in config.channels)
    policies = tuple(policy_from_config(item) for item in config.policies)
    projector = NotificationProjector(repository, policies, channels, global_window_seconds=config.global_window_seconds, global_max_notifications=config.global_max_notifications, logger=application.logger)
    worker = NotificationWorker(repository, ChannelRegistry.default(), channels, logger=application.logger)
    return repository, projector, worker


def list_notifications() -> None:
    application = Application.create()
    repository, _projector, _worker = build(application)
    rows = repository.get_notifications(NotificationFilter(include_suppressed=True))
    if not rows:
        print("[yellow]No notifications recorded.[/yellow]")
        return
    for item in rows:
        print(f"{item.id}: {item.status.value} incident={item.incident_id} channel={item.channel_id} trigger={item.trigger.value}")


def retry(notification_id: int, yes: bool = False) -> None:
    application = Application.create()
    repository, _projector, _worker = build(application)
    if not yes:
        print("[yellow]No retry requested. Re-run with --yes after reviewing the notification.[/yellow]")
        return
    try:
        repository.retry_delivery(notification_id, allow_unknown=False, actor="cli", reason="operator requested retry")
        print(f"[green]Notification {notification_id} queued for a bounded retry.[/green]")
    except ValueError as exc:
        print(f"[red]Retry refused:[/red] {exc}")


def reconcile(notification_id: int, delivered: bool, yes: bool = False) -> None:
    application = Application.create()
    repository, _projector, _worker = build(application)
    if not yes:
        print("[yellow]No reconciliation recorded. Re-run with --yes after confirming provider state.[/yellow]")
        return
    try:
        repository.reconcile_unknown(notification_id, delivered=delivered, actor="cli", reason="operator provider reconciliation")
        print(f"[green]UNKNOWN notification {notification_id} reconciled as {'delivered' if delivered else 'not delivered'}.[/green]")
    except ValueError as exc:
        print(f"[red]Reconciliation refused:[/red] {exc}")


def retain() -> None:
    application = Application.create()
    repository, _projector, _worker = build(application)
    cutoff = datetime.now(timezone.utc) - timedelta(days=application.config.notifications.retention_days)
    result = repository.retain(cutoff)
    print(f"[green]Notification retention complete.[/green] {result}")


def metrics() -> None:
    application = Application.create()
    repository, _projector, _worker = build(application)
    print(repository.metrics())


def test_channel(channel_id: str, confirm: bool = False) -> None:
    if not confirm:
        print("[yellow]No notification was sent. Re-run with --yes only after reviewing the configured destination.[/yellow]")
        return
    print(f"[yellow]Real channel test for {channel_id} is intentionally not executed by automated MC-4.5 verification.[/yellow]")


def run() -> None:
    application = Application.create()
    if not application.config.notifications.enabled:
        print("[yellow]Notification worker is disabled by configuration.[/yellow]")
        return
    repository, projector, worker = build(application)
    NotificationRunner(projector, worker, application.config.notifications, logger=application.logger).run()
