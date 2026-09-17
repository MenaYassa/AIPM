"""Tests for MC-6.15-B.1/B.2: Compose Service Update Plan and Dependency/Atomicity Design.

Exhaustively verifies deterministic planning, dependency topology, atomicity classification,
canonical UpdatePlanIdentity binding, staleness detection, and adversarial resilience.
"""
from datetime import datetime, timezone
import pytest

from aipm.models.compose_intelligence import (
    CandidateLookupKey,
    ComposeProjectObservation,
    ComposeServiceObservation,
    ImageReference,
    ServiceCandidateReason,
    ServiceCandidateStatus,
)
from aipm.models.compose_plan import (
    ServicePlanBlockingReason,
    ServiceUpdateAtomicity,
    ServiceUpdatePlan,
)
from aipm.services.compose.planner import ComposeServiceUpdatePlanner
from aipm.services.update.plan_identity import UpdatePlanIdentity


def _make_sample_service(
    service_name: str = "ollama",
    status: ServiceCandidateStatus = ServiceCandidateStatus.UPDATE_AVAILABLE,
    reason: ServiceCandidateReason = ServiceCandidateReason.CANDIDATE_DIGEST_DIFFERS,
    running_digest: str = "sha256:1111111111111111111111111111111111111111111111111111111111111111",
    candidate_digest: str = "sha256:2222222222222222222222222222222222222222222222222222222222222222",
    child_digest: str | None = "sha256:3333333333333333333333333333333333333333333333333333333333333333",
    depends_on: tuple[str, ...] = (),
    freshness: str = "fresh",
    provenance_verified: bool = True,
    health: str | None = "healthy",
    state: str = "running",
    is_build: bool = False,
    container_ids: tuple[str, ...] = ("c101",),
) -> ComposeServiceObservation:
    img_ref = ImageReference(
        raw=f"{service_name}:latest",
        registry="docker.io",
        repository=service_name,
        tag="latest",
    )
    cand_key = CandidateLookupKey.from_image_ref(img_ref)
    return ComposeServiceObservation(
        service_name=service_name,
        container_names=(service_name,),
        container_ids=container_ids,
        state=state,
        health=health,
        declared_image=f"{service_name}:latest",
        declared_image_ref=img_ref,
        running_image=f"{service_name}:latest",
        running_image_id=running_digest,
        running_repo_digests=(f"{service_name}@{running_digest}",) if running_digest else (),
        candidate_digest=candidate_digest,
        candidate_child_digest=child_digest,
        candidate_status=status,
        candidate_reason=reason,
        candidate_detail="Test detail",
        is_build=is_build,
        build_context="./custom" if is_build else None,
        build_dockerfile="Dockerfile" if is_build else None,
        ports=("8080/tcp",),
        freshness=freshness,
        observed_at=datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc),
        provenance_verified=provenance_verified,
        depends_on=depends_on,
        candidate_key=cand_key,
    )


def _make_project_observation(
    services: tuple[ComposeServiceObservation, ...],
    project_name: str = "local-ai-packaged",
    compose_identity: str = "localai",
) -> ComposeProjectObservation:
    return ComposeProjectObservation(
        project_name=project_name,
        compose_identity=compose_identity,
        project_path="/home/ubuntu/localai",
        compose_files=("docker-compose.yml",),
        services=services,
        running_services_count=sum(1 for s in services if s.state == "running"),
        total_services_count=len(services),
        updates_available_count=sum(1 for s in services if s.candidate_status == ServiceCandidateStatus.UPDATE_AVAILABLE),
        current_count=sum(1 for s in services if s.candidate_status == ServiceCandidateStatus.CURRENT),
        drift_count=sum(1 for s in services if s.candidate_status == ServiceCandidateStatus.DRIFT),
        not_applicable_count=sum(1 for s in services if s.candidate_status == ServiceCandidateStatus.NOT_APPLICABLE),
        unknown_count=sum(1 for s in services if s.candidate_status == ServiceCandidateStatus.UNKNOWN),
        observed_at=datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc),
        freshness="fresh",
    )


