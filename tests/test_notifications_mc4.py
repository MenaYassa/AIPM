from datetime import datetime, timezone

from aipm.models.events import EventType, ResourceRef, ResourceType
from aipm.models.finding import Severity
from aipm.models.incidents import IncidentStatus
from aipm.models.notifications import DeliveryResult, DeliveryStatus, IncidentTransition, NotificationStatus, NotificationTrigger, NotificationFilter
from aipm.repositories.notifications.sqlite import SQLiteNotificationRepository
from aipm.services.notifications.channels import ChannelRegistry
from aipm.services.notifications.policy import evaluate
from aipm.services.notifications.worker import NotificationProjector, NotificationWorker
from aipm.models.notifications import NotificationChannel, NotificationPolicy


def transition(transition_type=NotificationTrigger.INCIDENT_OPENED):
    return IncidentTransition(1, 7, "incident:container:x", transition_type, datetime.now(timezone.utc), None, IncidentStatus.OPEN if transition_type is not NotificationTrigger.INCIDENT_RECOVERED else IncidentStatus.RESOLVED, None, Severity.CRITICAL, None, None, "corr", ResourceRef(ResourceType.CONTAINER, "container-x", "container-x"), EventType.CONTAINER_RESTARTING)


def test_policy_matches_critical_opening():
    policy = NotificationPolicy("critical", "Critical", True, Severity.HIGH, (EventType.CONTAINER_RESTARTING,), (ResourceType.CONTAINER,), (), (NotificationTrigger.INCIDENT_OPENED,), False, False, False, 10, 60, 3, ("mock",))
    decision = evaluate(policy, "mock", transition())
    assert decision.matched is True
    assert decision.identity_key


def test_policy_suppresses_recovery_when_disabled():
    policy = NotificationPolicy("critical", "Critical", True, Severity.HIGH, (), (), (), (NotificationTrigger.INCIDENT_RECOVERED,), False, False, False, 10, 60, 3, ("mock",))
    decision = evaluate(policy, "mock", transition(NotificationTrigger.INCIDENT_RECOVERED))
    assert decision.suppressed is True
    assert decision.reason == "recovery_disabled"


def test_projector_is_idempotent_and_worker_uses_mock_adapter(tmp_path):
    repository = SQLiteNotificationRepository(tmp_path / "mc.db")
    with repository._connection() as connection:
        connection.execute("INSERT INTO incidents (id, incident_key, title, severity, status, started_at, updated_at, resource_type, resource_id, correlation_key, summary) VALUES (7, 'incident:container:x', 'test', 'critical', 'open', 1, 1, 'container', 'container-x', 'corr', 'test')")
    repository.add_transition(transition())
    channel = NotificationChannel("mock", "Mock", "mock", True)
    policy = NotificationPolicy("critical", "Critical", True, Severity.CRITICAL, (), (), (), (NotificationTrigger.INCIDENT_OPENED,), False, False, False, 10, 60, 3, ("mock",))
    projector = NotificationProjector(repository, (policy,), (channel,))
    assert projector.project_once() == 1
    assert projector.project_once() == 0
    rows = repository.get_notifications(NotificationFilter(include_suppressed=True))
    assert len(rows) == 1

    class Adapter:
        channel_type = "mock"
        def send(self, notification, context):
            assert context.secret is None
            return DeliveryResult(DeliveryStatus.SENT, False, provider_message_id="m1")

    worker = NotificationWorker(repository, ChannelRegistry({"mock": Adapter()}), (channel,))
    assert worker.deliver_once() is True
    assert repository.get_notification(rows[0].id).status is NotificationStatus.SENT


def test_worker_records_retryable_failure(tmp_path):
    repository = SQLiteNotificationRepository(tmp_path / "mc.db")
    with repository._connection() as connection:
        connection.execute("INSERT INTO incidents (id, incident_key, title, severity, status, started_at, updated_at, resource_type, resource_id, correlation_key, summary) VALUES (7, 'incident:container:x', 'test', 'critical', 'open', 1, 1, 'container', 'container-x', 'corr', 'test')")
    repository.add_transition(transition())
    channel = NotificationChannel("mock", "Mock", "mock", True)
    policy = NotificationPolicy("critical", "Critical", True, Severity.CRITICAL, (), (), (), (NotificationTrigger.INCIDENT_OPENED,), False, False, False, 10, 60, 3, ("mock",))
    NotificationProjector(repository, (policy,), (channel,)).project_once()

    class Adapter:
        channel_type = "mock"
        def send(self, notification, context):
            return DeliveryResult(DeliveryStatus.FAILED, True, error_code="timeout", error_message="safe timeout")

    worker = NotificationWorker(repository, ChannelRegistry({"mock": Adapter()}), (channel,))
    assert worker.deliver_once() is True
    row = repository.get_notifications(NotificationFilter(include_suppressed=True))[0]
    assert row.status is NotificationStatus.PENDING
    assert row.next_attempt_at is not None
