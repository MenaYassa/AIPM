from __future__ import annotations

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
    projector = NotificationProjector(repository, policies, channels, logger=application.logger)
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


def retry(notification_id: int) -> None:
    application = Application.create()
    repository, _projector, _worker = build(application)
    item = repository.get_notification(notification_id)
    if item is None:
        print("[red]Notification not found.[/red]")
        return
    if item.status not in {NotificationStatus.FAILED, NotificationStatus.UNKNOWN}:
        print("[yellow]Only failed or unknown notifications can be retried.[/yellow]")
        return
    print("[yellow]Retry is intentionally limited to the worker's durable delivery state; use the configured worker to perform delivery.[/yellow]")


def test_channel(channel_id: str, confirm: bool = False) -> None:
    if not confirm:
        print("[yellow]No notification was sent. Re-run with --yes only after reviewing the configured destination.[/yellow]")
        return
    print(f"[yellow]Real channel test for {channel_id} is not enabled in this implementation slice.[/yellow]")


def run() -> None:
    application = Application.create()
    if not application.config.notifications.enabled:
        print("[yellow]Notification worker is disabled by configuration.[/yellow]")
        return
    repository, projector, worker = build(application)
    NotificationRunner(projector, worker, application.config.notifications, logger=application.logger).run()
