"""Compose service-specific update planning service (MC-6.15-B.1/B.2).

Builds deterministic, read-only update plans for individual Compose services
on top of validated Compose candidate intelligence.
Strictly PLAN-ONLY: contains NO execution authority, Docker mutations, or IPC triggers.
"""
from __future__ import annotations

from typing import Any

from aipm.models.compose_intelligence import (
    ComposeProjectObservation,
    ComposeServiceObservation,
    ServiceCandidateReason,
    ServiceCandidateStatus,
)
from aipm.models.compose_plan import (
    DependencyScopeItem,
    ServiceHealthContract,
    ServiceMutationShape,
    ServicePlanBlockingReason,
    ServiceRollbackDesign,
    ServiceUpdateAtomicity,
    ServiceUpdatePlan,
)
from aipm.services.update.plan_identity import UpdatePlanIdentity


class ComposeServiceUpdatePlanner:
    """Deterministic read-only planner for single Compose service updates."""

    def plan_service(
        self,
        observation: ComposeProjectObservation,
        service_name: str,
        *,
        project_id: str | None = None,
    ) -> ServiceUpdatePlan:
        """Derive an immutable ServiceUpdatePlan for the requested service."""
        services_by_name: dict[str, ComposeServiceObservation] = {
            s.service_name: s for s in observation.services
        }
        svc = services_by_name.get(service_name)

        # 1. Target existence
        if svc is None:
            return self._build_missing_service_plan(
                observation=observation,
                service_name=service_name,
                project_id=project_id,
            )

        blocking_reason: ServicePlanBlockingReason | None = None

        # 2. Non-Compose verification
        if not observation.compose_identity or observation.compose_identity.lower() == "none":
            blocking_reason = ServicePlanBlockingReason.NON_COMPOSE_PROJECT

        # 3. Status and reason evaluation
        if blocking_reason is None:
            if svc.candidate_status == ServiceCandidateStatus.CURRENT:
                blocking_reason = ServicePlanBlockingReason.NOT_UPDATE_AVAILABLE
            elif svc.candidate_status == ServiceCandidateStatus.NOT_APPLICABLE:
                if svc.candidate_reason == ServiceCandidateReason.LOCAL_BUILD:
                    blocking_reason = ServicePlanBlockingReason.LOCAL_BUILD
                elif svc.candidate_reason == ServiceCandidateReason.DISABLED_BY_PROFILE:
                    blocking_reason = ServicePlanBlockingReason.DISABLED_BY_PROFILE
                else:
                    blocking_reason = ServicePlanBlockingReason.NOT_UPDATE_AVAILABLE
            elif svc.candidate_status in (ServiceCandidateStatus.UNKNOWN, ServiceCandidateStatus.DRIFT):
                blocking_reason = ServicePlanBlockingReason.NOT_UPDATE_AVAILABLE
            elif svc.candidate_status != ServiceCandidateStatus.UPDATE_AVAILABLE:
                blocking_reason = ServicePlanBlockingReason.NOT_UPDATE_AVAILABLE

        # 4. Digest resolution and equality check
        current_digest: str | None = None
        if svc.running_repo_digests:
            current_digest = svc.running_repo_digests[0]
        elif svc.running_image_id:
            current_digest = svc.running_image_id

        target_digest = svc.candidate_digest

        if blocking_reason is None:
            if not current_digest:
                blocking_reason = ServicePlanBlockingReason.CURRENT_DIGEST_MISSING
            elif not target_digest:
                blocking_reason = ServicePlanBlockingReason.CANDIDATE_DIGEST_MISSING
            else:
                # Strip repo prefixes if present for pure digest comparison
                cd_hash = current_digest.split("@")[1] if "@" in current_digest else current_digest
                td_hash = target_digest.split("@")[1] if "@" in target_digest else target_digest
                if cd_hash == td_hash:
                    blocking_reason = ServicePlanBlockingReason.NOT_UPDATE_AVAILABLE

        # 5. Observation freshness
        if blocking_reason is None and svc.freshness != "fresh":
            blocking_reason = ServicePlanBlockingReason.OBSERVATION_STALE

        # 6. Provenance verification
        if blocking_reason is None and not svc.provenance_verified:
            blocking_reason = ServicePlanBlockingReason.PROVENANCE_INVALID

        # 7. Dependency analysis and topology classification
        dep_items: list[DependencyScopeItem] = []
        has_dep_cycle = self._detect_cycle(service_name, services_by_name)
        if has_dep_cycle and blocking_reason is None:
            blocking_reason = ServicePlanBlockingReason.DEPENDENCY_CYCLE_DETECTED

        sorted_deps = tuple(sorted(svc.depends_on))
        dep_update_available = False

        for dep_name in sorted_deps:
            dep_svc = services_by_name.get(dep_name)
            if dep_svc is None:
                if blocking_reason is None:
                    blocking_reason = ServicePlanBlockingReason.MISSING_DEPENDENCY_SERVICE
                dep_items.append(
                    DependencyScopeItem(
                        service_name=dep_name,
                        running_image=None,
                        running_digest=None,
                        candidate_digest=None,
                        candidate_status="unknown",
                        state="missing",
                        health=None,
                        in_scope_reason="missing_dependency_service",
                    )
                )
            else:
                # Verify dependency runtime state
                if dep_svc.state != "running" and blocking_reason is None:
                    blocking_reason = ServicePlanBlockingReason.DEPENDENCY_NOT_RUNNING
                elif dep_svc.candidate_status in (ServiceCandidateStatus.UNKNOWN, ServiceCandidateStatus.DRIFT) and blocking_reason is None:
                    blocking_reason = ServicePlanBlockingReason.DEPENDENCY_BLOCKED
                elif dep_svc.health == "unhealthy" and blocking_reason is None:
                    blocking_reason = ServicePlanBlockingReason.DEPENDENCY_BLOCKED

                dep_run_dig: str | None = None
                if dep_svc.running_repo_digests:
                    dep_run_dig = dep_svc.running_repo_digests[0]
                elif dep_svc.running_image_id:
                    dep_run_dig = dep_svc.running_image_id

                if dep_svc.candidate_status == ServiceCandidateStatus.UPDATE_AVAILABLE:
                    in_scope_reason = "co_update_candidate_available"
                    dep_update_available = True
                elif dep_svc.candidate_status == ServiceCandidateStatus.CURRENT:
                    in_scope_reason = "prerequisite_healthy_current"
                else:
                    in_scope_reason = f"prerequisite_{dep_svc.candidate_status.value}"

                dep_items.append(
                    DependencyScopeItem(
                        service_name=dep_name,
                        running_image=dep_svc.running_image,
                        running_digest=dep_run_dig,
                        candidate_digest=dep_svc.candidate_digest,
                        candidate_status=dep_svc.candidate_status.value,
                        state=dep_svc.state,
                        health=dep_svc.health,
                        in_scope_reason=in_scope_reason,
                    )
                )

        # 8. Atomicity determination
        if blocking_reason is not None:
            atomicity = ServiceUpdateAtomicity.BLOCKED
        elif not sorted_deps:
            atomicity = ServiceUpdateAtomicity.LEAF_INDEPENDENT
        elif dep_update_available:
            atomicity = ServiceUpdateAtomicity.ATOMIC_TIGHT
        else:
            atomicity = ServiceUpdateAtomicity.LEAF_INDEPENDENT

        # 9. Health verification contract
        health_contract = ServiceHealthContract(
            service_name=service_name,
            expected_state="running",
            expected_health="healthy" if svc.health is not None else "running",
            has_health_check=bool(svc.health is not None),
            timeout_seconds=30,
            success_condition="container_running_and_healthy" if svc.health else "container_running",
            check_dependencies=(atomicity == ServiceUpdateAtomicity.ATOMIC_TIGHT) or bool(sorted_deps),
            dependency_services=sorted_deps,
        )

        eligible = blocking_reason is None

        # 10. Expected mutation shape (DESIGN DATA ONLY)
        affected_services = (
            (service_name,)
            if atomicity == ServiceUpdateAtomicity.LEAF_INDEPENDENT
            else tuple(sorted((service_name, *sorted_deps)))
        )
        mutation_shape = ServiceMutationShape(
            target_service=service_name,
            candidate_digest=target_digest or "",
            dependency_mode="atomic_group" if atomicity == ServiceUpdateAtomicity.ATOMIC_TIGHT else "no_deps",
            affected_services=affected_services,
        )

        # 11. Rollback design (DESIGN DATA ONLY)
        rollback_design = ServiceRollbackDesign(
            snapshot_required=True,
            targeted_service_backup=True,
            rollback_supported=True,
            rollback_scope="dependency_group" if atomicity == ServiceUpdateAtomicity.ATOMIC_TIGHT else "target_only",
        )

        # 12. Reasons and actions
        if eligible:
            actions = (
                f"Update service {service_name} to {target_digest}",
                f"Verify health contract for {service_name}",
            )
            reasons = (
                f"Candidate digest differs from running image for {service_name}",
            )
        else:
            actions = ()
            reasons = (
                f"Service update blocked: {blocking_reason.value}",
            )

        # 13. Canonical UpdatePlanIdentity binding
        candidate_key_str = svc.candidate_key.canonical_str() if svc.candidate_key else None
        identity = UpdatePlanIdentity(
            project=observation.project_name,
            dry_run=True,
            proceed=eligible,
            approval_required=True,
            risk="low" if eligible and atomicity == ServiceUpdateAtomicity.LEAF_INDEPENDENT else ("medium" if eligible else "blocked"),
            reasons=reasons,
            actions=actions,
            snapshot_required=rollback_design.snapshot_required,
            estimated_restart=True,
            stash_required=False,
            pull_required=False,
            service_name=service_name,
            target_digest=target_digest,
            current_digest=current_digest,
            candidate_lookup_key=candidate_key_str,
            dependency_scope=sorted_deps if sorted_deps else None,
            atomicity=atomicity.value,
            observation_freshness=svc.freshness,
            health_probe_contract=health_contract.canonical_summary(),
        )

        return ServiceUpdatePlan(
            project_name=observation.project_name,
            project_id=project_id,
            compose_identity=observation.compose_identity,
            service_name=service_name,
            current_runtime_image=svc.running_image,
            current_runtime_digest=current_digest,
            declared_image=svc.declared_image,
            declared_image_ref=svc.declared_image_ref,
            target_candidate_digest=target_digest,
            target_candidate_child_digest=svc.candidate_child_digest,
            candidate_key=svc.candidate_key,
            candidate_freshness=svc.freshness,
            candidate_status=svc.candidate_status,
            candidate_reason=svc.candidate_reason,
            candidate_detail=svc.candidate_detail,
            provenance_verified=svc.provenance_verified,
            dependency_scope=tuple(dep_items),
            atomicity=atomicity,
            health_contract=health_contract,
            eligible=eligible,
            blocking_reason=blocking_reason,
            expected_mutation=mutation_shape,
            rollback_design=rollback_design,
            observed_at=observation.observed_at,
            plan_digest=identity.digest(),
            actions=actions,
            reasons=reasons,
            plan_identity=identity,
        )

    def plan_all(
        self,
        observation: ComposeProjectObservation,
        *,
        project_id: str | None = None,
    ) -> tuple[ServiceUpdatePlan, ...]:
        """Derive update plans for all declared services in the observation."""
        return tuple(
            self.plan_service(observation, svc.service_name, project_id=project_id)
            for svc in observation.services
        )

    def check_staleness(
        self,
        plan: ServiceUpdatePlan,
        current_observation: ComposeProjectObservation,
    ) -> tuple[bool, str | None]:
        """Detect whether a previously generated service update plan is stale against a fresh observation."""
        if current_observation.project_name != plan.project_name:
            return True, "project_mismatch"
        if current_observation.compose_identity != plan.compose_identity:
            return True, "compose_identity_mismatch"

        fresh_plan = self.plan_service(
            current_observation,
            plan.service_name,
            project_id=plan.project_id,
        )

        if fresh_plan.current_runtime_digest != plan.current_runtime_digest:
            return True, "current_digest_changed"
        if fresh_plan.target_candidate_digest != plan.target_candidate_digest:
            return True, "candidate_digest_changed"
        if [d.service_name for d in fresh_plan.dependency_scope] != [d.service_name for d in plan.dependency_scope]:
            return True, "dependency_scope_changed"
        if fresh_plan.candidate_freshness != "fresh":
            return True, "observation_became_stale"
        if fresh_plan.plan_digest != plan.plan_digest:
            return True, "plan_digest_mismatch"

        return False, None

    def _detect_cycle(
        self,
        start_service: str,
        services_by_name: dict[str, ComposeServiceObservation],
    ) -> bool:
        """Check whether start_service is involved in any dependency cycle."""
        visited: set[str] = set()
        stack: list[str] = []

        def dfs(node: str) -> bool:
            if node in stack:
                return True
            if node in visited:
                return False
            visited.add(node)
            stack.append(node)
            node_svc = services_by_name.get(node)
            if node_svc:
                for dep in node_svc.depends_on:
                    if dfs(dep):
                        return True
            stack.pop()
            return False

        return dfs(start_service)

    def _build_missing_service_plan(
        self,
        observation: ComposeProjectObservation,
        service_name: str,
        project_id: str | None,
    ) -> ServiceUpdatePlan:
        """Construct a safe blocked plan when the requested service does not exist."""
        blocking_reason = ServicePlanBlockingReason.SERVICE_NOT_FOUND
        health_contract = ServiceHealthContract(
            service_name=service_name,
            expected_state="running",
            expected_health=None,
            has_health_check=False,
            timeout_seconds=30,
            success_condition="container_running",
            check_dependencies=False,
            dependency_services=(),
        )
        mutation_shape = ServiceMutationShape(
            target_service=service_name,
            candidate_digest="",
            dependency_mode="no_deps",
            affected_services=(service_name,),
        )
        rollback_design = ServiceRollbackDesign(
            snapshot_required=True,
            targeted_service_backup=False,
            rollback_supported=False,
            rollback_scope="none",
        )
        reasons = (f"Service update blocked: {blocking_reason.value}",)
        identity = UpdatePlanIdentity(
            project=observation.project_name,
            dry_run=True,
            proceed=False,
            approval_required=True,
            risk="blocked",
            reasons=reasons,
            actions=(),
            snapshot_required=True,
            estimated_restart=False,
            stash_required=False,
            pull_required=False,
            service_name=service_name,
            target_digest=None,
            current_digest=None,
            candidate_lookup_key=None,
            dependency_scope=None,
            atomicity=ServiceUpdateAtomicity.BLOCKED.value,
            observation_freshness="unknown",
            health_probe_contract=health_contract.canonical_summary(),
        )
        return ServiceUpdatePlan(
            project_name=observation.project_name,
            project_id=project_id,
            compose_identity=observation.compose_identity,
            service_name=service_name,
            current_runtime_image=None,
            current_runtime_digest=None,
            declared_image=None,
            declared_image_ref=None,
            target_candidate_digest=None,
            target_candidate_child_digest=None,
            candidate_key=None,
            candidate_freshness="unknown",
            candidate_status=ServiceCandidateStatus.UNKNOWN,
            candidate_reason=ServiceCandidateReason.NO_RUNNING_CONTAINER,
            candidate_detail="Service does not exist in declared Compose configuration",
            provenance_verified=False,
            dependency_scope=(),
            atomicity=ServiceUpdateAtomicity.BLOCKED,
            health_contract=health_contract,
            eligible=False,
            blocking_reason=blocking_reason,
            expected_mutation=mutation_shape,
            rollback_design=rollback_design,
            observed_at=observation.observed_at,
            plan_digest=identity.digest(),
            actions=(),
            reasons=reasons,
        )
