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
            "filtered_candidates": [cls.project(item) for item in getattr(value, "filtered_candidates", ())],
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
            "candidate_classification": value.candidate_classification,
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

    @classmethod
    def compose_project(cls, application: ProjectApplication, observation: Any) -> dict[str, Any]:
        services = sorted(
            [cls.compose_service(s) for s in getattr(observation, "services", ())],
            key=lambda item: item["service_name"],
        )
        return {
            "id": application.id,
            "display_name": application.display_name,
            "compose_identity": getattr(observation, "compose_identity", "unknown"),
            "running_services_count": int(getattr(observation, "running_services_count", 0)),
            "total_services_count": int(getattr(observation, "total_services_count", 0)),
            "updates_available_count": int(getattr(observation, "updates_available_count", 0)),
            "current_count": int(getattr(observation, "current_count", 0)),
            "drift_count": int(getattr(observation, "drift_count", 0)),
            "not_applicable_count": int(getattr(observation, "not_applicable_count", 0)),
            "unknown_count": int(getattr(observation, "unknown_count", 0)),
            "freshness": str(getattr(observation, "freshness", "never_sampled")),
            "services": services,
        }

    @classmethod
    def compose_service(cls, service: Any) -> dict[str, Any]:
        candidate_status = getattr(service, "candidate_status", None)
        status_val = candidate_status.value if hasattr(candidate_status, "value") else str(candidate_status or "unknown")

        candidate_reason = getattr(service, "candidate_reason", None)
        reason_val = candidate_reason.value if hasattr(candidate_reason, "value") else str(candidate_reason or "unknown")

        candidate_key_obj = getattr(service, "candidate_key", None)
        if candidate_key_obj is not None:
            candidate_key = {
                "registry": str(getattr(candidate_key_obj, "registry", ""))[:128],
                "repository": str(getattr(candidate_key_obj, "repository", ""))[:256],
                "tag": str(getattr(candidate_key_obj, "tag", ""))[:128],
                "os": str(getattr(candidate_key_obj, "target_os", ""))[:32],
                "arch": str(getattr(candidate_key_obj, "target_arch", ""))[:32],
            }
        else:
            candidate_key = None

        observed_at = getattr(service, "observed_at", None)
        if hasattr(observed_at, "isoformat"):
            observed_at_str = observed_at.isoformat()
        else:
            observed_at_str = str(observed_at) if observed_at else None

        detail = getattr(service, "candidate_detail", None)

        return {
            "service_name": str(getattr(service, "service_name", ""))[:128],
            "container_names": [str(name)[:128] for name in getattr(service, "container_names", ())][:64],
            "container_ids": [str(cid)[:64] for cid in getattr(service, "container_ids", ())][:64],
            "state": str(getattr(service, "state", "unknown"))[:32],
            "health": str(getattr(service, "health", None))[:32] if getattr(service, "health", None) is not None else None,
            "declared_image": str(getattr(service, "declared_image", None))[:256] if getattr(service, "declared_image", None) is not None else None,
            "running_image": str(getattr(service, "running_image", None))[:256] if getattr(service, "running_image", None) is not None else None,
            "running_image_id": str(getattr(service, "running_image_id", None))[:128] if getattr(service, "running_image_id", None) is not None else None,
            "running_repo_digests": [str(d)[:256] for d in getattr(service, "running_repo_digests", ())][:16],
            "candidate_digest": str(getattr(service, "candidate_digest", None))[:128] if getattr(service, "candidate_digest", None) is not None else None,
            "candidate_child_digest": str(getattr(service, "candidate_child_digest", None))[:128] if getattr(service, "candidate_child_digest", None) is not None else None,
            "candidate_status": status_val[:32],
            "candidate_reason": reason_val[:64],
            "candidate_detail": str(detail)[:256] if detail is not None else None,
            "is_build": bool(getattr(service, "is_build", False)),
            "depends_on": [str(dep)[:128] for dep in getattr(service, "depends_on", ())][:32],
            "candidate_key": candidate_key,
            "freshness": str(getattr(service, "freshness", "unknown"))[:32],
            "observed_at": observed_at_str,
            "provenance_verified": bool(getattr(service, "provenance_verified", False)),
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
