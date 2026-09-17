"""Deterministic Candidate Query Scheduler and intra-observation coalescing.

Implements fair, bounded, explainable prioritization of OCI candidate lookups:
- Coalesces duplicate canonical image references into a single remote query.
- Prioritizes running workloads and cache hits to maximize observation yield.
- Enforces strict two-level budgets (logical lookups and physical network operations).
- Provides deterministic lexical ordering without reliance on hash or dictionary order.
- Projects candidate results safely into services without sharing runtime or container state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from aipm.models.compose_intelligence import (
    CandidateCacheFreshness,
    CandidateLookupKey,
    DeclaredServiceConfig,
    ImageReference,
    ServiceCandidateReason,
    ServiceCandidateStatus,
)
from aipm.services.compose.budget import TwoLevelBudget
from aipm.services.compose.cache import CandidateCache
from aipm.services.compose.image_ref import parse_image_reference
from aipm.services.compose.registry_client import RegistryCandidateResult


@dataclass(frozen=True, slots=True)
class ServiceCandidateResolution:
    """Final resolved candidate determination for an individual Compose service."""

    service_name: str
    candidate_key: CandidateLookupKey | None
    candidate_digest: str | None
    candidate_child_digest: str | None
    candidate_status: ServiceCandidateStatus
    candidate_reason: ServiceCandidateReason
    candidate_detail: str | None
    scheduling_state: str


class CandidateQueryScheduler:
    """Deterministic scheduler for resolving OCI candidate images across a project."""

    def __init__(
        self,
        *,
        cache: CandidateCache | None = None,
        query_fn: Callable[[ImageReference, TwoLevelBudget], RegistryCandidateResult] | None = None,
        target_arch: str = "arm64",
        target_os: str = "linux",
    ) -> None:
        self.cache = cache if cache is not None else CandidateCache()
        self.query_fn = query_fn
        self.target_arch = target_arch
        self.target_os = target_os

    def schedule_and_resolve(
        self,
        *,
        all_service_names: list[str],
        declared_services: dict[str, DeclaredServiceConfig],
        containers_by_service: dict[str, list[Any]],
        resolved_active_profiles: set[str],
        service_runtime_metadata: dict[str, dict[str, Any]],
        budget: TwoLevelBudget,
        query_registries: bool = True,
    ) -> dict[str, ServiceCandidateResolution]:
        """Execute deterministic scheduling, deduplication, and resolution across all services."""
        resolutions: dict[str, ServiceCandidateResolution] = {}

        # Services grouped by candidate lookup key: key -> list of (service_name, declared_img_ref)
        key_to_services: dict[CandidateLookupKey, list[tuple[str, ImageReference]]] = {}
        # Reverse map: service_name -> key
        service_to_key: dict[str, CandidateLookupKey] = {}

        # 1. First pass: classify fast-path services that require NO registry query
        for svc_name in all_service_names:
            declared = declared_services.get(svc_name)
            svc_containers = containers_by_service.get(svc_name, [])
            runtime_meta = service_runtime_metadata.get(svc_name, {})
            running_img = runtime_meta.get("running_img")
            running_repo_digests = runtime_meta.get("running_repo_digests", [])

            declared_img = declared.image if declared else None
            declared_img_ref = declared.image_ref if declared else None
            is_build = declared.is_build if declared else False
            build_context = declared.build_context if declared else None
            profiles = declared.profiles if declared else ()
            is_profile_inactive = bool(profiles) and not any(p in resolved_active_profiles for p in profiles)

            # Check fast-path non-query cases
            if not svc_containers:
                if is_profile_inactive:
                    resolutions[svc_name] = ServiceCandidateResolution(
                        service_name=svc_name,
                        candidate_key=None,
                        candidate_digest=declared_img_ref.digest if declared_img_ref and declared_img_ref.is_pinned_by_digest else None,
                        candidate_child_digest=None,
                        candidate_status=ServiceCandidateStatus.NOT_APPLICABLE,
                        candidate_reason=ServiceCandidateReason.DISABLED_BY_PROFILE,
                        candidate_detail=(
                            f"Service is inactive under current profiles (service profiles: {list(profiles)}, "
                            f"active profiles: {sorted(resolved_active_profiles) or 'none'})"
                        ),
                        scheduling_state="skipped_profile",
                    )
                else:
                    resolutions[svc_name] = ServiceCandidateResolution(
                        service_name=svc_name,
                        candidate_key=None,
                        candidate_digest=declared_img_ref.digest if declared_img_ref and declared_img_ref.is_pinned_by_digest else None,
                        candidate_child_digest=None,
                        candidate_status=ServiceCandidateStatus.NOT_APPLICABLE,
                        candidate_reason=ServiceCandidateReason.NO_RUNNING_CONTAINER,
                        candidate_detail="No running container for service",
                        scheduling_state="skipped_no_container",
                    )
            elif not declared:
                resolutions[svc_name] = ServiceCandidateResolution(
                    service_name=svc_name,
                    candidate_key=None,
                    candidate_digest=None,
                    candidate_child_digest=None,
                    candidate_status=ServiceCandidateStatus.UNKNOWN,
                    candidate_reason=ServiceCandidateReason.MALFORMED_IMAGE_REFERENCE,
                    candidate_detail=f"Running container belongs to undeclared Compose service '{svc_name}' (unmatched service)",
                    scheduling_state="skipped_malformed",
                )
            elif is_build and (not declared_img_ref or declared_img_ref.is_local_build):
                resolutions[svc_name] = ServiceCandidateResolution(
                    service_name=svc_name,
                    candidate_key=None,
                    candidate_digest=None,
                    candidate_child_digest=None,
                    candidate_status=ServiceCandidateStatus.NOT_APPLICABLE,
                    candidate_reason=ServiceCandidateReason.LOCAL_BUILD,
                    candidate_detail=f"Registry comparison not applicable: service is built from local context ({build_context or '.'})",
                    scheduling_state="skipped_local_build",
                )
            elif declared and declared.parse_error and not is_build:
                resolutions[svc_name] = ServiceCandidateResolution(
                    service_name=svc_name,
                    candidate_key=None,
                    candidate_digest=None,
                    candidate_child_digest=None,
                    candidate_status=ServiceCandidateStatus.UNKNOWN,
                    candidate_reason=ServiceCandidateReason.MALFORMED_IMAGE_REFERENCE,
                    candidate_detail=f"Declared service '{svc_name}' configuration cannot be safely resolved: {declared.parse_error}; failing closed",
                    scheduling_state="skipped_malformed",
                )
            elif declared_img and not declared_img_ref and not is_build:
                resolutions[svc_name] = ServiceCandidateResolution(
                    service_name=svc_name,
                    candidate_key=None,
                    candidate_digest=None,
                    candidate_child_digest=None,
                    candidate_status=ServiceCandidateStatus.UNKNOWN,
                    candidate_reason=ServiceCandidateReason.MALFORMED_IMAGE_REFERENCE,
                    candidate_detail=f"Declared image expression '{declared_img}' cannot be safely resolved (unresolved interpolation or invalid syntax); failing closed",
                    scheduling_state="skipped_malformed",
                )
            elif not declared_img and not is_build:
                resolutions[svc_name] = ServiceCandidateResolution(
                    service_name=svc_name,
                    candidate_key=None,
                    candidate_digest=None,
                    candidate_child_digest=None,
                    candidate_status=ServiceCandidateStatus.UNKNOWN,
                    candidate_reason=ServiceCandidateReason.MALFORMED_IMAGE_REFERENCE,
                    candidate_detail="Service specifies neither image nor build configuration; failing closed",
                    scheduling_state="skipped_malformed",
                )
            elif declared_img_ref and declared_img_ref.is_pinned_by_digest:
                # Pinned immutable digest: strict positive evidence invariant
                pinned_digest = declared_img_ref.digest
                known_running_digests: set[str] = set()
                for rd in running_repo_digests:
                    if "@" in rd:
                        known_running_digests.add(rd.split("@", 1)[1])
                    elif ":" in rd:
                        known_running_digests.add(rd)
                if running_img and "@" in running_img:
                    parts = running_img.split("@", 1)
                    if len(parts) == 2:
                        known_running_digests.add(parts[1])

                if not known_running_digests:
                    resolutions[svc_name] = ServiceCandidateResolution(
                        service_name=svc_name,
                        candidate_key=None,
                        candidate_digest=pinned_digest,
                        candidate_child_digest=None,
                        candidate_status=ServiceCandidateStatus.UNKNOWN,
                        candidate_reason=ServiceCandidateReason.DIGEST_UNAVAILABLE,
                        candidate_detail=f"Running container has no verifiable image digest evidence to compare against declared pinned digest {pinned_digest}",
                        scheduling_state="skipped_pinned",
                    )
                elif pinned_digest in known_running_digests:
                    resolutions[svc_name] = ServiceCandidateResolution(
                        service_name=svc_name,
                        candidate_key=None,
                        candidate_digest=pinned_digest,
                        candidate_child_digest=None,
                        candidate_status=ServiceCandidateStatus.CURRENT,
                        candidate_reason=ServiceCandidateReason.PINNED_BY_DIGEST,
                        candidate_detail=f"Running container matches declared immutable digest {pinned_digest}",
                        scheduling_state="skipped_pinned",
                    )
                else:
                    resolutions[svc_name] = ServiceCandidateResolution(
                        service_name=svc_name,
                        candidate_key=None,
                        candidate_digest=pinned_digest,
                        candidate_child_digest=None,
                        candidate_status=ServiceCandidateStatus.DRIFT,
                        candidate_reason=ServiceCandidateReason.CONFIGURATION_DRIFT,
                        candidate_detail=f"Running image digest(s) {sorted(known_running_digests)} drift from declared immutable digest '{pinned_digest}'",
                        scheduling_state="skipped_pinned",
                    )
            elif declared_img_ref and running_img and running_img != "<none>" and not is_build:
                # Check for runtime drift against declared image
                is_drift = False
                try:
                    running_ref = parse_image_reference(running_img)
                    if running_ref.repository != declared_img_ref.repository:
                        is_drift = True
                    elif running_ref.tag and declared_img_ref.tag and running_ref.tag != declared_img_ref.tag:
                        is_drift = True
                except ValueError:
                    pass

                if is_drift:
                    resolutions[svc_name] = ServiceCandidateResolution(
                        service_name=svc_name,
                        candidate_key=CandidateLookupKey.from_image_ref(
                            declared_img_ref, target_arch=self.target_arch, target_os=self.target_os
                        ),
                        candidate_digest=None,
                        candidate_child_digest=None,
                        candidate_status=ServiceCandidateStatus.DRIFT,
                        candidate_reason=ServiceCandidateReason.CONFIGURATION_DRIFT,
                        candidate_detail=f"Running image '{running_img}' drifts from declared Compose configuration '{declared_img}'",
                        scheduling_state="skipped_drift",
                    )
                elif not query_registries:
                    resolutions[svc_name] = ServiceCandidateResolution(
                        service_name=svc_name,
                        candidate_key=CandidateLookupKey.from_image_ref(
                            declared_img_ref, target_arch=self.target_arch, target_os=self.target_os
                        ),
                        candidate_digest=None,
                        candidate_child_digest=None,
                        candidate_status=ServiceCandidateStatus.UNKNOWN,
                        candidate_reason=ServiceCandidateReason.REGISTRY_UNAVAILABLE,
                        candidate_detail="Registry queries disabled",
                        scheduling_state="skipped_disabled",
                    )
                else:
                    key = CandidateLookupKey.from_image_ref(
                        declared_img_ref, target_arch=self.target_arch, target_os=self.target_os
                    )
                    key_to_services.setdefault(key, []).append((svc_name, declared_img_ref))
                    service_to_key[svc_name] = key
            elif declared_img_ref and not query_registries:
                resolutions[svc_name] = ServiceCandidateResolution(
                    service_name=svc_name,
                    candidate_key=CandidateLookupKey.from_image_ref(
                        declared_img_ref, target_arch=self.target_arch, target_os=self.target_os
                    ),
                    candidate_digest=None,
                    candidate_child_digest=None,
                    candidate_status=ServiceCandidateStatus.UNKNOWN,
                    candidate_reason=ServiceCandidateReason.REGISTRY_UNAVAILABLE,
                    candidate_detail="Registry queries disabled",
                    scheduling_state="skipped_disabled",
                )
            else:
                # Eligible for candidate lookup: group and deduplicate
                key = CandidateLookupKey.from_image_ref(
                    declared_img_ref, target_arch=self.target_arch, target_os=self.target_os
                )
                key_to_services.setdefault(key, []).append((svc_name, declared_img_ref))
                service_to_key[svc_name] = key

        # If no registry queries requested, we are done
        if not query_registries or not key_to_services:
            return resolutions

        # 2. Prioritize unique candidate keys
        def _key_priority(k: CandidateLookupKey) -> tuple[int, str]:
            # Tier 0: Already fresh in cache (cost 0 network ops)
            cached_val = self.cache.peek(k)
            if cached_val is not None:
                _res, freshness = cached_val
                if freshness == CandidateCacheFreshness.FRESH:
                    return (0, k.canonical_str())

            # Check if any service using this key is currently running with positive RepoDigests
            services = key_to_services[k]
            has_running_with_digests = any(
                bool(service_runtime_metadata.get(s_name, {}).get("running_repo_digests"))
                for s_name, _ in services
            )
            if has_running_with_digests:
                return (1, k.canonical_str())

            # Tier 2: Running without RepoDigests
            has_running = any(
                bool(containers_by_service.get(s_name, []))
                for s_name, _ in services
            )
            if has_running:
                return (2, k.canonical_str())

            # Tier 3: Stale in cache (needs refresh)
            if cached_val is not None:
                return (3, k.canonical_str())

            # Tier 4: Other keys
            return (4, k.canonical_str())

        sorted_keys = sorted(key_to_services.keys(), key=_key_priority)

        # 3. Execute queries for prioritized unique keys
        key_results: dict[CandidateLookupKey, tuple[RegistryCandidateResult, str]] = {}

        for key in sorted_keys:
            # Check cache first
            cached = self.cache.get(key)
            if cached is not None:
                res, freshness = cached
                if freshness == CandidateCacheFreshness.FRESH:
                    key_results[key] = (res, "cache_hit_fresh")
                    continue

            # Budget check before performing logical query
            allowed, reason, err_detail = budget.check_logical_lookup()
            if not allowed:
                # If budget exhausted, record budget exhaustion result
                res = RegistryCandidateResult(
                    status=ServiceCandidateStatus.UNKNOWN,
                    reason=reason or ServiceCandidateReason.BUDGET_EXHAUSTED,
                    index_digest=None,
                    child_digest=None,
                    detail=err_detail or "Query budget exhausted",
                )
                sched_state = "timeout" if reason == ServiceCandidateReason.REGISTRY_TIMEOUT else "budget_exhausted"
                key_results[key] = (res, sched_state)
                continue

            # Execute remote query via query_fn
            first_svc_name, ref = key_to_services[key][0]
            budget.record_logical_lookup()

            if self.query_fn:
                try:
                    res = self.query_fn(ref, budget)
                    if res.reason not in (
                        ServiceCandidateReason.BUDGET_EXHAUSTED,
                        ServiceCandidateReason.NETWORK_BUDGET_EXHAUSTED,
                    ):
                        is_neg = res.status == ServiceCandidateStatus.UNKNOWN and res.reason in (
                            ServiceCandidateReason.REGISTRY_UNAVAILABLE,
                            ServiceCandidateReason.REGISTRY_TIMEOUT,
                        )
                        self.cache.put(key, res, is_negative=is_neg)
                    key_results[key] = (res, "queried")
                except Exception as exc:
                    res = RegistryCandidateResult(
                        status=ServiceCandidateStatus.UNKNOWN,
                        reason=ServiceCandidateReason.REGISTRY_UNAVAILABLE,
                        index_digest=None,
                        child_digest=None,
                        detail=f"Registry query error: {exc}",
                    )
                    self.cache.put(key, res, is_negative=True)
                    key_results[key] = (res, "queried_error")
            else:
                res = RegistryCandidateResult(
                    status=ServiceCandidateStatus.UNKNOWN,
                    reason=ServiceCandidateReason.REGISTRY_UNAVAILABLE,
                    index_digest=None,
                    child_digest=None,
                    detail="No registry query provider configured",
                )
                key_results[key] = (res, "unconfigured")

        # 4. Project candidate results into each service independently
        for key in sorted_keys:
            candidate_res, base_sched_state = key_results[key]
            service_list = key_to_services[key]

            for idx, (svc_name, _ref) in enumerate(service_list):
                runtime_meta = service_runtime_metadata.get(svc_name, {})
                running_repo_digests = runtime_meta.get("running_repo_digests", [])

                cand_digest = candidate_res.index_digest or candidate_res.child_digest
                cand_child_digest = candidate_res.child_digest
                sched_state = base_sched_state if idx == 0 else f"deduplicated ({base_sched_state})"

                if candidate_res.index_digest or candidate_res.child_digest:
                    if not running_repo_digests:
                        resolutions[svc_name] = ServiceCandidateResolution(
                            service_name=svc_name,
                            candidate_key=key,
                            candidate_digest=cand_digest,
                            candidate_child_digest=cand_child_digest,
                            candidate_status=ServiceCandidateStatus.UNKNOWN,
                            candidate_reason=ServiceCandidateReason.DIGEST_UNAVAILABLE,
                            candidate_detail="Running container has no RepoDigests to compare against registry candidate",
                            scheduling_state=sched_state,
                        )
                    else:
                        matched = False
                        for rd in running_repo_digests:
                            if candidate_res.index_digest and (rd.endswith(f"@{candidate_res.index_digest}") or rd == candidate_res.index_digest):
                                matched = True
                                break
                            if candidate_res.child_digest and (rd.endswith(f"@{candidate_res.child_digest}") or rd == candidate_res.child_digest):
                                matched = True
                                break

                        if matched:
                            resolutions[svc_name] = ServiceCandidateResolution(
                                service_name=svc_name,
                                candidate_key=key,
                                candidate_digest=cand_digest,
                                candidate_child_digest=cand_child_digest,
                                candidate_status=ServiceCandidateStatus.CURRENT,
                                candidate_reason=ServiceCandidateReason.UP_TO_DATE,
                                candidate_detail=f"Running digest matches registry candidate ({candidate_res.detail or ''})".strip(),
                                scheduling_state=sched_state,
                            )
                        else:
                            short_dig = cand_digest[:19] if cand_digest else ""
                            resolutions[svc_name] = ServiceCandidateResolution(
                                service_name=svc_name,
                                candidate_key=key,
                                candidate_digest=cand_digest,
                                candidate_child_digest=cand_child_digest,
                                candidate_status=ServiceCandidateStatus.UPDATE_AVAILABLE,
                                candidate_reason=ServiceCandidateReason.CANDIDATE_DIGEST_DIFFERS,
                                candidate_detail=f"Registry candidate digest differs from running image: {short_dig}... ({candidate_res.detail or ''})".strip(),
                                scheduling_state=sched_state,
                            )
                else:
                    resolutions[svc_name] = ServiceCandidateResolution(
                        service_name=svc_name,
                        candidate_key=key,
                        candidate_digest=None,
                        candidate_child_digest=None,
                        candidate_status=candidate_res.status,
                        candidate_reason=candidate_res.reason,
                        candidate_detail=candidate_res.detail,
                        scheduling_state=sched_state,
                    )

        return resolutions
