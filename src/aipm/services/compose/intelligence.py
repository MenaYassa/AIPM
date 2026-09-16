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
    ServiceCandidateReason,
    ServiceCandidateStatus,
)
from aipm.models.project import Project
from aipm.providers.compose.identity import resolve_compose_project_name
from aipm.providers.compose.provider import ComposeProvider
from aipm.services.compose.config_parser import parse_declared_compose_services
from aipm.services.compose.image_ref import parse_image_reference
from aipm.services.compose.registry_client import RegistryCandidateClient


class ComposeIntelligenceService:
    """Read-only service for Compose service-level and image-level intelligence."""

    def __init__(
        self,
        compose_provider: ComposeProvider | None = None,
        registry_client: RegistryCandidateClient | None = None,
    ):
        self.compose_provider = compose_provider or ComposeProvider()
        self.registry_client = registry_client or RegistryCandidateClient()

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

        service_observations: list[ComposeServiceObservation] = []
        running_services_count = 0

        for svc_name in all_service_names:
            declared = declared_services.get(svc_name)
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

            declared_img = declared.image if declared else None
            declared_img_ref = declared.image_ref if declared else None
            is_build = declared.is_build if declared else False
            build_context = declared.build_context if declared else None
            build_dockerfile = declared.build_dockerfile if declared else None
            profiles = declared.profiles if declared else ()
            is_profile_inactive = bool(profiles) and not any(p in resolved_active_profiles for p in profiles)

            # Candidate determination
            candidate_digest: str | None = None
            candidate_child_digest: str | None = None
            candidate_status: ServiceCandidateStatus = ServiceCandidateStatus.UNKNOWN
            candidate_reason: ServiceCandidateReason = ServiceCandidateReason.NO_RUNNING_CONTAINER
            candidate_detail: str | None = None

            if not svc_containers:
                candidate_status = ServiceCandidateStatus.NOT_APPLICABLE
                if is_profile_inactive:
                    candidate_reason = ServiceCandidateReason.DISABLED_BY_PROFILE
                    candidate_detail = (
                        f"Service is inactive under current profiles (service profiles: {list(profiles)}, "
                        f"active profiles: {sorted(resolved_active_profiles) or 'none'})"
                    )
                else:
                    candidate_reason = ServiceCandidateReason.NO_RUNNING_CONTAINER
                    candidate_detail = "No running container for service"
                if declared_img_ref and declared_img_ref.is_pinned_by_digest:
                    candidate_digest = declared_img_ref.digest
            elif not declared:
                candidate_status = ServiceCandidateStatus.UNKNOWN
                candidate_reason = ServiceCandidateReason.MALFORMED_IMAGE_REFERENCE
                candidate_detail = f"Running container belongs to undeclared Compose service '{svc_name}' (unmatched service)"
            elif is_build and (not declared_img_ref or declared_img_ref.is_local_build):
                candidate_status = ServiceCandidateStatus.NOT_APPLICABLE
                candidate_reason = ServiceCandidateReason.LOCAL_BUILD
                candidate_detail = (
                    f"Registry comparison not applicable: service is built from local context ({build_context or '.'})"
                )
            elif declared and declared.parse_error and not is_build:
                candidate_status = ServiceCandidateStatus.UNKNOWN
                candidate_reason = ServiceCandidateReason.MALFORMED_IMAGE_REFERENCE
                candidate_detail = (
                    f"Declared service '{svc_name}' configuration cannot be safely resolved: "
                    f"{declared.parse_error}; failing closed"
                )
            elif declared_img and not declared_img_ref and not is_build:
                candidate_status = ServiceCandidateStatus.UNKNOWN
                candidate_reason = ServiceCandidateReason.MALFORMED_IMAGE_REFERENCE
                candidate_detail = (
                    f"Declared image expression '{declared_img}' cannot be safely resolved "
                    "(unresolved interpolation or invalid syntax); failing closed"
                )
            elif not declared_img and not is_build:
                candidate_status = ServiceCandidateStatus.UNKNOWN
                candidate_reason = ServiceCandidateReason.MALFORMED_IMAGE_REFERENCE
                candidate_detail = "Service specifies neither image nor build configuration; failing closed"
            elif declared_img_ref and declared_img_ref.is_pinned_by_digest:
                # Declared image is pinned by immutable digest: require positive runtime evidence
                candidate_digest = declared_img_ref.digest
                pinned_digest = declared_img_ref.digest

                # Extract positive running digest evidence
                known_running_digests: set[str] = set()
                for rd in running_repo_digests:
                    if "@" in rd:
                        known_running_digests.add(rd.split("@", 1)[1])
                    elif ":" in rd:
                        known_running_digests.add(rd)
                if running_img and "@" in running_img:
                    try:
                        r_ref = parse_image_reference(running_img)
                        if r_ref.digest:
                            known_running_digests.add(r_ref.digest)
                    except ValueError:
                        pass

                if not known_running_digests:
                    candidate_status = ServiceCandidateStatus.UNKNOWN
                    candidate_reason = ServiceCandidateReason.DIGEST_UNAVAILABLE
                    candidate_detail = (
                        f"Running container has no verifiable image digest evidence to compare "
                        f"against declared pinned digest {pinned_digest}"
                    )
                elif pinned_digest in known_running_digests:
                    candidate_status = ServiceCandidateStatus.CURRENT
                    candidate_reason = ServiceCandidateReason.PINNED_BY_DIGEST
                    candidate_detail = (
                        f"Running container matches declared immutable digest {pinned_digest}"
                    )
                else:
                    candidate_status = ServiceCandidateStatus.DRIFT
                    candidate_reason = ServiceCandidateReason.CONFIGURATION_DRIFT
                    candidate_detail = (
                        f"Running image digest(s) {sorted(known_running_digests)} drift from "
                        f"declared immutable digest '{pinned_digest}'"
                    )
            elif declared_img_ref and running_img and running_img != "<none>" and not is_build:
                # Check for runtime drift against declared image
                try:
                    running_ref = parse_image_reference(running_img)
                    is_drift = False
                    if running_ref.repository != declared_img_ref.repository:
                        is_drift = True
                    elif running_ref.tag and declared_img_ref.tag and running_ref.tag != declared_img_ref.tag:
                        is_drift = True

                    if is_drift:
                        candidate_status = ServiceCandidateStatus.DRIFT
                        candidate_reason = ServiceCandidateReason.CONFIGURATION_DRIFT
                        candidate_detail = (
                            f"Running image '{running_img}' drifts from declared Compose configuration '{declared_img}'"
                        )
                except ValueError:
                    pass

            if (
                candidate_status == ServiceCandidateStatus.UNKNOWN
                and svc_containers
                and declared_img_ref
                and not declared_img_ref.is_pinned_by_digest
                and not (declared and declared.parse_error)
            ):
                ref_to_query = declared_img_ref

                if query_registries:
                    res = self.registry_client.query_candidate_digest(ref_to_query)
                    candidate_digest = res.index_digest or res.child_digest
                    candidate_child_digest = res.child_digest
                    candidate_detail = res.detail

                    if res.index_digest or res.child_digest:
                        if not running_repo_digests:
                            candidate_status = ServiceCandidateStatus.UNKNOWN
                            candidate_reason = ServiceCandidateReason.DIGEST_UNAVAILABLE
                            candidate_detail = (
                                "Running container has no RepoDigests to compare against registry candidate"
                            )
                        else:
                            matched = False
                            for rd in running_repo_digests:
                                if res.index_digest and (rd.endswith(f"@{res.index_digest}") or rd == res.index_digest):
                                    matched = True
                                    break
                                if res.child_digest and (rd.endswith(f"@{res.child_digest}") or rd == res.child_digest):
                                    matched = True
                                    break

                            if matched:
                                candidate_status = ServiceCandidateStatus.CURRENT
                                candidate_reason = ServiceCandidateReason.UP_TO_DATE
                                candidate_detail = (
                                    f"Running digest matches registry candidate ({res.detail or ''})".strip()
                                )
                            else:
                                candidate_status = ServiceCandidateStatus.UPDATE_AVAILABLE
                                candidate_reason = ServiceCandidateReason.CANDIDATE_DIGEST_DIFFERS
                                candidate_detail = (
                                    f"Registry candidate digest differs from running image: {candidate_digest[:19]}... ({res.detail or ''})".strip()
                                )
                    else:
                        candidate_status = res.status
                        candidate_reason = res.reason
                elif not query_registries:
                    candidate_status = ServiceCandidateStatus.UNKNOWN
                    candidate_reason = ServiceCandidateReason.REGISTRY_UNAVAILABLE
                    candidate_detail = "Registry queries disabled"

            service_observations.append(
                ComposeServiceObservation(
                    service_name=svc_name,
                    container_names=c_names,
                    container_ids=c_ids,
                    state=state,
                    health=health,
                    declared_image=declared_img,
                    declared_image_ref=declared_img_ref,
                    running_image=running_img,
                    running_image_id=running_img_id,
                    running_repo_digests=tuple(running_repo_digests),
                    candidate_digest=candidate_digest,
                    candidate_child_digest=candidate_child_digest,
                    candidate_status=candidate_status,
                    candidate_reason=candidate_reason,
                    candidate_detail=candidate_detail,
                    is_build=is_build,
                    build_context=build_context,
                    build_dockerfile=build_dockerfile,
                    ports=tuple(ports),
                    freshness="fresh",
                    observed_at=now,
                    provenance_verified=bool(svc_containers),
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
