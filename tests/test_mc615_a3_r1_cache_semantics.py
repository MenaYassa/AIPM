"""MC-6.15-A.3-R1 Cache Semantics Tests.

Verifies that:
1. NETWORK_BUDGET_EXHAUSTED results are NOT cached.
2. BUDGET_EXHAUSTED results are NOT cached.
3. Subsequent observation with fresh budget retries candidates instead of receiving previous exhaustion.
4. Positive candidate results remain cached for positive TTL (900s).
5. Intentional negative results retain negative TTL (60s).
6. Cache hit/miss counters remain accurate.
7. Scheduler accounting remains exactly once per logical key.
8. DEFAULT_MAX_NETWORK_OPS remains invariant at 50.
9. Public API response schema remains strictly preserved without leakage.
10. Regression: litellm-style exhaustion cannot poison subsequent observations.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aipm.capabilities.dashboard.project_api import DashboardProjectApi
from aipm.models.compose_intelligence import (
    CandidateCacheFreshness,
    CandidateLookupKey,
    DeclaredServiceConfig,
    ImageReference,
    ServiceCandidateReason,
    ServiceCandidateStatus,
)
from aipm.models.project import Project, ProjectCapabilities
from aipm.services.compose.budget import DEFAULT_MAX_NETWORK_OPS, TwoLevelBudget
from aipm.services.compose.cache import (
    CandidateCache,
    DEFAULT_NEGATIVE_TTL_SECONDS,
    DEFAULT_POSITIVE_TTL_SECONDS,
)
from aipm.services.compose.image_ref import parse_image_reference
from aipm.services.compose.intelligence import ComposeIntelligenceService
from aipm.services.compose.registry_client import (
    RegistryCandidateClient,
    RegistryCandidateResult,
)
from aipm.services.compose.scheduler import CandidateQueryScheduler


def test_cache_rejects_network_budget_exhausted_directly() -> None:
    """Requirement 1: CandidateCache.put must ignore NETWORK_BUDGET_EXHAUSTED."""
    cache = CandidateCache()
    key = CandidateLookupKey.from_image_ref(
        parse_image_reference("ghcr.io/berriai/litellm:main-stable"),
        target_arch="arm64",
        target_os="linux",
    )
    res = RegistryCandidateResult(
        status=ServiceCandidateStatus.UNKNOWN,
        reason=ServiceCandidateReason.NETWORK_BUDGET_EXHAUSTED,
        detail="Physical network operations budget reached (50)",
    )

    cache.put(key, res)

    assert cache.get(key) is None
    assert cache.peek(key) is None
    assert len(cache) == 0
    assert cache.stats()["size"] == 0


def test_cache_rejects_budget_exhausted_directly() -> None:
    """Requirement 2: CandidateCache.put must ignore BUDGET_EXHAUSTED."""
    cache = CandidateCache()
    key = CandidateLookupKey.from_image_ref(
        parse_image_reference("ollama/ollama:latest"),
        target_arch="arm64",
        target_os="linux",
    )
    res = RegistryCandidateResult(
        status=ServiceCandidateStatus.UNKNOWN,
        reason=ServiceCandidateReason.BUDGET_EXHAUSTED,
        detail="Logical candidate lookup budget reached (25)",
    )

    cache.put(key, res)

    assert cache.get(key) is None
    assert cache.peek(key) is None
    assert len(cache) == 0


def test_scheduler_does_not_cache_network_budget_exhausted() -> None:
    """Requirement 1 & 3: Scheduler does not populate cache when query_fn returns NETWORK_BUDGET_EXHAUSTED."""
    cache = CandidateCache()
    queries: list[str] = []

    def exhausting_query(
        ref: ImageReference, b: TwoLevelBudget
    ) -> RegistryCandidateResult:
        queries.append(ref.repository)
        # Consume budget
        b.record_network_op(100)
        return RegistryCandidateResult(
            status=ServiceCandidateStatus.UNKNOWN,
            reason=ServiceCandidateReason.NETWORK_BUDGET_EXHAUSTED,
            detail="Budget reached",
        )

    scheduler = CandidateQueryScheduler(cache=cache, query_fn=exhausting_query)

    declared = {
        "litellm": DeclaredServiceConfig(
            service_name="litellm",
            image="ghcr.io/berriai/litellm:main-stable",
            image_ref=parse_image_reference("ghcr.io/berriai/litellm:main-stable"),
        )
    }
    containers = {"litellm": [SimpleNamespace(name="litellm")]}
    runtime_meta = {
        "litellm": {
            "running_img": "ghcr.io/berriai/litellm:main-stable",
            "running_repo_digests": ["ghcr.io/berriai/litellm@sha256:digest1"],
        }
    }
    budget = TwoLevelBudget(max_logical_lookups=5, max_network_ops=1)

    resolutions = scheduler.schedule_and_resolve(
        all_service_names=["litellm"],
        declared_services=declared,
        containers_by_service=containers,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta,
        budget=budget,
        query_registries=True,
    )

    assert resolutions["litellm"].candidate_status == ServiceCandidateStatus.UNKNOWN
    assert (
        resolutions["litellm"].candidate_reason
        == ServiceCandidateReason.NETWORK_BUDGET_EXHAUSTED
    )
    assert len(queries) == 1

    # Invariant: Cache must remain completely empty!
    key = CandidateLookupKey.from_image_ref(
        parse_image_reference("ghcr.io/berriai/litellm:main-stable"),
        target_arch="arm64",
        target_os="linux",
    )
    assert cache.get(key) is None
    assert cache.peek(key) is None
    assert len(cache) == 0


def test_scheduler_does_not_cache_logical_budget_exhausted() -> None:
    """Requirement 2: Scheduler does not cache when logical lookup budget is exhausted."""
    cache = CandidateCache()
    queries: list[str] = []

    def query_mock(ref: ImageReference, b: TwoLevelBudget) -> RegistryCandidateResult:
        queries.append(ref.repository)
        return RegistryCandidateResult(
            status=ServiceCandidateStatus.UNKNOWN,
            reason=ServiceCandidateReason.BUDGET_EXHAUSTED,
            detail="Budget exhausted",
        )

    scheduler = CandidateQueryScheduler(cache=cache, query_fn=query_mock)
    ref = parse_image_reference("test/app:latest")
    declared = {
        "app": DeclaredServiceConfig(
            service_name="app", image="test/app:latest", image_ref=ref
        )
    }
    containers = {"app": [SimpleNamespace(name="c_app")]}
    runtime_meta = {
        "app": {
            "running_img": "test/app:latest",
            "running_repo_digests": ["test/app@sha256:d1"],
        }
    }

    budget = TwoLevelBudget(max_logical_lookups=5, max_network_ops=5)
    resolutions = scheduler.schedule_and_resolve(
        all_service_names=["app"],
        declared_services=declared,
        containers_by_service=containers,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta,
        budget=budget,
        query_registries=True,
    )

    assert (
        resolutions["app"].candidate_reason == ServiceCandidateReason.BUDGET_EXHAUSTED
    )
    key = CandidateLookupKey.from_image_ref(ref, target_arch="arm64", target_os="linux")
    assert cache.get(key) is None
    assert len(cache) == 0


def test_subsequent_observation_with_fresh_budget_retries_and_succeeds() -> None:
    """Requirement 3: Subsequent observation with fresh budget retries instead of receiving cached exhaustion."""
    cache = CandidateCache()
    attempt = 0

    def dynamic_query(
        ref: ImageReference, b: TwoLevelBudget
    ) -> RegistryCandidateResult:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            # First run: simulated network budget exhaustion
            b.record_network_op(50)
            return RegistryCandidateResult(
                status=ServiceCandidateStatus.UNKNOWN,
                reason=ServiceCandidateReason.NETWORK_BUDGET_EXHAUSTED,
                detail="Physical network operations budget reached (50)",
            )
        # Second run: success
        b.record_network_op(10)
        return RegistryCandidateResult(
            status=ServiceCandidateStatus.CURRENT,
            reason=ServiceCandidateReason.UP_TO_DATE,
            index_digest="sha256:resolved_candidate_digest",
            child_digest=None,
            detail="multi-arch (arm64)",
        )

    scheduler = CandidateQueryScheduler(cache=cache, query_fn=dynamic_query)

    ref = parse_image_reference("ghcr.io/berriai/litellm:main-stable")
    declared = {
        "litellm": DeclaredServiceConfig(
            service_name="litellm",
            image="ghcr.io/berriai/litellm:main-stable",
            image_ref=ref,
        )
    }
    containers = {"litellm": [SimpleNamespace(name="litellm")]}
    runtime_meta = {
        "litellm": {
            "running_img": "ghcr.io/berriai/litellm:main-stable",
            "running_repo_digests": [
                "ghcr.io/berriai/litellm@sha256:resolved_candidate_digest"
            ],
        }
    }

    # Run 1: Fails due to budget exhaustion
    budget_1 = TwoLevelBudget(max_logical_lookups=5, max_network_ops=50)
    res_1 = scheduler.schedule_and_resolve(
        all_service_names=["litellm"],
        declared_services=declared,
        containers_by_service=containers,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta,
        budget=budget_1,
        query_registries=True,
    )
    assert res_1["litellm"].candidate_status == ServiceCandidateStatus.UNKNOWN
    assert (
        res_1["litellm"].candidate_reason
        == ServiceCandidateReason.NETWORK_BUDGET_EXHAUSTED
    )
    assert attempt == 1

    # Run 2: Fresh budget — MUST RETRY rather than returning a cached exhaustion result!
    budget_2 = TwoLevelBudget(max_logical_lookups=5, max_network_ops=50)
    res_2 = scheduler.schedule_and_resolve(
        all_service_names=["litellm"],
        declared_services=declared,
        containers_by_service=containers,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta,
        budget=budget_2,
        query_registries=True,
    )
    assert attempt == 2
    assert res_2["litellm"].candidate_status == ServiceCandidateStatus.CURRENT
    assert res_2["litellm"].candidate_reason == ServiceCandidateReason.UP_TO_DATE
    assert res_2["litellm"].candidate_digest == "sha256:resolved_candidate_digest"


def test_positive_candidate_remains_cached_for_positive_ttl() -> None:
    """Requirement 4: Positive candidates remain cached for 900s."""
    cache = CandidateCache(positive_ttl_seconds=DEFAULT_POSITIVE_TTL_SECONDS)
    key = CandidateLookupKey.from_image_ref(
        parse_image_reference("redis:7-alpine"),
        target_arch="arm64",
        target_os="linux",
    )
    pos_res = RegistryCandidateResult(
        status=ServiceCandidateStatus.CURRENT,
        reason=ServiceCandidateReason.UP_TO_DATE,
        index_digest="sha256:redis_index",
    )

    t0 = 1000.0
    cache.put(key, pos_res, now=t0)

    # Within TTL (t0 + 899s)
    cached, freshness = cache.get(key, now=t0 + 899.0)
    assert cached == pos_res
    assert freshness == CandidateCacheFreshness.FRESH

    # Past TTL (t0 + 901s)
    cached, freshness = cache.get(key, now=t0 + 901.0)
    assert cached == pos_res
    assert freshness == CandidateCacheFreshness.STALE


def test_intentional_negative_results_retain_existing_negative_ttl() -> None:
    """Requirement 5: Intentional negative results (e.g. REGISTRY_UNAVAILABLE) retain 60s negative TTL."""
    cache = CandidateCache(negative_ttl_seconds=DEFAULT_NEGATIVE_TTL_SECONDS)
    key = CandidateLookupKey.from_image_ref(
        parse_image_reference("unreachable/app:1.0"),
        target_arch="arm64",
        target_os="linux",
    )
    neg_res = RegistryCandidateResult(
        status=ServiceCandidateStatus.UNKNOWN,
        reason=ServiceCandidateReason.REGISTRY_UNAVAILABLE,
        detail="DNS resolution failed",
    )

    t0 = 1000.0
    cache.put(key, neg_res, is_negative=True, now=t0)

    # Within negative TTL (t0 + 30s)
    cached, freshness = cache.get(key, now=t0 + 30.0)
    assert cached == neg_res
    assert freshness == CandidateCacheFreshness.FRESH

    # Past negative TTL (t0 + 61s)
    cached, freshness = cache.get(key, now=t0 + 61.0)
    assert cached == neg_res
    assert freshness == CandidateCacheFreshness.STALE


def test_cache_hit_miss_counters_correct() -> None:
    """Requirement 6: Cache hit/miss and eviction stats operate accurately."""
    cache = CandidateCache(max_capacity=2)
    k1 = CandidateLookupKey.from_image_ref(parse_image_reference("img1:latest"))
    k2 = CandidateLookupKey.from_image_ref(parse_image_reference("img2:latest"))
    res_pos = RegistryCandidateResult(
        ServiceCandidateStatus.CURRENT, ServiceCandidateReason.UP_TO_DATE, "d1"
    )
    res_exhaust = RegistryCandidateResult(
        ServiceCandidateStatus.UNKNOWN, ServiceCandidateReason.NETWORK_BUDGET_EXHAUSTED
    )

    # Peek on empty: no hit, no miss
    assert cache.peek(k1) is None
    assert cache.stats()["hits"] == 0
    assert cache.stats()["misses"] == 0

    # Get on empty: 1 miss
    assert cache.get(k1) is None
    assert cache.stats()["misses"] == 1

    # Put exhausted result: ignored, size remains 0
    cache.put(k1, res_exhaust)
    assert cache.stats()["size"] == 0
    assert cache.get(k1) is None
    assert cache.stats()["misses"] == 2

    # Put valid positive: size 1
    cache.put(k1, res_pos)
    assert cache.stats()["size"] == 1

    # Get on stored key: 1 hit
    assert cache.get(k1) is not None
    assert cache.stats()["hits"] == 1


def test_scheduler_accounting_remains_exactly_once_per_logical_key() -> None:
    """Requirement 7: Deduplication coalesces multiple services to 1 logical lookup on budget."""
    cache = CandidateCache()
    query_count = 0

    def query_mock(ref: ImageReference, b: TwoLevelBudget) -> RegistryCandidateResult:
        nonlocal query_count
        query_count += 1
        b.record_network_op(200)
        return RegistryCandidateResult(
            status=ServiceCandidateStatus.CURRENT,
            reason=ServiceCandidateReason.UP_TO_DATE,
            index_digest="sha256:shared_image_digest",
        )

    scheduler = CandidateQueryScheduler(cache=cache, query_fn=query_mock)

    # 5 services sharing the exact same image
    services = {
        f"worker_{i}": DeclaredServiceConfig(
            service_name=f"worker_{i}",
            image="shared/worker:latest",
            image_ref=parse_image_reference("shared/worker:latest"),
        )
        for i in range(5)
    }
    containers = {f"worker_{i}": [SimpleNamespace(name=f"c_{i}")] for i in range(5)}
    runtime_meta = {
        f"worker_{i}": {
            "running_img": "shared/worker:latest",
            "running_repo_digests": ["shared/worker@sha256:shared_image_digest"],
        }
        for i in range(5)
    }

    budget = TwoLevelBudget(max_logical_lookups=10, max_network_ops=10)
    resolutions = scheduler.schedule_and_resolve(
        all_service_names=sorted(services.keys()),
        declared_services=services,
        containers_by_service=containers,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta,
        budget=budget,
        query_registries=True,
    )

    assert len(resolutions) == 5
    assert query_count == 1
    assert budget.logical_lookups_performed == 1
    assert budget.network_ops_performed == 1


def test_default_max_network_ops_invariant_remains_50() -> None:
    """Requirement 8: DEFAULT_MAX_NETWORK_OPS must remain 50."""
    assert DEFAULT_MAX_NETWORK_OPS == 50
    budget = TwoLevelBudget()
    assert budget.max_network_ops == 50


def test_public_api_response_schema_invariants() -> None:
    """Requirement 9: DashboardProjectApi schema preserves invariants."""
    mock_intelligence = SimpleNamespace(
        compose_intelligence=lambda pid, query_registries=True: (
            SimpleNamespace(
                id="f55f34521cf1e43173e51795",
                display_name="local-ai-packaged",
                name="local-ai-packaged",
            ),
            SimpleNamespace(
                compose_identity="localai",
                running_services_count=1,
                total_services_count=1,
                updates_available_count=0,
                current_count=1,
                drift_count=0,
                not_applicable_count=0,
                unknown_count=0,
                freshness="fresh",
                services=[
                    SimpleNamespace(
                        service_name="auth",
                        container_names=["supabase-auth"],
                        container_ids=["bad3654a27ef"],
                        state="running",
                        health="healthy",
                        declared_image="supabase/gotrue:v2.189.0",
                        running_image="supabase/gotrue:v2.189.0",
                        running_image_id="sha256:abc",
                        running_repo_digests=["supabase/gotrue@sha256:xyz"],
                        candidate_digest="sha256:xyz",
                        candidate_child_digest="sha256:child",
                        candidate_status=ServiceCandidateStatus.CURRENT,
                        candidate_reason=ServiceCandidateReason.UP_TO_DATE,
                        candidate_detail="up to date",
                        is_build=False,
                        depends_on=["db"],
                        candidate_key=SimpleNamespace(
                            registry="docker.io",
                            repository="supabase/gotrue",
                            tag="v2.189.0",
                            target_os="linux",
                            target_arch="arm64",
                        ),
                        freshness="fresh",
                        observed_at=None,
                        provenance_verified=True,
                    )
                ],
            ),
            None,
        )
    )
    api = DashboardProjectApi(intelligence=mock_intelligence)
    res = api.compose_intelligence("f55f34521cf1e43173e51795")

    assert res["status"] == "ok"
    assert res["available"] is True
    assert "project" in res
    assert res["project"]["compose_identity"] == "localai"
    # Ensure no internal filesystem paths or tokens
    assert "build_context" not in res["project"]["services"][0]
    assert "project_path" not in res["project"]


def test_litellm_style_exhaustion_cannot_poison_later_fresh_observation() -> None:
    """Requirement 10: Multi-service observation where 16 succeed, 17 exhausts budget.

    Proves that the exhausted service (litellm) is NOT cached and resolves
    normally in a subsequent fresh-budget observation.
    """
    global_cache = CandidateCache()
    query_log: list[str] = []

    def mock_query(ref: ImageReference, b: TwoLevelBudget) -> RegistryCandidateResult:
        query_log.append(ref.repository)
        if ref.repository == "berriai/litellm":
            # Simulate 2 network ops performed, then 3rd is blocked
            b.record_network_op(200)
            b.record_network_op(400)
            # Budget check fails
            return RegistryCandidateResult(
                status=ServiceCandidateStatus.UNKNOWN,
                reason=ServiceCandidateReason.NETWORK_BUDGET_EXHAUSTED,
                detail="Physical network operations budget reached (50)",
            )
        # All other services succeed with 3 ops
        b.record_network_op(100)
        b.record_network_op(100)
        b.record_network_op(100)
        return RegistryCandidateResult(
            status=ServiceCandidateStatus.CURRENT,
            reason=ServiceCandidateReason.UP_TO_DATE,
            index_digest=f"sha256:{ref.repository}_digest",
            detail="multi-arch (arm64)",
        )

    scheduler = CandidateQueryScheduler(cache=global_cache, query_fn=mock_query)

    # 16 docker.io services + 1 ghcr.io service (litellm)
    declared: dict[str, DeclaredServiceConfig] = {}
    containers: dict[str, list[Any]] = {}
    runtime_meta: dict[str, dict[str, Any]] = {}

    service_names = [f"svc_{i:02d}" for i in range(16)] + ["litellm"]
    for i in range(16):
        name = f"svc_{i:02d}"
        img = f"docker.io/repo/app_{i:02d}:1.0"
        ref = parse_image_reference(img)
        declared[name] = DeclaredServiceConfig(
            service_name=name, image=img, image_ref=ref
        )
        containers[name] = [SimpleNamespace(name=f"c_{name}")]
        runtime_meta[name] = {
            "running_img": img,
            "running_repo_digests": [f"{img}@sha256:repo/app_{i:02d}_digest"],
        }

    litellm_ref = parse_image_reference("ghcr.io/berriai/litellm:main-stable")
    declared["litellm"] = DeclaredServiceConfig(
        service_name="litellm",
        image="ghcr.io/berriai/litellm:main-stable",
        image_ref=litellm_ref,
    )
    containers["litellm"] = [SimpleNamespace(name="litellm")]
    runtime_meta["litellm"] = {
        "running_img": "ghcr.io/berriai/litellm:main-stable",
        "running_repo_digests": [
            "ghcr.io/berriai/litellm@sha256:berriai/litellm_digest"
        ],
    }

    # RUN 1: Budget of 50
    budget_1 = TwoLevelBudget(max_logical_lookups=25, max_network_ops=50)
    res_1 = scheduler.schedule_and_resolve(
        all_service_names=service_names,
        declared_services=declared,
        containers_by_service=containers,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta,
        budget=budget_1,
        query_registries=True,
    )

    # 16 services succeeded
    for i in range(16):
        assert res_1[f"svc_{i:02d}"].candidate_status == ServiceCandidateStatus.CURRENT
    # litellm exhausted budget
    assert res_1["litellm"].candidate_status == ServiceCandidateStatus.UNKNOWN
    assert (
        res_1["litellm"].candidate_reason
        == ServiceCandidateReason.NETWORK_BUDGET_EXHAUSTED
    )

    # CRITICAL CHECK: litellm must NOT be in the global cache!
    litellm_key = CandidateLookupKey.from_image_ref(litellm_ref)
    assert global_cache.get(litellm_key) is None
    assert global_cache.peek(litellm_key) is None
    # Only the 16 successful keys should be cached!
    assert len(global_cache) == 16

    # RUN 2: Now run an observation where litellm has budget (e.g. 16 services hit cache Tier 0, so litellm gets query)
    def mock_query_run2(
        ref: ImageReference, b: TwoLevelBudget
    ) -> RegistryCandidateResult:
        query_log.append(f"run2_{ref.repository}")
        b.record_network_op(300)
        return RegistryCandidateResult(
            status=ServiceCandidateStatus.CURRENT,
            reason=ServiceCandidateReason.UP_TO_DATE,
            index_digest="sha256:berriai/litellm_digest",
            detail="multi-arch (arm64)",
        )

    scheduler_run2 = CandidateQueryScheduler(
        cache=global_cache, query_fn=mock_query_run2
    )
    budget_2 = TwoLevelBudget(max_logical_lookups=25, max_network_ops=50)

    res_2 = scheduler_run2.schedule_and_resolve(
        all_service_names=service_names,
        declared_services=declared,
        containers_by_service=containers,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta,
        budget=budget_2,
        query_registries=True,
    )

    # litellm was NOT blocked by a cached exhaustion entry; it was retried and SUCCEEDED!
    assert "run2_berriai/litellm" in query_log
    assert res_2["litellm"].candidate_status == ServiceCandidateStatus.CURRENT
    assert res_2["litellm"].candidate_reason == ServiceCandidateReason.UP_TO_DATE
    assert res_2["litellm"].candidate_digest == "sha256:berriai/litellm_digest"