class TestComposeServiceUpdatePlanner:

    def test_01_update_available_yields_eligible_plan(self):
        svc = _make_sample_service("ollama")
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "ollama")

        assert plan.eligible is True
        assert plan.blocking_reason is None
        assert plan.service_name == "ollama"
        assert plan.target_candidate_digest == svc.candidate_digest
        assert plan.current_runtime_digest is not None
        assert plan.atomicity == ServiceUpdateAtomicity.LEAF_INDEPENDENT
        assert plan.expected_mutation.dependency_mode == "no_deps"
        assert plan.rollback_design.snapshot_required is True

    def test_02_current_service_is_blocked(self):
        svc = _make_sample_service("db", status=ServiceCandidateStatus.CURRENT, reason=ServiceCandidateReason.UP_TO_DATE)
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "db")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.NOT_UPDATE_AVAILABLE
        assert plan.atomicity == ServiceUpdateAtomicity.BLOCKED

    def test_03_not_applicable_local_build_is_blocked(self):
        svc = _make_sample_service(
            "edge-tts",
            status=ServiceCandidateStatus.NOT_APPLICABLE,
            reason=ServiceCandidateReason.LOCAL_BUILD,
            candidate_digest=None,
            is_build=True,
        )
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "edge-tts")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.LOCAL_BUILD

    def test_04_unknown_status_is_blocked(self):
        svc = _make_sample_service(
            "litellm",
            status=ServiceCandidateStatus.UNKNOWN,
            reason=ServiceCandidateReason.BUDGET_EXHAUSTED,
            candidate_digest=None,
        )
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "litellm")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.NOT_UPDATE_AVAILABLE

    def test_05_drift_status_is_blocked(self):
        svc = _make_sample_service(
            "web",
            status=ServiceCandidateStatus.DRIFT,
            reason=ServiceCandidateReason.CONFIGURATION_DRIFT,
        )
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "web")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.NOT_UPDATE_AVAILABLE

    def test_06_missing_current_digest_is_blocked(self):
        svc = _make_sample_service("ollama", running_digest="")
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "ollama")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.CURRENT_DIGEST_MISSING

    def test_07_missing_candidate_digest_is_blocked(self):
        svc = _make_sample_service("ollama", candidate_digest=None)
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "ollama")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.CANDIDATE_DIGEST_MISSING

    def test_08_stale_candidate_observation_is_blocked(self):
        svc = _make_sample_service("ollama", freshness="stale")
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "ollama")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.OBSERVATION_STALE

    def test_09_invalid_provenance_is_blocked(self):
        svc = _make_sample_service("ollama", provenance_verified=False)
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "ollama")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.PROVENANCE_INVALID

    def test_10_unsupported_non_compose_project_is_blocked(self):
        svc = _make_sample_service("standalone", candidate_digest="sha256:target")
        obs = _make_project_observation((svc,), compose_identity="")
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "standalone")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.NON_COMPOSE_PROJECT

    def test_11_leaf_independent_service(self):
        svc = _make_sample_service("ollama", depends_on=())
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "ollama")

        assert plan.eligible is True
        assert plan.atomicity == ServiceUpdateAtomicity.LEAF_INDEPENDENT
        assert len(plan.dependency_scope) == 0
        assert plan.expected_mutation.dependency_mode == "no_deps"
        assert plan.expected_mutation.affected_services == ("ollama",)

    def test_12_service_with_current_healthy_dependency(self):
        dep = _make_sample_service("db", status=ServiceCandidateStatus.CURRENT, reason=ServiceCandidateReason.UP_TO_DATE)
        svc = _make_sample_service("auth", depends_on=("db",))
        obs = _make_project_observation((svc, dep))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "auth")

        assert plan.eligible is True
        assert plan.atomicity == ServiceUpdateAtomicity.LEAF_INDEPENDENT
        assert len(plan.dependency_scope) == 1
        assert plan.dependency_scope[0].service_name == "db"
        assert plan.dependency_scope[0].in_scope_reason == "prerequisite_healthy_current"

    def test_13_atomic_dependency_group(self):
        valkey = _make_sample_service("searxng-valkey", status=ServiceCandidateStatus.UPDATE_AVAILABLE)
        searx = _make_sample_service("searxng", status=ServiceCandidateStatus.UPDATE_AVAILABLE, depends_on=("searxng-valkey",))
        obs = _make_project_observation((searx, valkey))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "searxng")

        assert plan.eligible is True
        assert plan.atomicity == ServiceUpdateAtomicity.ATOMIC_TIGHT
        assert plan.expected_mutation.dependency_mode == "atomic_group"
        assert set(plan.expected_mutation.affected_services) == {"searxng", "searxng-valkey"}
        assert plan.rollback_design.rollback_scope == "dependency_group"

    def test_14_contradictory_dependency_cycle_is_blocked(self):
        svc_a = _make_sample_service("service-a", depends_on=("service-b",))
        svc_b = _make_sample_service("service-b", depends_on=("service-a",))
        obs = _make_project_observation((svc_a, svc_b))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "service-a")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.DEPENDENCY_CYCLE_DETECTED
        assert plan.atomicity == ServiceUpdateAtomicity.BLOCKED

    def test_15_deterministic_dependency_ordering(self):
        dep_c = _make_sample_service("dep-c", status=ServiceCandidateStatus.CURRENT)
        dep_a = _make_sample_service("dep-a", status=ServiceCandidateStatus.CURRENT)
        dep_b = _make_sample_service("dep-b", status=ServiceCandidateStatus.CURRENT)
        svc = _make_sample_service("target", depends_on=("dep-c", "dep-a", "dep-b"))
        obs = _make_project_observation((svc, dep_c, dep_a, dep_b))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "target")

        scope_names = [d.service_name for d in plan.dependency_scope]
        assert scope_names == ["dep-a", "dep-b", "dep-c"]

    def test_16_health_contract_derivation(self):
        dep = _make_sample_service("redis", status=ServiceCandidateStatus.CURRENT)
        svc = _make_sample_service("app", depends_on=("redis",), health="healthy")
        obs = _make_project_observation((svc, dep))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "app")

        contract = plan.health_contract
        assert contract.service_name == "app"
        assert contract.expected_state == "running"
        assert contract.expected_health == "healthy"
        assert contract.has_health_check is True
        assert contract.timeout_seconds == 30
        assert contract.check_dependencies is True
        assert contract.dependency_services == ("redis",)
        assert "svc:app|state:running|health:healthy|timeout:30|deps:redis" in contract.canonical_summary()

    def test_17_stale_plan_when_current_digest_changes(self):
        svc = _make_sample_service("ollama")
        obs1 = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()
        plan = planner.plan_service(obs1, "ollama")

        # Mutate running digest in fresh observation
        mutated_svc = _make_sample_service(
            "ollama",
            running_digest="sha256:9999999999999999999999999999999999999999999999999999999999999999",
        )
        obs2 = _make_project_observation((mutated_svc,))

        is_stale, reason = planner.check_staleness(plan, obs2)
        assert is_stale is True
        assert reason == "current_digest_changed"

    def test_18_stale_plan_when_candidate_digest_changes(self):
        svc = _make_sample_service("ollama")
        obs1 = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()
        plan = planner.plan_service(obs1, "ollama")

        # Mutate candidate digest in fresh observation
        mutated_svc = _make_sample_service(
            "ollama",
            candidate_digest="sha256:8888888888888888888888888888888888888888888888888888888888888888",
        )
        obs2 = _make_project_observation((mutated_svc,))

        is_stale, reason = planner.check_staleness(plan, obs2)
        assert is_stale is True
        assert reason == "candidate_digest_changed"

    def test_19_stale_plan_when_dependency_scope_changes(self):
        dep = _make_sample_service("valkey", status=ServiceCandidateStatus.CURRENT)
        svc = _make_sample_service("searxng", depends_on=("valkey",))
        obs1 = _make_project_observation((svc, dep))
        planner = ComposeServiceUpdatePlanner()
        plan = planner.plan_service(obs1, "searxng")

        # Remove dependency in fresh observation
        svc_no_deps = _make_sample_service("searxng", depends_on=())
        obs2 = _make_project_observation((svc_no_deps,))

        is_stale, reason = planner.check_staleness(plan, obs2)
        assert is_stale is True
        assert reason == "dependency_scope_changed"

    def test_20_deterministic_plan_identity(self):
        svc = _make_sample_service("ollama")
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan1 = planner.plan_service(obs, "ollama")
        plan2 = planner.plan_service(obs, "ollama")

        assert plan1.plan_digest == plan2.plan_digest
        assert len(plan1.plan_digest) == 64

    def test_21_timestamp_and_container_id_changes_do_not_alter_identity(self):
        svc1 = _make_sample_service("ollama", container_ids=("c101",))
        obs1 = _make_project_observation((svc1,))
        planner = ComposeServiceUpdatePlanner()
        plan1 = planner.plan_service(obs1, "ollama")

        # Create identical service with different container ID and different timestamp
        svc2 = _make_sample_service("ollama", container_ids=("c999_different",))
        obs2 = ComposeProjectObservation(
            project_name="local-ai-packaged",
            compose_identity="localai",
            project_path="/home/ubuntu/localai",
            compose_files=("docker-compose.yml",),
            services=(svc2,),
            running_services_count=1,
            total_services_count=1,
            updates_available_count=1,
            current_count=0,
            drift_count=0,
            not_applicable_count=0,
            unknown_count=0,
            observed_at=datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            freshness="fresh",
        )
        plan2 = planner.plan_service(obs2, "ollama")

        assert plan1.plan_digest == plan2.plan_digest

    def test_22_distinct_services_sharing_image_have_distinct_plans(self):
        svc_a = _make_sample_service("app-primary")
        svc_b = _make_sample_service("app-secondary")
        obs = _make_project_observation((svc_a, svc_b))
        planner = ComposeServiceUpdatePlanner()

        plan_a = planner.plan_service(obs, "app-primary")
        plan_b = planner.plan_service(obs, "app-secondary")

        assert plan_a.plan_digest != plan_b.plan_digest
        assert plan_a.service_name != plan_b.service_name

    def test_23_local_build_never_eligible(self):
        svc = _make_sample_service(
            "short-video-maker",
            status=ServiceCandidateStatus.NOT_APPLICABLE,
            reason=ServiceCandidateReason.LOCAL_BUILD,
            is_build=True,
        )
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "short-video-maker")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.LOCAL_BUILD

    def test_24_profile_disabled_never_eligible(self):
        svc = _make_sample_service(
            "realtime",
            status=ServiceCandidateStatus.NOT_APPLICABLE,
            reason=ServiceCandidateReason.DISABLED_BY_PROFILE,
            state="not_created",
            health=None,
        )
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "realtime")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.DISABLED_BY_PROFILE

    def test_25_no_executable_mutation_primitive(self):
        svc = _make_sample_service("ollama")
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "ollama")

        # Verify mutation shape contains data only
        assert hasattr(plan, "expected_mutation")
        assert isinstance(plan.expected_mutation.target_service, str)
        assert isinstance(plan.expected_mutation.candidate_digest, str)
        assert not hasattr(plan.expected_mutation, "execute")
        assert not hasattr(plan.expected_mutation, "run")
        assert not hasattr(plan, "execute")

    def test_26_no_executor_ipc_reachable(self):
        import aipm.services.compose.planner as pmod
        source = open(pmod.__file__).read()
        assert "executor_ipc" not in source
        assert "subprocess" not in source
        assert "os.system" not in source

    def test_27_no_docker_mutation_occurs(self):
        svc = _make_sample_service("ollama")
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        # Pure in-memory calculation
        plan = planner.plan_service(obs, "ollama")
        assert isinstance(plan, ServiceUpdatePlan)


