from __future__ import annotations

import hashlib
from datetime import datetime

from aipm.engines.health.engine import HealthEngine
from aipm.models.events import Event, EventSource, EventType, ResourceRef, ResourceType
from aipm.models.finding import Finding
from aipm.models.health_observation import HealthFindingRecord, HealthObservation
from aipm.models.history import ProjectHistoryPoint
from aipm.services.project.service import ProjectService


class HealthEvidenceService:
    """Run the existing deterministic HealthEngine and normalize its evidence."""

    def __init__(self, project_service: ProjectService, health_engine: HealthEngine):
        self.project_service = project_service
        self.health_engine = health_engine

    def observe(self, source_run_id: int, sampled_at: datetime, project_points: tuple[ProjectHistoryPoint, ...]) -> tuple[HealthObservation, ...]:
        discovered = {project.path: project for project in self.project_service.discover()}
        observations: list[HealthObservation] = []
        for point in project_points:
            project = discovered.get(point.path or "")
            if project is None:
                continue
            report = self.health_engine.analyze(project)
            findings = tuple(_finding_record(finding) for finding in report.findings)
            observations.append(
                HealthObservation(
                    id=None,
                    source_run_id=source_run_id,
                    sampled_at=sampled_at,
                    project_path=project.path,
                    project_name=project.name,
                    report_state=report.state,
                    score=report.score,
                    findings=findings,
                )
            )
        return tuple(observations)

    def transition_events(self, current: tuple[HealthObservation, ...], previous_lookup) -> tuple[Event, ...]:
        events: list[Event] = []
        for observation in current:
            previous = previous_lookup(observation.project_path)
            if previous is None:
                continue
            if previous.report_state != observation.report_state:
                severity = _health_severity(observation.report_state.value)
                resource = ResourceRef(ResourceType.PROJECT, observation.project_path, observation.project_name, observation.project_path)
                events.append(
                    _health_event(
                        observation,
                        previous.source_run_id,
                        EventType.HEALTH_STATE_CHANGED,
                        severity,
                        "Project health state changed",
                        f"Project health changed from {previous.report_state.value} to {observation.report_state.value}.",
                        previous.report_state.value,
                        observation.report_state.value,
                        resource,
                        f"project:{observation.project_path}:health",
                    )
                )
            previous_fingerprints = {finding.fingerprint: finding for finding in previous.findings}
            current_fingerprints = {finding.fingerprint: finding for finding in observation.findings}
            if previous_fingerprints != current_fingerprints:
                resource = ResourceRef(ResourceType.PROJECT, observation.project_path, observation.project_name, observation.project_path)
                events.append(
                    _health_event(
                        observation,
                        previous.source_run_id,
                        EventType.HEALTH_FINDING_CHANGED,
                        max((finding.severity for finding in observation.findings), default=_health_severity("healthy"), key=_severity_rank),
                        "Project health findings changed",
                        "The deterministic HealthEngine finding set changed.",
                        str(len(previous_fingerprints)),
                        str(len(current_fingerprints)),
                        resource,
                        f"project:{observation.project_path}:health",
                    )
                )
        return tuple(events)


def _finding_record(finding: Finding) -> HealthFindingRecord:
    fingerprint = hashlib.sha256(
        "|".join((finding.code, finding.component, finding.resource or "", finding.severity.value, finding.title)).encode("utf-8")
    ).hexdigest()
    return HealthFindingRecord(fingerprint, finding.code, finding.component, finding.severity, finding.title, finding.description, finding.resource)


def _health_event(observation, previous_run_id, event_type, severity, title, description, previous_value, current_value, resource, correlation_key):
    import hashlib
    event_key = hashlib.sha256("|".join((str(previous_run_id), str(observation.source_run_id), event_type.value, resource.identifier, previous_value, current_value)).encode("utf-8")).hexdigest()
    return Event(None, event_key, observation.sampled_at, event_type, severity, EventSource.HEALTH_ENGINE, resource, title, description, previous_value, current_value, observation.source_run_id, previous_run_id, correlation_key)


def _health_severity(state: str):
    from aipm.models.finding import Severity
    return {"critical": Severity.CRITICAL, "degraded": Severity.WARNING, "healthy": Severity.INFO, "unknown": Severity.WARNING}.get(state, Severity.WARNING)


def _severity_rank(severity):
    from aipm.models.finding import Severity
    return {Severity.INFO: 1, Severity.WARNING: 2, Severity.HIGH: 3, Severity.CRITICAL: 4}[severity]
