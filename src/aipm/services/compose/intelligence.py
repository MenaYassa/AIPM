"""Authoritative, read-only Compose service and image intelligence service.

Discovers running services and containers, correlates declared compose
specifications, and inspects image versions, digests, and update candidates.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

from aipm.models.compose_intelligence import (
    ComposeProjectObservation,
    ComposeServiceObservation,
    ServiceCandidateStatus,
)
from aipm.models.project import Project
from aipm.providers.compose.identity import resolve_compose_project_name
from aipm.providers.compose.provider import ComposeProvider
from aipm.services.compose.budget import DEFAULT_MAX_NETWORK_OPS, TwoLevelBudget
from aipm.services.compose.cache import CandidateCache
from aipm.services.compose.config_parser import parse_declared_compose_services
from aipm.services.compose.registry_client import RegistryCandidateClient
from aipm.services.compose.scheduler import CandidateQueryScheduler


class ComposeIntelligenceService:
    """Read-only service for Compose service-level and image-level intelligence."""

    def __init__(
        self,
        compose_provider: ComposeProvider | None = None,
        registry_client: RegistryCandidateClient | None = None,
        cache: CandidateCache | None = None,
        scheduler: CandidateQueryScheduler | None = None,
        target_arch: str = "arm64",
        target_os: str = "linux",
    ):
        self.compose_provider = compose_provider or ComposeProvider()
        self.registry_client = registry_client or RegistryCandidateClient(
            target_arch=target_arch,
            target_os=target_os,
        )
        if cache is not None:
            self.cache = cache
        elif getattr(self.registry_client, "cache", None) is not None:
            self.cache = self.registry_client.cache
        else:
            self.cache = CandidateCache()
        self.target_arch = target_arch
        self.target_os = target_os
        self.scheduler = scheduler or CandidateQueryScheduler(
            cache=self.cache,
            query_fn=self.registry_client.query_candidate_digest,
            target_arch=target_arch,
            target_os=target_os,
        )

    def _discover_all_project_compose_files(self, project: Project) -> list[str]:
        """Discover all compose files belonging to the project.

        Includes declared files plus standard override and nested sub-stack files.
        """
        files: list[str] = list(getattr(project, "compose_files", []) or [])
        project_root = Path(project.path).resolve()

        # Check for docker-compose.override.yml in project root
        for override_name in (
            "docker-compose.override.yml",
            "docker-compose.override.yaml",
            "compose.override.yml",
            "compose.override.yaml",
        ):
            override_path = project_root / override_name
            if override_path.is_file() and str(override_path) not in files:
                files.append(str(override_path))

        # Check for nested compose stacks (e.g. supabase/docker/docker-compose.yml)
        try:
            for child_path in sorted(project_root.glob("*/docker/docker-compose.yml")):
                if child_path.is_file() and str(child_path) not in files:
                    files.append(str(child_path))
            for child_path in sorted(project_root.glob("*/docker-compose.yml")):
                if child_path.is_file() and str(child_path) not in files:
                    files.append(str(child_path))
        except OSError:
            pass

        return files

    def observe(
        self,
        project: Project,
        *,
        query_registries: bool = True,
        active_profiles: set[str] | list[str] | None = None,
        budget: TwoLevelBudget | None = None,
    ) -> ComposeProjectObservation:
        """Perform a complete, read-only observation of a project's Compose stack."""
        now = datetime.now(timezone.utc)
        compose_identity = resolve_compose_project_name(project)

        if not compose_identity:
            return ComposeProjectObservation(
                project_name=project.name,
                compose_identity="unknown",
                project_path=project.path,
                compose_files=(),
                services=(),
                running_services_count=0,
                total_services_count=0,
                updates_available_count=0,
                current_count=0,
                drift_count=0,
                not_applicable_count=0,
                unknown_count=0,
                observed_at=now,
                freshness="never_sampled",
                error="Project does not declare Compose capabilities or valid Compose identity",
            )

        # Resolve active Compose profiles
        resolved_active_profiles: set[str] = set()
        if active_profiles is not None:
            resolved_active_profiles = {p.strip() for p in active_profiles if p and p.strip()}
        else:
            env_prof = os.environ.get("COMPOSE_PROFILES")
            if env_prof:
                resolved_active_profiles = {p.strip() for p in env_prof.split(",") if p.strip()}
            else:
                p_env = Path(project.path) / ".env"
                if p_env.is_file():
                    try:
                        for line in p_env.read_text(encoding="utf-8", errors="replace").splitlines():
                            line = line.strip()
                            if line.startswith("COMPOSE_PROFILES="):
                                val = line.split("=", 1)[1].strip().strip("\"'")
                                resolved_active_profiles = {p.strip() for p in val.split(",") if p.strip()}
                                break
                    except OSError:
                        pass

        # 1. Discover all compose files
        all_compose_files = self._discover_all_project_compose_files(project)

        # 2. Parse declared service configurations
        declared_services = parse_declared_compose_services(
            all_compose_files,
            project_root=Path(project.path).resolve(),
        )

        # 3. Discover running containers via ComposeProvider (using canonical identity + provenance)
        try:
            if hasattr(self.compose_provider, "ps_raw"):
                containers = self.compose_provider.ps_raw(project)
            else:
                containers = self.compose_provider.ps(project)
        except Exception as exc:
            return ComposeProjectObservation(
                project_name=project.name,
                compose_identity=compose_identity,
                project_path=project.path,
                compose_files=tuple(all_compose_files),
                services=(),
                running_services_count=0,
                total_services_count=len(declared_services),
                updates_available_count=0,
                current_count=0,
                drift_count=0,
                not_applicable_count=0,
                unknown_count=0,
                observed_at=now,
                freshness="unavailable",
                error=f"Unable to query Compose containers: {exc}",
            )

        # 4. Group containers by service label
        containers_by_service: dict[str, list[Any]] = {}
        for c in containers:
            labels = getattr(c, "labels", None) or {}
            svc_name = labels.get("com.docker.compose.service")
            if not svc_name:
                svc_name = getattr(c, "name", "unknown")
            containers_by_service.setdefault(svc_name, []).append(c)

        # 5. Union all service names (declared + running)
        all_service_names = sorted(set(declared_services.keys()) | set(containers_by_service.keys()))

        service_runtime_metadata: dict[str, dict[str, Any]] = {}
        service_container_metadata: dict[str, dict[str, Any]] = {}
        running_services_count = 0

        for svc_name in all_service_names:
            svc_containers = containers_by_service.get(svc_name, [])

            c_names = tuple(getattr(c, "name", "unknown") for c in svc_containers)
            c_ids = tuple(str(getattr(c, "short_id", getattr(c, "id", "unknown"))) for c in svc_containers)

            states = set()
            healths = []
            running_img = None
            running_img_id = None
            running_repo_digests: list[str] = []
            ports: list[str] = []

            for c in svc_containers:
                attrs = getattr(c, "attrs", {}) or {}
                state_obj = attrs.get("State", {}) or {}

                # State
                st = state_obj.get("Status", getattr(c, "status", getattr(c, "state", "unknown")))
                states.add(st)

                # Health
                hl = (state_obj.get("Health") or {}).get("Status") or getattr(c, "health", None)
                if hl:
                    healths.append(hl)

                # Image metadata
                img_obj = getattr(c, "image", None)
                if img_obj:
                    if not running_img_id:
                        running_img_id = getattr(img_obj, "id", None) or attrs.get("Image")
                    img_attrs = getattr(img_obj, "attrs", {}) or {}
                    for rd in img_attrs.get("RepoDigests") or []:
                        if rd and rd not in running_repo_digests:
                            running_repo_digests.append(rd)
                    if not running_img:
                        tags = getattr(img_obj, "tags", None) or []
                        if tags:
                            running_img = tags[0]

                if not running_img:
                    cfg_img = attrs.get("Config", {}).get("Image") or getattr(c, "image", None)
                    if cfg_img and cfg_img != "<none>":
                        running_img = cfg_img

                # Ports
                raw_ports = getattr(c, "ports", None) or {}
                if isinstance(raw_ports, dict):
                    for p in raw_ports.keys():
                        if str(p) not in ports:
                            ports.append(str(p))
                elif isinstance(raw_ports, (list, tuple)):
                    for p in raw_ports:
                        if str(p) not in ports:
                            ports.append(str(p))

            if not states:
                state = "not_created"
            elif states == {"running"}:
                state = "running"
            elif len(states) > 1:
                state = "mixed"
            else:
                state = next(iter(states))

            if state == "running":
                running_services_count += 1

            if "unhealthy" in healths:
                health = "unhealthy"
            elif "starting" in healths:
                health = "starting"
            elif set(healths) == {"healthy"}:
                health = "healthy"
            elif "healthy" in healths:
                health = "mixed"
            elif healths:
                health = healths[0]
            else:
                health = None

            service_container_metadata[svc_name] = {
                "c_names": c_names,
                "c_ids": c_ids,
                "state": state,
                "health": health,
                "ports": ports,
            }
            service_runtime_metadata[svc_name] = {
                "running_img": running_img,
                "running_img_id": running_img_id,
                "running_repo_digests": running_repo_digests,
            }

        # 6. Execute deterministic candidate scheduling and resolution
        max_lookups = (
            self.registry_client.max_queries
            if isinstance(getattr(self.registry_client, "max_queries", None), int)
            else 25
        )
        active_budget = budget or TwoLevelBudget(
            max_logical_lookups=max_lookups,
            max_network_ops=DEFAULT_MAX_NETWORK_OPS,
        )

        resolutions = self.scheduler.schedule_and_resolve(
            all_service_names=all_service_names,
            declared_services=declared_services,
            containers_by_service=containers_by_service,
            resolved_active_profiles=resolved_active_profiles,
            service_runtime_metadata=service_runtime_metadata,
            budget=active_budget,
            query_registries=query_registries,
        )

        # 7. Construct final observations
        service_observations: list[ComposeServiceObservation] = []
        for svc_name in all_service_names:
            declared = declared_services.get(svc_name)
            c_meta = service_container_metadata.get(svc_name, {})
            r_meta = service_runtime_metadata.get(svc_name, {})
            res = resolutions[svc_name]

            service_observations.append(
                ComposeServiceObservation(
                    service_name=svc_name,
                    container_names=c_meta.get("c_names", ()),
                    container_ids=c_meta.get("c_ids", ()),
                    state=c_meta.get("state", "not_created"),
                    health=c_meta.get("health"),
                    declared_image=declared.image if declared else None,
                    declared_image_ref=declared.image_ref if declared else None,
                    running_image=r_meta.get("running_img"),
                    running_image_id=r_meta.get("running_img_id"),
                    running_repo_digests=tuple(r_meta.get("running_repo_digests", ())),
                    candidate_digest=res.candidate_digest,
                    candidate_child_digest=res.candidate_child_digest,
                    candidate_status=res.candidate_status,
                    candidate_reason=res.candidate_reason,
                    candidate_detail=res.candidate_detail,
                    is_build=declared.is_build if declared else False,
                    build_context=declared.build_context if declared else None,
                    build_dockerfile=declared.build_dockerfile if declared else None,
                    ports=tuple(c_meta.get("ports", ())),
                    freshness="fresh",
                    observed_at=now,
                    provenance_verified=bool(containers_by_service.get(svc_name, [])),
                    depends_on=declared.depends_on if declared else (),
                    candidate_key=res.candidate_key,
                )
            )

        updates_available_count = sum(
            1 for s in service_observations if s.candidate_status == ServiceCandidateStatus.UPDATE_AVAILABLE
        )
        current_count = sum(
            1 for s in service_observations if s.candidate_status == ServiceCandidateStatus.CURRENT
        )
        drift_count = sum(
            1 for s in service_observations if s.candidate_status == ServiceCandidateStatus.DRIFT
        )
        not_applicable_count = sum(
            1 for s in service_observations if s.candidate_status == ServiceCandidateStatus.NOT_APPLICABLE
        )
        unknown_count = sum(
            1 for s in service_observations if s.candidate_status == ServiceCandidateStatus.UNKNOWN
        )

        return ComposeProjectObservation(
            project_name=project.name,
            compose_identity=compose_identity,
            project_path=project.path,
            compose_files=tuple(all_compose_files),
            services=tuple(service_observations),
            running_services_count=running_services_count,
            total_services_count=len(all_service_names),
            updates_available_count=updates_available_count,
            current_count=current_count,
            drift_count=drift_count,
            not_applicable_count=not_applicable_count,
            unknown_count=unknown_count,
            observed_at=now,
            freshness="fresh",
            error=None,
        )