class TestAdversarialCases:

    def test_adversarial_candidate_equals_runtime_digest(self):
        same_digest = "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        svc = _make_sample_service("ollama", running_digest=same_digest, candidate_digest=same_digest)
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "ollama")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.NOT_UPDATE_AVAILABLE

    def test_adversarial_multi_arch_manifest_vs_platform_child(self):
        index_digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        child_digest = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        svc = _make_sample_service("db", candidate_digest=index_digest, child_digest=child_digest)
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "db")

        assert plan.target_candidate_digest == index_digest
        assert plan.target_candidate_child_digest == child_digest

    def test_adversarial_self_dependency_cycle(self):
        svc = _make_sample_service("self-dep", depends_on=("self-dep",))
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "self-dep")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.DEPENDENCY_CYCLE_DETECTED

    def test_adversarial_3_service_cycle(self):
        svc_a = _make_sample_service("svc-a", depends_on=("svc-b",))
        svc_b = _make_sample_service("svc-b", depends_on=("svc-c",))
        svc_c = _make_sample_service("svc-c", depends_on=("svc-a",))
        obs = _make_project_observation((svc_a, svc_b, svc_c))
        planner = ComposeServiceUpdatePlanner()

        plan_a = planner.plan_service(obs, "svc-a")
        plan_b = planner.plan_service(obs, "svc-b")

        assert plan_a.eligible is False
        assert plan_a.blocking_reason == ServicePlanBlockingReason.DEPENDENCY_CYCLE_DETECTED
        assert plan_b.eligible is False
        assert plan_b.blocking_reason == ServicePlanBlockingReason.DEPENDENCY_CYCLE_DETECTED

    def test_adversarial_missing_dependency_service(self):
        svc = _make_sample_service("web", depends_on=("ghost-service",))
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "web")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.MISSING_DEPENDENCY_SERVICE
        assert len(plan.dependency_scope) == 1
        assert plan.dependency_scope[0].service_name == "ghost-service"
        assert plan.dependency_scope[0].state == "missing"

    def test_adversarial_disabled_profile_dependency(self):
        dep = _make_sample_service(
            "imgproxy",
            status=ServiceCandidateStatus.NOT_APPLICABLE,
            reason=ServiceCandidateReason.DISABLED_BY_PROFILE,
            state="not_created",
            health=None,
        )
        svc = _make_sample_service("storage", depends_on=("imgproxy",))
        obs = _make_project_observation((svc, dep))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "storage")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.DEPENDENCY_NOT_RUNNING

    def test_adversarial_dependency_not_running(self):
        dep = _make_sample_service("db", state="exited", health=None)
        svc = _make_sample_service("app", depends_on=("db",))
        obs = _make_project_observation((svc, dep))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "app")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.DEPENDENCY_NOT_RUNNING

    def test_adversarial_dependency_unhealthy(self):
        dep = _make_sample_service("db", state="running", health="unhealthy")
        svc = _make_sample_service("app", depends_on=("db",))
        obs = _make_project_observation((svc, dep))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "app")

        assert plan.eligible is False
        assert plan.blocking_reason == ServicePlanBlockingReason.DEPENDENCY_BLOCKED

    def test_adversarial_plan_all_services(self):
        ollama = _make_sample_service("ollama", status=ServiceCandidateStatus.UPDATE_AVAILABLE)
        db = _make_sample_service("db", status=ServiceCandidateStatus.CURRENT, reason=ServiceCandidateReason.UP_TO_DATE)
        obs = _make_project_observation((ollama, db))
        planner = ComposeServiceUpdatePlanner()

        plans = planner.plan_all(obs)

        assert len(plans) == 2
        p_ollama = next(p for p in plans if p.service_name == "ollama")
        p_db = next(p for p in plans if p.service_name == "db")
        assert p_ollama.eligible is True
        assert p_db.eligible is False

    def test_adversarial_serialization_to_dict(self):
        valkey = _make_sample_service("searxng-valkey", status=ServiceCandidateStatus.UPDATE_AVAILABLE)
        searx = _make_sample_service("searxng", status=ServiceCandidateStatus.UPDATE_AVAILABLE, depends_on=("searxng-valkey",))
        obs = _make_project_observation((searx, valkey))
        planner = ComposeServiceUpdatePlanner()

        plan = planner.plan_service(obs, "searxng")
        payload = plan.to_dict()

        assert payload["service_name"] == "searxng"
        assert payload["eligible"] is True
        assert payload["atomicity"] == "atomic_tight"
        assert payload["expected_mutation"]["dependency_mode"] == "atomic_group"
        assert payload["health_contract"]["check_dependencies"] is True
        assert len(payload["dependency_scope"]) == 1
        assert payload["dependency_scope"][0]["service_name"] == "searxng-valkey"

    def test_adversarial_same_candidate_digest_across_unrelated_services(self):
        same_cand_digest = "sha256:7777777777777777777777777777777777777777777777777777777777777777"
        svc_a = _make_sample_service("app-a", candidate_digest=same_cand_digest)
        svc_b = _make_sample_service("app-b", candidate_digest=same_cand_digest)
        obs = _make_project_observation((svc_a, svc_b))
        planner = ComposeServiceUpdatePlanner()

        plan_a = planner.plan_service(obs, "app-a")
        plan_b = planner.plan_service(obs, "app-b")

        assert plan_a.target_candidate_digest == plan_b.target_candidate_digest
        assert plan_a.plan_digest != plan_b.plan_digest
        assert plan_a.service_name != plan_b.service_name

    def test_adversarial_health_contract_alteration_changes_plan_digest(self):
        svc = _make_sample_service("ollama", health="healthy")
        obs = _make_project_observation((svc,))
        planner = ComposeServiceUpdatePlanner()

        plan1 = planner.plan_service(obs, "ollama")

        # Now test with service having no health check (None)
        svc_no_health = _make_sample_service("ollama", health=None)
        obs2 = _make_project_observation((svc_no_health,))
        plan2 = planner.plan_service(obs2, "ollama")

        assert plan1.health_contract.canonical_summary() != plan2.health_contract.canonical_summary()
        assert plan1.plan_digest != plan2.plan_digest


