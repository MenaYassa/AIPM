from __future__ import annotations

import hashlib
import json
from typing import Iterable

from aipm.models.events import Event, EventSource, EventType, ResourceRef, ResourceType
from aipm.models.finding import Severity
from aipm.models.history import ContainerHistoryPoint, ProjectHistoryPoint
from aipm.services.events.frame import HistoricalFrame


class EventDerivationService:
    """Derive deterministic transition events from typed current/previous facts."""

    def derive(self, frame: HistoricalFrame, health_events: Iterable[Event] = ()) -> tuple[Event, ...]:
        if frame.previous is None:
            return tuple(health_events)
        events: list[Event] = []
        events.extend(self._containers(frame))
        events.extend(self._projects(frame))
        events.extend(self._tunnel(frame))
        events.extend(health_events)
        return tuple(events)

    def _containers(self, frame: HistoricalFrame) -> list[Event]:
        previous = {item.container_id: item for item in frame.previous_containers}
        events: list[Event] = []
        for current in frame.current_containers:
            prior = previous.get(current.container_id)
            if prior is None:
                continue
            if prior.state != "restarting" and current.state == "restarting":
                events.append(self._container_event(frame, current, prior, EventType.CONTAINER_RESTARTING, Severity.HIGH, "Container entered restarting", "The container state changed into restarting."))
            if prior.state not in {"running", "restarting"} and current.state == "running":
                events.append(self._container_event(frame, current, prior, EventType.CONTAINER_STARTED, Severity.INFO, "Container started", "The container transitioned into running."))
            if prior.state in {"running", "restarting"} and current.state in {"exited", "dead"}:
                events.append(self._container_event(frame, current, prior, EventType.CONTAINER_STOPPED, Severity.HIGH, "Container stopped", "The container transitioned into a stopped state."))
            if prior.state == "restarting" and current.state == "running":
                events.append(self._container_event(frame, current, prior, EventType.CONTAINER_RECOVERED, Severity.INFO, "Container recovered", "The container returned from restarting to running."))
            if prior.restart_count is not None and current.restart_count is not None and current.restart_count > prior.restart_count:
                events.append(self._container_event(frame, current, prior, EventType.CONTAINER_RESTARTED, Severity.WARNING, "Container restarted", "The observed Docker restart counter increased."))
            if prior.health and current.health and prior.health != current.health:
                severity = Severity.INFO if current.health == "healthy" else Severity.WARNING
                events.append(self._container_event(frame, current, prior, EventType.CONTAINER_HEALTH_CHANGED, severity, "Container health changed", f"Container health changed from {prior.health} to {current.health}."))
                if prior.health == "unhealthy" and current.health == "healthy":
                    events.append(self._container_event(frame, current, prior, EventType.CONTAINER_RECOVERED, Severity.INFO, "Container recovered", "The container health check returned to healthy."))
        return events

    def _projects(self, frame: HistoricalFrame) -> list[Event]:
        previous = {item.path or item.name: item for item in frame.previous_projects}
        events: list[Event] = []
        for current in frame.current_projects:
            key = current.path or current.name
            prior = previous.get(key)
            if prior is None or self._project_signature(prior) == self._project_signature(current):
                continue
            resource = ResourceRef(ResourceType.PROJECT, key, current.name, current.path)
            events.append(
                self._event(
                    frame,
                    EventType.PROJECT_GIT_STATE_CHANGED,
                    Severity.WARNING if current.dirty or (current.behind or 0) > 0 else Severity.INFO,
                    resource,
                    "Project Git state changed",
                    "The observed project Git branch, dirty, ahead, or behind state changed.",
                    self._project_signature(prior),
                    self._project_signature(current),
                    f"project:{key}:git",
                )
            )
        return events

    def _tunnel(self, frame: HistoricalFrame) -> list[Event]:
        prior = frame.previous_tunnel
        current = frame.current_tunnel
        if prior is None or current is None or prior.state == "unknown" or current.state == "unknown" or prior.state == current.state:
            return []
        resource = ResourceRef(ResourceType.TUNNEL, "local", "cloudflared")
        severity = Severity.HIGH if current.state == "down" else Severity.INFO
        return [
            self._event(
                frame,
                EventType.TUNNEL_STATE_CHANGED,
                severity,
                resource,
                "Tunnel state changed",
                f"Local tunnel state changed from {prior.state} to {current.state}.",
                prior.state,
                current.state,
                "tunnel:local:availability",
            )
        ]

    def _container_event(self, frame, current: ContainerHistoryPoint, prior: ContainerHistoryPoint, event_type, severity, title, description) -> Event:
        resource = ResourceRef(ResourceType.CONTAINER, current.container_id, current.container_name, current.stack)
        if event_type is EventType.CONTAINER_RESTARTED:
            previous_value, current_value = str(prior.restart_count), str(current.restart_count)
        elif event_type is EventType.CONTAINER_HEALTH_CHANGED:
            previous_value, current_value = prior.health, current.health
        else:
            previous_value, current_value = prior.state, current.state
        return self._event(
            frame,
            event_type,
            severity,
            resource,
            title,
            description,
            previous_value,
            current_value,
            f"container:{current.container_id}:stability",
        )

    def _event(self, frame, event_type, severity, resource, title, description, previous_value, current_value, correlation_key) -> Event:
        event_key = _event_key(frame.previous.id if frame.previous else None, frame.current.id, event_type.value, resource.identifier, previous_value, current_value)
        return Event(
            id=None,
            event_key=event_key,
            occurred_at=frame.current.sampled_at,
            event_type=event_type,
            severity=severity,
            source=EventSource.DERIVED,
            resource=resource,
            title=title,
            description=description,
            previous_value=previous_value,
            current_value=current_value,
            source_run_id=frame.current.id,
            previous_run_id=frame.previous.id if frame.previous else None,
            correlation_key=correlation_key,
        )

    @staticmethod
    def _project_signature(project: ProjectHistoryPoint) -> str:
        return json.dumps(
            {"branch": project.branch, "dirty": project.dirty, "ahead": project.ahead, "behind": project.behind},
            sort_keys=True,
        )


def _event_key(previous_run_id: int | None, source_run_id: int, event_type: str, resource_id: str, previous_value: str | None, current_value: str | None) -> str:
    payload = json.dumps(
        [previous_run_id, source_run_id, event_type, resource_id, previous_value, current_value],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
