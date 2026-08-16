from datetime import datetime, timezone

from aipm.models.history import ContainerHistoryPoint, HistoricalRun, ProjectHistoryPoint, TunnelHistoryPoint
from aipm.models.events import EventType
from aipm.services.events.derivation import EventDerivationService
from aipm.services.events.frame import HistoricalFrame


UTC = timezone.utc
NOW = datetime(2026, 8, 16, tzinfo=UTC)


def run(identifier: int) -> HistoricalRun:
    return HistoricalRun(identifier, NOW, True, True, True, "healthy")


def container(state: str, restart_count: int = 0, health: str | None = "healthy") -> ContainerHistoryPoint:
    return ContainerHistoryPoint(NOW, "cid", "app", "app:latest", state, health, "stack", restart_count, 1.0, 2.0, 10.0, 20.0, True)


def frame(previous_container, current_container, previous_project=None, current_project=None, previous_tunnel=None, current_tunnel=None):
    return HistoricalFrame(
        current=run(2),
        previous=run(1),
        current_containers=(current_container,) if current_container else (),
        previous_containers=(previous_container,) if previous_container else (),
        current_projects=(current_project,) if current_project else (),
        previous_projects=(previous_project,) if previous_project else (),
        current_tunnel=current_tunnel,
        previous_tunnel=previous_tunnel,
    )


def test_no_previous_frame_emits_no_transition_events():
    current = HistoricalFrame(run(1), None, (container("running"),), (), (), (), None, None)
    assert EventDerivationService().derive(current) == ()


def test_restarting_transition_emits_one_event_with_stable_key():
    service = EventDerivationService()
    current_frame = frame(container("running"), container("restarting"))
    first = service.derive(current_frame)
    second = service.derive(current_frame)
    assert [item.event_type for item in first] == [EventType.CONTAINER_RESTARTING]
    assert first[0].event_key == second[0].event_key


def test_recovery_transition_emits_recovery_and_restart_counter_event():
    events = EventDerivationService().derive(frame(container("restarting", 1), container("running", 2)))
    assert EventType.CONTAINER_RECOVERED in [item.event_type for item in events]
    assert EventType.CONTAINER_RESTARTED in [item.event_type for item in events]


def test_unchanged_container_emits_no_event():
    assert EventDerivationService().derive(frame(container("running"), container("running"))) == ()


def test_git_state_transition_emits_project_event():
    previous = ProjectHistoryPoint(NOW, "demo", "/srv/demo", "main", True, True, False, 0, 0)
    current = ProjectHistoryPoint(NOW, "demo", "/srv/demo", "main", True, True, True, 0, 0)
    events = EventDerivationService().derive(frame(None, None, previous, current))
    assert len(events) == 1
    assert events[0].event_type is EventType.PROJECT_GIT_STATE_CHANGED


def test_tunnel_down_transition_emits_high_event():
    previous = TunnelHistoryPoint(NOW, "healthy", "docker", "active", ("cloudflared",))
    current = TunnelHistoryPoint(NOW, "down", "local-agent", "failed", ("cloudflared",))
    events = EventDerivationService().derive(frame(None, None, None, None, previous, current))
    assert len(events) == 1
    assert events[0].event_type is EventType.TUNNEL_STATE_CHANGED
    assert events[0].severity.value == "high"