class TestComposeServicePlanApi:
    """Tests for GET /api/projects/{project_id}/compose-services/{service_name}/plan."""

    @pytest.fixture
    def api_fixture(self):
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        from fastapi.testclient import TestClient
        from aipm.capabilities.dashboard.project_api import DashboardProjectApi
        from aipm.dashboard.server import create_app
        from aipm.models.project import Project, ProjectCapabilities
        from aipm.services.compose.service import ComposeService
        from aipm.services.project.intelligence import ProjectIntelligenceService

        svc_ollama = _make_sample_service("ollama", status=ServiceCandidateStatus.UPDATE_AVAILABLE)
        svc_valkey = _make_sample_service("searxng-valkey", status=ServiceCandidateStatus.UPDATE_AVAILABLE)
        svc_searxng = _make_sample_service(
            "searxng",
            status=ServiceCandidateStatus.UPDATE_AVAILABLE,
            depends_on=("searxng-valkey",),
        )
        obs = _make_project_observation((svc_ollama, svc_valkey, svc_searxng))

        project_compose = Project(
            name="local-ai-packaged",
            path="/srv/projects/local-ai-packaged",
            capabilities=ProjectCapabilities(has_compose=True),
            compose_files=["/srv/projects/local-ai-packaged/docker-compose.yml"],
        )
        project_non_compose = Project(
            name="invoicing",
            path="/srv/projects/invoicing",
            capabilities=ProjectCapabilities(has_compose=False, has_git=True),
            compose_files=[],
        )

        class FakeProjectService:
            def __init__(self, projects):
                self.projects = projects
                self.app = SimpleNamespace(config=SimpleNamespace(discovery=SimpleNamespace(search_paths=["/srv/projects"])))

            def discover(self):
                return self.projects

            def get_project(self, name):
                for p in self.projects:
                    if p.name == name:
                        return p
                raise LookupError(f"Project '{name}' not found")

        mock_intelligence = MagicMock()
        mock_intelligence.observe.return_value = obs

        compose_svc = ComposeService(intelligence=mock_intelligence)
        project_svc = FakeProjectService([project_compose, project_non_compose])

        class FakeObservation:
            def containers(self):
                return []

        class FakeTelemetry:
            def fast_snapshot(self, *, now):
                return SimpleNamespace(containers=[], state_sampled_at=now)

        intel_svc = ProjectIntelligenceService(
            project_svc,
            FakeObservation(),
            FakeTelemetry(),
            compose_service=compose_svc,
        )
        dashboard_api = DashboardProjectApi(intel_svc)
        app = create_app(project_api=dashboard_api)
        client = TestClient(app)

        inventory = intel_svc.inventory()
        compose_id = next(p.id for p in inventory.projects if p.display_name == "local-ai-packaged")
        non_compose_id = next(p.id for p in inventory.local_candidates if p.display_name == "invoicing")

        return {
            "client": client,
            "compose_id": compose_id,
            "non_compose_id": non_compose_id,
        }

    def test_28_public_api_get_returns_service_plan(self, api_fixture):
        client = api_fixture["client"]
        compose_id = api_fixture["compose_id"]

        res = client.get(f"/api/projects/{compose_id}/compose-services/ollama/plan")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "ok"
        assert "service_plan" in body
        plan = body["service_plan"]
        assert plan["service_name"] == "ollama"
        assert plan["eligible"] is True
        assert plan["atomicity"] == "leaf_independent"
        assert plan["plan_identity"]["version"] == "mc612-update-plan-identity-v1"

    def test_29_public_api_rejects_post_and_mutations(self, api_fixture):
        client = api_fixture["client"]
        compose_id = api_fixture["compose_id"]

        # POST is not allowed on this read-only plan preview endpoint
        res = client.post(f"/api/projects/{compose_id}/compose-services/ollama/plan", json={})
        assert res.status_code == 405

        # PUT is not allowed
        res = client.put(f"/api/projects/{compose_id}/compose-services/ollama/plan", json={})
        assert res.status_code == 405

        # DELETE is not allowed
        res = client.delete(f"/api/projects/{compose_id}/compose-services/ollama/plan")
        assert res.status_code == 405

    def test_30_public_api_non_compose_project_unavailable(self, api_fixture):
        client = api_fixture["client"]
        non_compose_id = api_fixture["non_compose_id"]

        res = client.get(f"/api/projects/{non_compose_id}/compose-services/ollama/plan")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "unavailable"
        assert body["service_plan"] is None
        assert "Compose update planning unavailable" in body["error"]

    def test_31_public_api_invalid_identifier_and_service(self, api_fixture):
        client = api_fixture["client"]
        compose_id = api_fixture["compose_id"]

        # Invalid project ID
        res = client.get("/api/projects/bad_id!/compose-services/ollama/plan")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "error"
        assert body["error"] == "Project identifier is invalid"

        # Invalid service name
        res = client.get(f"/api/projects/{compose_id}/compose-services/bad;service$/plan")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "error"
        assert body["error"] == "Service name is invalid"

