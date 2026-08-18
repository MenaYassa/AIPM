"""Read-only project/application intelligence aggregation for Mission Control."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from aipm.mappers.docker_detail import DockerDetailMapper
from aipm.services.compose.service import ComposeService
from aipm.models.mission_control import ObservationError
from aipm.models.project import Project
from aipm.models.project_intelligence import (
    AssociationConfidence,
    AssociationRole,
    InventoryScope,
    ProjectApplication,
    ProjectComponent,
    ProjectEvidence,
    ProjectHealth,
    ProjectHealthStatus,
    ProjectInventory,
    ProjectSource,
)
from aipm.models.telemetry import TelemetryError


class ProjectIntelligenceService:
    """Compose bounded local-project and Docker observations without mutation."""

    MAX_PROJECTS = 200
    MAX_COMPONENTS = 200
    MAX_EVIDENCE = 24
    STALE_AFTER_SECONDS = 180

    def __init__(
        self,
        project_service: Any,
        docker_observation: Any,
        docker_telemetry: Any,
        *,
        compose_service: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.project_service = project_service
        self.docker_observation = docker_observation
        self.docker_telemetry = docker_telemetry
        self.compose_service = compose_service
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def inventory(self, *, limit: int = MAX_PROJECTS, search: str | None = None, status: str | None = None, scope: str | InventoryScope = InventoryScope.ALL) -> ProjectInventory:
        bounded = self._limit(limit, self.MAX_PROJECTS)
        scope_value = self._scope(scope)
        projects, project_error = self._discover_projects()
        details, runtime_error, observed_at = self._runtime_components()
        applications = self._aggregate(projects, details, observed_at, project_error, runtime_error)
        query = self._safe_filter(search)
        if query:
            applications = [item for item in applications if query in item.display_name.lower() or any(query in component.name.lower() for component in item.components)]
        if status in {item.value for item in ProjectHealthStatus}:
            applications = [item for item in applications if item.health.status.value == status]
        applications = sorted(applications, key=lambda item: item.display_name.lower())
        local_candidates = tuple(replace(item, inventory_scope=scope_value) for item in applications if item.association_role is AssociationRole.LOCAL_CANDIDATE)
        selected = tuple(replace(item, inventory_scope=scope_value) for item in applications if self._in_scope(item, scope_value))[:bounded]
        freshness = self._freshness(observed_at, available=not bool(runtime_error), error=runtime_error)
        errors = tuple(value for value in (project_error, runtime_error) if value)
        return ProjectInventory(
            projects=selected,
            search_paths=tuple(self._search_paths()),
            freshness=freshness,
            source_errors=errors,
            inventory_scope=scope_value,
            local_candidates=local_candidates[: self.MAX_PROJECTS],
        )

    def detail(self, project_id: str) -> ProjectApplication | None:
        project_id = self._identifier(project_id)
        if project_id is None:
            return None
        return next((item for item in self.inventory(limit=self.MAX_PROJECTS).projects if item.id == project_id), None)

    def health(self, project_id: str) -> ProjectHealth | None:
        detail = self.detail(project_id)
        return detail.health if detail else None

    def containers(self, project_id: str) -> tuple[ProjectComponent, ...] | None:
        detail = self.detail(project_id)
        return detail.components if detail else None

    def _discover_projects(self) -> tuple[list[Project], str | None]:
        try:
            return list(self.project_service.discover()), None
        except Exception:
            return [], "Project discovery unavailable"

    def _runtime_components(self) -> tuple[list[Any], str | None, datetime | None]:
        try:
            snapshot = self.docker_telemetry.fast_snapshot(now=self.clock())
            observed_at = getattr(snapshot, "state_sampled_at", None)
            values = []
            for item in getattr(snapshot, "containers", ()):
                raw = getattr(item, "container", item)
                resources = getattr(item, "resources", None)
                if hasattr(raw, "project_key") and hasattr(raw, "restart_count"):
                    values.append(raw)
                else:
                    values.append(DockerDetailMapper.container(raw, resources=resources))
            return values[: self.MAX_COMPONENTS], None, observed_at
        except Exception:
            try:
                raw_values = self.docker_observation.containers()
                return [DockerDetailMapper.container(item) for item in raw_values[: self.MAX_COMPONENTS]], None, self.clock()
            except Exception:
                return [], "Docker runtime observation unavailable", None

    def _aggregate(
        self,
        projects: list[Project],
        components: list[Any],
        observed_at: datetime | None,
        project_error: str | None,
        runtime_error: str | None,
    ) -> list[ProjectApplication]:
        result: list[ProjectApplication] = []
        matched_runtime: set[str] = set()
        for project in projects[: self.MAX_PROJECTS]:
            group = self._matching_group(project, components)
            selected = [item for item in components if (item.project_key or "ungrouped") == group] if group else []
            if group:
                matched_runtime.add(group)
            evidence = []
            if group:
                confidence = AssociationConfidence.EXACT
                evidence_code = "EXACT_COMPOSE_PROJECT_IDENTITY"
                evidence.append(ProjectEvidence(evidence_code, "info", "docker", f"Runtime group matched local Compose project {project.name}"))
                role = AssociationRole.ASSOCIATED_LOCAL
                explanation = "Runtime group has a local Compose project association supported by Docker project identity."
            else:
                confidence = AssociationConfidence.UNKNOWN
                evidence.append(ProjectEvidence("LOCAL_PROJECT_NO_RUNTIME_MATCH", "warning", "association", "Local project discovered; no trustworthy runtime group was matched"))
                role = AssociationRole.LOCAL_CANDIDATE
                explanation = "Local project discovered without a trustworthy runtime association; shown as a local candidate."
            if project_error:
                evidence.append(ProjectEvidence("PROJECT_DISCOVERY_UNAVAILABLE", "warning", "project", project_error))
            app = self._application(project.name, ProjectSource.DISCOVERED, confidence, project, group, selected, observed_at, evidence, runtime_error, association_role=role, association_explanation=explanation)
            result.append(app)
        runtime_groups = sorted({item.project_key for item in components if item.project_key and item.project_key not in matched_runtime})
        for group in runtime_groups:
            selected = [item for item in components if item.project_key == group]
            evidence = (ProjectEvidence("RUNTIME_ONLY_GROUP", "warning", "docker", "No discovered local project matched this runtime group"),)
            result.append(self._application(group, ProjectSource.RUNTIME_GROUP, AssociationConfidence.UNKNOWN, None, group, selected, observed_at, evidence, runtime_error, association_role=AssociationRole.RUNTIME_ONLY, association_explanation="Runtime group observed; no trusted local project root matched."))
        ungrouped = [item for item in components if not item.project_key]
        if ungrouped:
            evidence = (ProjectEvidence("UNGROUPED_RUNTIME", "warning", "docker", "Container has no trustworthy project association"),)
            result.append(self._application("Ungrouped runtime", ProjectSource.UNGROUPED, AssociationConfidence.UNKNOWN, None, None, ungrouped, observed_at, evidence, runtime_error, association_role=AssociationRole.UNGROUPED, association_explanation="Runtime components lack a trustworthy Compose or project identity."))
        return result

    def _application(
        self,
        name: str,
        source: ProjectSource,
        confidence: AssociationConfidence,
        project: Project | None,
        group: str | None,
        values: list[Any],
        observed_at: datetime | None,
        evidence: list[ProjectEvidence] | tuple[ProjectEvidence, ...],
        runtime_error: str | None,
        *,
        association_role: AssociationRole,
        association_explanation: str,
    ) -> ProjectApplication:
        components = tuple(self._component(item) for item in values[: self.MAX_COMPONENTS])
        all_evidence = list(evidence)
        if runtime_error:
            all_evidence.append(ProjectEvidence("DOCKER_RUNTIME_UNAVAILABLE", "warning", "docker", runtime_error))
        health = self._health(components, all_evidence)
        project_path = getattr(project, "path", None)
        identity = f"{source.value}:{project_path or name}:{group or ''}"
        identifier = hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:24]
        git = self._git(project)
        compose = self._compose(project, values)
        runtime = {"group": group, "component_count": len(components), "running": sum(item.state == "running" for item in components)}
        warnings = tuple(item.message for item in all_evidence if item.severity in {"warning", "error"})[:8]
        local_project_id = hashlib.sha256(f"local:{project_path}".encode("utf-8", "replace")).hexdigest()[:24] if project_path else None
        return ProjectApplication(
            id=identifier,
            display_name=name[:128],
            source=source,
            confidence=confidence,
            local_project_name=getattr(project, "name", None),
            local_project_path=project_path,
            runtime_group=group,
            components=components,
            git=git,
            compose=compose,
            runtime=runtime,
            health=health,
            freshness=self._freshness(observed_at, available=not bool(runtime_error), error=runtime_error),
            evidence=tuple(all_evidence[: self.MAX_EVIDENCE]),
            warnings=warnings,
            inventory_scope=InventoryScope.ALL,
            association_role=association_role,
            association_explanation=association_explanation,
            local_project_id=local_project_id,
        )

    def _component(self, item: Any) -> ProjectComponent:
        resources = getattr(item, "resources", None)
        resource_payload = {
            "available": bool(getattr(resources, "available", False)) if resources else False,
            "cpu_percent": getattr(resources, "cpu_percent", None) if resources else None,
            "memory_used_mb": getattr(resources, "memory_used_mb", None) if resources else None,
            "memory_limit_mb": getattr(resources, "memory_limit_mb", None) if resources else None,
            "freshness": getattr(getattr(resources, "freshness", None), "status", None).value if resources and getattr(resources, "freshness", None) else "never_sampled",
        }
        evidence: list[ProjectEvidence] = []
        if getattr(item, "health", None) is None:
            evidence.append(ProjectEvidence("MISSING_HEALTH_CHECK", "warning", "docker", "Container health check is not configured"))
        return ProjectComponent(
            id=str(getattr(item, "id", ""))[:128],
            name=str(getattr(item, "name", "unknown"))[:128],
            service_name=getattr(item, "service_name", None),
            container_id=str(getattr(item, "id", ""))[:128],
            state=str(getattr(item, "state", "unknown"))[:32],
            health=getattr(item, "health", None),
            restart_count=max(0, int(getattr(item, "restart_count", 0) or 0)),
            image=str(getattr(item, "image", ""))[:256] or None,
            resources=resource_payload,
            evidence=tuple(evidence),
        )

    def _health(self, components: tuple[ProjectComponent, ...], evidence: list[ProjectEvidence]) -> ProjectHealth:
        for component in components:
            evidence.extend(component.evidence)
        counts = {
            "total": len(components),
            "running": sum(item.state == "running" for item in components),
            "stopped": sum(item.state in {"exited", "dead", "created"} for item in components),
            "restarting": sum(item.state == "restarting" for item in components),
            "unhealthy": sum(item.health == "unhealthy" for item in components),
            "missing_health_check": sum(item.health is None for item in components),
            "restarts": sum(item.restart_count for item in components),
        }
        if not components:
            status = ProjectHealthStatus.UNKNOWN
            summary = "No trustworthy runtime components are available"
            evidence.append(ProjectEvidence("NO_RUNTIME_COMPONENTS", "warning", "runtime", summary))
        elif counts["unhealthy"] or counts["stopped"]:
            status = ProjectHealthStatus.RED
            summary = "One or more observed components are unhealthy or stopped"
        elif counts["restarting"] or counts["missing_health_check"] or counts["restarts"]:
            status = ProjectHealthStatus.YELLOW
            summary = "Runtime evidence contains warnings or incomplete health checks"
        elif counts["running"] == counts["total"] and all(item.health == "healthy" for item in components):
            status = ProjectHealthStatus.GREEN
            summary = "All observed components are running and healthy"
        else:
            status = ProjectHealthStatus.UNKNOWN
            summary = "Evidence is insufficient to classify application health"
        return ProjectHealth(status, summary, counts, tuple(evidence[: self.MAX_EVIDENCE]))

    @staticmethod
    def _git(project: Project | None) -> dict[str, Any]:
        git = getattr(project, "git", None)
        if git is None:
            return {"available": False, "status": "unavailable", "error": "Git posture unavailable"}
        return {
            "available": bool(getattr(git, "exists", True)),
            "status": "dirty" if getattr(git, "dirty", False) else "clean",
            "branch": str(getattr(git, "branch", None) or "detached")[:128],
            "detached": bool(getattr(git, "detached", False)),
            "ahead": max(0, int(getattr(git, "ahead", 0) or 0)),
            "behind": max(0, int(getattr(git, "behind", 0) or 0)),
            "conflicted": bool(getattr(git, "conflicted_files", [])),
            "modified_count": min(len(getattr(git, "modified_files", []) or []), 200),
            "untracked_count": min(len(getattr(git, "untracked_files", []) or []), 200),
        }

    def _compose(self, project: Project | None, values: list[Any]) -> dict[str, Any]:
        if project is None:
            return {"available": False, "status": "unavailable", "error": "Compose posture unavailable"}
        files = [str(value).split("/")[-1][:128] for value in getattr(project, "compose_files", [])[:16]]
        payload = {
            "available": bool(files),
            "status": "configured" if files else "unavailable",
            "file_names": files,
            "service_count": len(getattr(project, "services", []) or []),
            "runtime_component_count": len(values),
        }
        if not files or self.compose_service is None:
            return payload
        try:
            status = self.compose_service.status(project)
            payload.update({
                "status": "observed",
                "running": max(0, int(getattr(status, "running", 0) or 0)),
                "stopped": max(0, int(getattr(status, "stopped", 0) or 0)),
                "restarting": max(0, int(getattr(status, "restarting", 0) or 0)),
                "unhealthy": max(0, int(getattr(status, "unhealthy", 0) or 0)),
            })
        except Exception:
            payload.update({"available": False, "status": "unavailable", "error": "Compose runtime observation unavailable"})
        return payload

    def _matching_group(self, project: Project, components: list[Any]) -> str | None:
        identity = self._compose_identity(project)
        if identity is None:
            return None
        for item in components:
            group = getattr(item, "project_key", None)
            if group and str(group).strip().lower() == identity:
                return str(group)
        return None

    @staticmethod
    def _compose_identity(project: Project) -> str | None:
        compose_files = list(getattr(project, "compose_files", []) or [])[:4]
        if not compose_files:
            return None
        for filename in compose_files:
            try:
                with Path(filename).open("r", encoding="utf-8", errors="replace") as handle:
                    for raw_line in handle.read(16384).splitlines():
                        line = raw_line.strip()
                        if not line or line.startswith("#") or raw_line[:1].isspace() or not line.startswith("name:"):
                            continue
                        value = line.split(":", 1)[1].strip().strip("'\"").lower()
                        if value and len(value) <= 128 and all(char.isalnum() or char in "-_." for char in value):
                            return value
            except (OSError, UnicodeError):
                continue
        project_name = str(getattr(project, "name", "")).strip().lower()
        return project_name if project_name and len(project_name) <= 128 else None

    def _freshness(self, observed_at: datetime | None, *, available: bool, error: str | None) -> dict[str, Any]:
        if error:
            return {"status": "unavailable", "sampled_at": None, "age_seconds": None, "error": error}
        if observed_at is None:
            return {"status": "never_sampled", "sampled_at": None, "age_seconds": None, "error": None}
        age = max(0.0, (self.clock() - observed_at).total_seconds())
        return {"status": "fresh" if age <= self.STALE_AFTER_SECONDS else "stale", "sampled_at": observed_at.isoformat(), "age_seconds": round(age, 1), "error": None}

    def _search_paths(self) -> list[str]:
        try:
            return [str(value).split("/")[-1][:128] for value in self.project_service.app.config.discovery.search_paths]
        except Exception:
            return []

    @staticmethod
    def _scope(value: str | InventoryScope) -> InventoryScope:
        try:
            return value if isinstance(value, InventoryScope) else InventoryScope(str(value or "all").strip().lower())
        except ValueError:
            return InventoryScope.ALL

    @staticmethod
    def _in_scope(item: ProjectApplication, scope: InventoryScope) -> bool:
        if scope is InventoryScope.ALL:
            return True
        if scope is InventoryScope.LOCAL:
            return item.association_role is AssociationRole.LOCAL_CANDIDATE
        if scope is InventoryScope.ASSOCIATED:
            return item.association_role is AssociationRole.ASSOCIATED_LOCAL
        return item.association_role in {AssociationRole.APPLICATION, AssociationRole.ASSOCIATED_LOCAL, AssociationRole.RUNTIME_ONLY, AssociationRole.UNGROUPED}

    @staticmethod
    def _safe_filter(value: str | None) -> str:
        return str(value or "").strip().lower()[:128]

    @staticmethod
    def _identifier(value: str | None) -> str | None:
        value = str(value or "").strip()
        return value if len(value) == 24 and all(char in "0123456789abcdef" for char in value) else None

    @staticmethod
    def _limit(value: int, maximum: int) -> int:
        try:
            return max(1, min(int(value), maximum))
        except (TypeError, ValueError):
            return maximum


__all__ = ["ProjectIntelligenceService", "ObservationError", "TelemetryError"]
