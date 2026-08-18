"""Safe public mappers for MC-6.6 project intelligence."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from aipm.models.project_intelligence import ProjectApplication, ProjectComponent, ProjectEvidence, ProjectHealth


class ProjectIntelligenceMapper:
    """Map project intelligence into bounded JSON-safe dashboard payloads."""

    @classmethod
    def inventory(cls, value: Any) -> dict[str, Any]:
        return {
            "available": True,
            "status": "ok",
            "error": None,
            "observation": cls._observation(getattr(value, "freshness", {})),
            "inventory_scope": getattr(getattr(value, "inventory_scope", None), "value", "all"),
            "search_paths": list(getattr(value, "search_paths", ()))[:32],
            "projects": [cls.project(item) for item in getattr(value, "projects", ())],
            "local_candidates": [cls.project(item) for item in getattr(value, "local_candidates", ())],
            "source_errors": list(getattr(value, "source_errors", ()))[:8],
            "truncated": False,
        }

    @classmethod
    def project(cls, value: ProjectApplication) -> dict[str, Any]:
        return {
            "id": value.id,
            "display_name": value.display_name,
            "source": value.source.value,
            "confidence": value.confidence.value,
            "inventory_scope": value.inventory_scope.value,
            "association_role": value.association_role.value,
            "association_explanation": value.association_explanation[:256],
            "local_project_id": value.local_project_id,
            "local_project_name": value.local_project_name,
            "runtime_group": value.runtime_group,
            "components": [cls.component(item) for item in value.components],
            "component_count": len(value.components),
            "git": cls._bounded_dict(value.git, {"available", "status", "branch", "detached", "ahead", "behind", "conflicted", "modified_count", "untracked_count"}),
            "compose": cls._bounded_dict(value.compose, {"available", "status", "file_names", "service_count", "runtime_component_count", "error"}),
            "runtime": cls._bounded_dict(value.runtime, {"group", "component_count", "running"}),
            "health": cls.health(value.health),
            "freshness": cls._observation(value.freshness),
            "evidence": [cls.evidence(item) for item in value.evidence[:24]],
            "warnings": list(value.warnings[:8]),
        }

    @classmethod
    def component(cls, value: ProjectComponent) -> dict[str, Any]:
        return {
            "id": value.id,
            "name": value.name,
            "service_name": value.service_name,
            "container_id": value.container_id,
            "state": value.state,
            "health": value.health,
            "restart_count": value.restart_count,
            "image": value.image,
            "resources": cls._bounded_dict(value.resources, {"available", "cpu_percent", "memory_used_mb", "memory_limit_mb", "freshness"}),
            "evidence": [cls.evidence(item) for item in value.evidence[:8]],
        }

    @classmethod
    def health(cls, value: ProjectHealth) -> dict[str, Any]:
        return {
            "status": value.status.value,
            "summary": value.summary,
            "counts": {str(key)[:64]: max(0, int(item)) for key, item in list(value.counts.items())[:16]},
            "evidence": [cls.evidence(item) for item in value.evidence[:24]],
        }

    @staticmethod
    def evidence(value: ProjectEvidence) -> dict[str, Any]:
        return {
            "code": value.code[:64],
            "severity": value.severity[:16],
            "source": value.source[:32],
            "message": value.message[:256],
            "freshness": value.freshness[:32],
            "observed_at": value.observed_at,
        }

    @staticmethod
    def _observation(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "transport_ok": value.get("status") != "unavailable",
            "available": value.get("status") not in {"unavailable", "never_sampled"},
            "state": value.get("status", "unknown"),
            "observed_at": value.get("sampled_at"),
            "age_seconds": value.get("age_seconds"),
            "max_age_seconds": 180,
            "error": value.get("error"),
        }

    @staticmethod
    def _bounded_dict(value: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
        return {key: value[key] for key in allowed if key in value}


__all__ = ["ProjectIntelligenceMapper"]
