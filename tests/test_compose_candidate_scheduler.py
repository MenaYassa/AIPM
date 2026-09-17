"""Comprehensive unit, adversarial, and scaling tests for candidate scheduler and budget model (MC-6.15-A.2).

Tests:
1. CandidateLookupKey canonicalization, arch-separation, hashing, and equality.
2. CandidateCache bounded LRU, eviction, TTLs, and freshness transitions.
3. TwoLevelBudget logical vs physical ops, monotonic deadline, byte bounds.
4. CandidateQueryScheduler deterministic prioritization, deduplication, and explainability.
5. Depends-on parsing and projection.
6. Adversarial 26-service A-Z matrix.
7. Synthetic scaling benchmarks (10, 50, 100, 250 services).
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from aipm.models.compose_intelligence import (
    CandidateCacheFreshness,
    CandidateLookupKey,
    DeclaredServiceConfig,
    ImageReference,
    ServiceCandidateReason,
    ServiceCandidateStatus,
)
from aipm.models.project import Project, ProjectCapabilities
from aipm.providers.compose.provider import ComposeProvider
from aipm.services.compose.budget import TwoLevelBudget
from aipm.services.compose.cache import CandidateCache
from aipm.services.compose.config_parser import parse_declared_compose_services
from aipm.services.compose.image_ref import parse_image_reference
from aipm.services.compose.intelligence import ComposeIntelligenceService
from aipm.services.compose.registry_client import RegistryCandidateClient, RegistryCandidateResult
from aipm.services.compose.scheduler import CandidateQueryScheduler, ServiceCandidateResolution


# ==============================================================================
# 1. CandidateLookupKey Tests
# ==============================================================================

def test_candidate_lookup_key_canonicalization() -> None:
    """Verify registry, repo, tag canonicalization and architecture separation."""
    ref1 = parse_image_reference("Docker.io/Library/Nginx:Latest")
    key1 = CandidateLookupKey.from_image_ref(ref1, target_arch="arm64", target_os="linux")

    assert key1.registry == "docker.io"
    assert key1.repository == "library/nginx"
    assert key1.tag == "latest"
    assert key1.target_arch == "arm64"
    assert key1.target_os == "linux"
    assert key1.canonical_str() == "docker.io/library/nginx:latest [linux/arm64]"

    # Default tag
    ref_notag = parse_image_reference("postgres")
    key_notag = CandidateLookupKey.from_image_ref(ref_notag, target_arch="arm64", target_os="linux")
    assert key_notag.tag == "latest"

    # Architecture separation: arm64 != amd64
    key_arm64 = CandidateLookupKey.from_image_ref(ref1, target_arch="arm64")
    key_amd64 = CandidateLookupKey.from_image_ref(ref1, target_arch="amd64")
    assert key_arm64 != key_amd64
    assert hash(key_arm64) != hash(key_amd64)

    # Identical references coalesce
    ref2 = parse_image_reference("nginx:latest")
    key2 = CandidateLookupKey.from_image_ref(ref2, target_arch="arm64", target_os="linux")
    assert key1 == key2
    assert hash(key1) == hash(key2)


# ==============================================================================
# 2. CandidateCache Tests
# ==============================================================================

def test_candidate_cache_lru_bounding_and_eviction() -> None:
    """Verify bounded capacity and LRU eviction policy."""
    cache = CandidateCache(max_capacity=3, positive_ttl_seconds=600.0)

    keys = [
        CandidateLookupKey("docker.io", f"repo{i}", "latest", "arm64", "linux")
        for i in range(4)
    ]
    res = RegistryCandidateResult(
        status=ServiceCandidateStatus.CURRENT,
        reason=ServiceCandidateReason.UP_TO_DATE,
        index_digest="sha256:dummy",
    )

    # Put 3 items
    cache.put(keys[0], res)
    cache.put(keys[1], res)
    cache.put(keys[2], res)
    assert len(cache) == 3

    # Access key 0 so it becomes most recently used
    val = cache.get(keys[0])
    assert val is not None
    assert val[1] == CandidateCacheFreshness.FRESH

    # Put key 3 -> triggers eviction of oldest (key 1)
    cache.put(keys[3], res)
    assert len(cache) == 3

    assert cache.get(keys[1]) is None  # evicted!
    assert cache.get(keys[0]) is not None  # kept!
    assert cache.get(keys[2]) is not None
    assert cache.get(keys[3]) is not None

    stats = cache.stats()
    assert stats["size"] == 3
    assert stats["max_capacity"] == 3
    assert stats["evictions"] == 1


def test_candidate_cache_freshness_and_ttls() -> None:
    """Verify positive vs negative TTL expiration and FRESH -> STALE transition."""
    cache = CandidateCache(positive_ttl_seconds=100.0, negative_ttl_seconds=10.0)
    key_pos = CandidateLookupKey("docker.io", "positive", "1.0", "arm64", "linux")
    key_neg = CandidateLookupKey("docker.io", "negative", "1.0", "arm64", "linux")

    pos_res = RegistryCandidateResult(
        status=ServiceCandidateStatus.CURRENT,
        reason=ServiceCandidateReason.UP_TO_DATE,
        index_digest="sha256:pos",
    )
    neg_res = RegistryCandidateResult(
        status=ServiceCandidateStatus.UNKNOWN,
        reason=ServiceCandidateReason.REGISTRY_UNAVAILABLE,
        detail="timeout",
    )

    t0 = 1000.0
    cache.put(key_pos, pos_res, is_negative=False, now=t0)
    cache.put(key_neg, neg_res, is_negative=True, now=t0)

    # At t = 1005: both fresh
    assert cache.get(key_pos, now=1005.0)[1] == CandidateCacheFreshness.FRESH
    assert cache.get(key_neg, now=1005.0)[1] == CandidateCacheFreshness.FRESH

    # At t = 1015: negative is STALE, positive is still FRESH
    assert cache.get(key_neg, now=1015.0)[1] == CandidateCacheFreshness.STALE
    assert cache.get(key_pos, now=1015.0)[1] == CandidateCacheFreshness.FRESH

    # At t = 1105: positive is now STALE
    assert cache.get(key_pos, now=1105.0)[1] == CandidateCacheFreshness.STALE


# ==============================================================================
# 3. TwoLevelBudget Tests
# ==============================================================================

def test_two_level_budget_logical_exhaustion() -> None:
    """Verify Level 1 logical query limit exhaustion."""
    budget = TwoLevelBudget(max_logical_lookups=2, max_network_ops=10)

    allowed, reason, _ = budget.check_logical_lookup()
    assert allowed and reason is None
    budget.record_logical_lookup()

    allowed, reason, _ = budget.check_logical_lookup()
    assert allowed and reason is None
    budget.record_logical_lookup()

    # 3rd query blocked by logical budget
    allowed, reason, detail = budget.check_logical_lookup()
    assert not allowed
    assert reason == ServiceCandidateReason.BUDGET_EXHAUSTED
    assert "Logical candidate lookup budget reached (2)" in str(detail)


def test_two_level_budget_physical_network_exhaustion() -> None:
    """Verify Level 2 physical network operations limit exhaustion."""
    budget = TwoLevelBudget(max_logical_lookups=10, max_network_ops=2)

    # 1st logical query starts
    assert budget.check_logical_lookup()[0]
    budget.record_logical_lookup()

    # Consumes 2 network operations (e.g. 401 challenge + token request)
    assert budget.check_network_op()[0]
    budget.record_network_op(100)
    assert budget.check_network_op()[0]
    budget.record_network_op(100)

    # 3rd network op blocked
    allowed, reason, detail = budget.check_network_op()
    assert not allowed
    assert reason == ServiceCandidateReason.NETWORK_BUDGET_EXHAUSTED
    assert "Physical network operations budget reached (2)" in str(detail)

    # Subsequent logical lookup is also blocked because network budget is dead
    allowed_log, reason_log, _ = budget.check_logical_lookup()
    assert not allowed_log
    assert reason_log == ServiceCandidateReason.NETWORK_BUDGET_EXHAUSTED


def test_two_level_budget_aggregate_bytes_limit() -> None:
    """Verify aggregate byte threshold protects against streaming memory explosion."""
    budget = TwoLevelBudget(max_aggregate_bytes=1000)

    budget.record_network_op(500)
    assert budget.aggregate_bytes_received == 500

    with pytest.raises(ValueError, match="Aggregate response bytes .* exceeded limit"):
        budget.record_network_op(600)  # total 1100 > 1000


# ==============================================================================
# 4. CandidateQueryScheduler Tests
# ==============================================================================

def test_scheduler_deduplication_intra_observation() -> None:
    """Verify that multiple services sharing the exact same image trigger only 1 query."""
    cache = CandidateCache()
    query_mock = MagicMock(return_value=RegistryCandidateResult(
        status=ServiceCandidateStatus.CURRENT,
        reason=ServiceCandidateReason.UP_TO_DATE,
        index_digest="sha256:shared1234567890abcdef",
        detail="mock single",
    ))

    scheduler = CandidateQueryScheduler(cache=cache, query_fn=query_mock)

    declared = {
        "web1": DeclaredServiceConfig(
            service_name="web1",
            image="nginx:alpine",
            image_ref=parse_image_reference("nginx:alpine"),
        ),
        "web2": DeclaredServiceConfig(
            service_name="web2",
            image="nginx:alpine",
            image_ref=parse_image_reference("nginx:alpine"),
        ),
        "web3": DeclaredServiceConfig(
            service_name="web3",
            image="nginx:alpine",
            image_ref=parse_image_reference("nginx:alpine"),
        ),
    }

    containers = {
        "web1": [SimpleNamespace(name="c1")],
        "web2": [SimpleNamespace(name="c2")],
        "web3": [SimpleNamespace(name="c3")],
    }

    runtime_meta = {
        "web1": {"running_img": "nginx:alpine", "running_repo_digests": ["nginx@sha256:shared1234567890abcdef"]},
        "web2": {"running_img": "nginx:alpine", "running_repo_digests": ["nginx@sha256:shared1234567890abcdef"]},
        "web3": {"running_img": "nginx:alpine", "running_repo_digests": ["nginx@sha256:otherdigest9999999999"]},
    }

    budget = TwoLevelBudget(max_logical_lookups=5)

    resolutions = scheduler.schedule_and_resolve(
        all_service_names=["web1", "web2", "web3"],
        declared_services=declared,
        containers_by_service=containers,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta,
        budget=budget,
        query_registries=True,
    )

    # Exactly 1 query executed despite 3 services!
    assert query_mock.call_count == 1

    # web1 and web2 matched candidate -> CURRENT
    assert resolutions["web1"].candidate_status == ServiceCandidateStatus.CURRENT
    assert resolutions["web1"].scheduling_state == "queried"

    assert resolutions["web2"].candidate_status == ServiceCandidateStatus.CURRENT
    assert "deduplicated" in resolutions["web2"].scheduling_state

    # web3 differed from candidate -> UPDATE_AVAILABLE (independent projection!)
    assert resolutions["web3"].candidate_status == ServiceCandidateStatus.UPDATE_AVAILABLE
    assert "deduplicated" in resolutions["web3"].scheduling_state


def test_scheduler_deterministic_fair_priority() -> None:
    """Verify scheduler prioritizes fresh cache hits and running services before stopped services."""
    cache = CandidateCache()
    cached_ref = parse_image_reference("cached-app:latest")
    cached_key = CandidateLookupKey.from_image_ref(cached_ref, target_arch="arm64", target_os="linux")
    cache.put(cached_key, RegistryCandidateResult(
        status=ServiceCandidateStatus.CURRENT,
        reason=ServiceCandidateReason.UP_TO_DATE,
        index_digest="sha256:cached",
    ))

    queries_order: list[str] = []

    def tracking_query(ref: ImageReference, budget: TwoLevelBudget) -> RegistryCandidateResult:
        queries_order.append(ref.repository)
        return RegistryCandidateResult(
            status=ServiceCandidateStatus.CURRENT,
            reason=ServiceCandidateReason.UP_TO_DATE,
            index_digest=f"sha256:{ref.repository}",
        )

    scheduler = CandidateQueryScheduler(cache=cache, query_fn=tracking_query)

    declared = {
        "z_stopped": DeclaredServiceConfig(service_name="z_stopped", image="z-app:1.0", image_ref=parse_image_reference("z-app:1.0")),
        "m_running_digests": DeclaredServiceConfig(service_name="m_running_digests", image="m-app:1.0", image_ref=parse_image_reference("m-app:1.0")),
        "a_running_nodigests": DeclaredServiceConfig(service_name="a_running_nodigests", image="a-app:1.0", image_ref=parse_image_reference("a-app:1.0")),
        "c_cached": DeclaredServiceConfig(service_name="c_cached", image="cached-app:latest", image_ref=parse_image_reference("cached-app:latest")),
    }

    containers = {
        "m_running_digests": [SimpleNamespace(name="cm")],
        "a_running_nodigests": [SimpleNamespace(name="ca")],
        "c_cached": [SimpleNamespace(name="cc")],
        # z_stopped has no containers
    }

    runtime_meta = {
        "m_running_digests": {"running_img": "m-app:1.0", "running_repo_digests": ["m-app@sha256:something"]},
        "a_running_nodigests": {"running_img": "a-app:1.0", "running_repo_digests": []},
        "c_cached": {"running_img": "cached-app:latest", "running_repo_digests": ["cached-app@sha256:cached"]},
    }

    budget = TwoLevelBudget(max_logical_lookups=10)
    resolutions = scheduler.schedule_and_resolve(
        all_service_names=sorted(declared.keys()),
        declared_services=declared,
        containers_by_service=containers,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta,
        budget=budget,
        query_registries=True,
    )

    # z_stopped is skipped without consuming query budget
    assert resolutions["z_stopped"].candidate_reason == ServiceCandidateReason.NO_RUNNING_CONTAINER
    assert resolutions["c_cached"].scheduling_state == "cache_hit_fresh"

    # Running with digests was queried first, followed by running without digests
    assert queries_order == ["library/m-app", "library/a-app"]


# ==============================================================================
# 5. Depends-on Parsing and Metadata Verification
# ==============================================================================

def test_compose_parser_and_observation_depends_on(tmp_path: Path) -> None:
    """Verify depends_on parsing for both lists and dictionaries, including !reset."""
    base_file = tmp_path / "docker-compose.yml"
    base_file.write_text(
        "services:\n"
        "  db:\n"
        "    image: postgres:15\n"
        "  api:\n"
        "    image: api:latest\n"
        "    depends_on:\n"
        "      - db\n"
        "  worker:\n"
        "    image: worker:latest\n"
        "    depends_on:\n"
        "      db:\n"
        "        condition: service_healthy\n"
        "      api:\n"
        "        condition: service_started\n"
    )

    override_file = tmp_path / "docker-compose.override.yml"
    override_file.write_text(
        "services:\n"
        "  worker:\n"
        "    depends_on: !reset\n"
        "      - db\n"
    )

    declared = parse_declared_compose_services([base_file, override_file], project_root=tmp_path)

    assert declared["api"].depends_on == ("db",)
    assert declared["worker"].depends_on == ("db",)  # reset to only db

    # Verify projection into ComposeIntelligenceService observation
    proj = Project(
        name="dep-test",
        path=str(tmp_path),
        compose_files=[str(base_file), str(override_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = []
    service = ComposeIntelligenceService(compose_provider=mock_provider)

    obs = service.observe(proj, query_registries=False)
    services_by_name = {s.service_name: s for s in obs.services}

    assert services_by_name["api"].depends_on == ("db",)
    assert services_by_name["worker"].depends_on == ("db",)
    assert services_by_name["db"].depends_on == ()

    # Verify to_dict output includes depends_on
    d = obs.to_dict()
    svc_api_dict = next(s for s in d["services"] if s["service_name"] == "api")
    assert svc_api_dict["depends_on"] == ["db"]


# ==============================================================================
# 6. Adversarial A-Z 26-Service Matrix
# ==============================================================================

def test_adversarial_az_26_service_matrix(tmp_path: Path) -> None:
    """Verify deterministic, bounded behavior across 26 distinct service configurations (A through Z)."""
    lines = ["services:"]
    # Generate 26 services: a to z
    for idx, letter in enumerate("abcdefghijklmnopqrstuvwxyz"):
        if letter in ("a", "b", "c"):
            # Pinned digests
            lines.append(f"  svc_{letter}:")
            lines.append(f"    image: app:v1@sha256:{letter * 64}")
        elif letter in ("d", "e"):
            # Local builds
            lines.append(f"  svc_{letter}:")
            lines.append("    build: .")
        elif letter in ("f", "g", "h"):
            # Duplicate image: alpine:3.18
            lines.append(f"  svc_{letter}:")
            lines.append("    image: alpine:3.18")
        elif letter in ("i", "j"):
            # Profile gated: experimental
            lines.append(f"  svc_{letter}:")
            lines.append("    image: python:3.11")
            lines.append("    profiles: [experimental]")
        elif letter == "k":
            # Malformed image expression
            lines.append(f"  svc_{letter}:")
            lines.append("    image: '${UNRESOLVED_IMAGE_VAR}'")
        else:
            # Standard services
            lines.append(f"  svc_{letter}:")
            lines.append(f"    image: svc-{letter}:latest")

    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("\n".join(lines))

    proj = Project(
        name="az-matrix",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    containers: list[Any] = []
    # Make running containers for all except stopped (s, t, u) and profile (i, j)
    for letter in "abcdefghijklmnopqrstuvwxyz":
        if letter in ("s", "t", "u", "i", "j"):
            continue
        c = SimpleNamespace(
            id=f"cid_{letter}",
            short_id=f"cid_{letter}",
            name=f"az_svc_{letter}_1",
            status="running",
            labels={
                "com.docker.compose.project": "az-matrix",
                "com.docker.compose.service": f"svc_{letter}",
                "com.docker.compose.project.working_dir": str(tmp_path),
            },
            attrs={"State": {"Status": "running"}, "Config": {"Image": f"svc-{letter}:latest"}},
            image=SimpleNamespace(
                id=f"sha256:{letter * 64}",
                tags=[f"svc-{letter}:latest"],
                attrs={"RepoDigests": [f"svc-{letter}@sha256:{letter * 64}"]},
            ),
            ports={},
        )
        containers.append(c)

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = containers

    # Mock registry client with logical budget = 5
    mock_registry = MagicMock(spec=RegistryCandidateClient)
    mock_registry.max_queries = 5
    mock_registry.query_candidate_digest.return_value = RegistryCandidateResult(
        status=ServiceCandidateStatus.CURRENT,
        reason=ServiceCandidateReason.UP_TO_DATE,
        index_digest="sha256:candidate_digest_ok",
        detail="mock",
    )

    service = ComposeIntelligenceService(compose_provider=mock_provider, registry_client=mock_registry)
    obs = service.observe(proj, query_registries=True)

    assert obs.total_services_count == 26
    assert len(obs.services) == 26

    # Verify query budget was strictly respected
    assert mock_registry.query_candidate_digest.call_count <= 5

    services_by_name = {s.service_name: s for s in obs.services}

    # Stopped services: NO_RUNNING_CONTAINER
    assert services_by_name["svc_s"].candidate_reason == ServiceCandidateReason.NO_RUNNING_CONTAINER
    assert services_by_name["svc_s"].candidate_status == ServiceCandidateStatus.NOT_APPLICABLE

    # Profile gated services: DISABLED_BY_PROFILE
    assert services_by_name["svc_i"].candidate_reason == ServiceCandidateReason.DISABLED_BY_PROFILE
    assert services_by_name["svc_i"].candidate_status == ServiceCandidateStatus.NOT_APPLICABLE

    # Local builds: LOCAL_BUILD
    assert services_by_name["svc_d"].candidate_reason == ServiceCandidateReason.LOCAL_BUILD
    assert services_by_name["svc_d"].candidate_status == ServiceCandidateStatus.NOT_APPLICABLE

    # Malformed: MALFORMED_IMAGE_REFERENCE
    assert services_by_name["svc_k"].candidate_reason == ServiceCandidateReason.MALFORMED_IMAGE_REFERENCE
    assert services_by_name["svc_k"].candidate_status == ServiceCandidateStatus.UNKNOWN


# ==============================================================================
# 7. Synthetic Scaling Benchmarks (10, 50, 100, 250 Services)
# ==============================================================================

@pytest.mark.parametrize("service_count", [10, 50, 100, 250])
def test_synthetic_scaling_scheduler(service_count: int) -> None:
    """Verify candidate scheduler scales linearly and finishes in sub-second time without budget leakage."""
    cache = CandidateCache(max_capacity=500)

    # Mock registry returns deterministic digest
    queries_made = 0

    def mock_query(ref: ImageReference, budget: TwoLevelBudget) -> RegistryCandidateResult:
        nonlocal queries_made
        queries_made += 1
        return RegistryCandidateResult(
            status=ServiceCandidateStatus.CURRENT,
            reason=ServiceCandidateReason.UP_TO_DATE,
            index_digest=f"sha256:{ref.repository}_digest",
        )

    scheduler = CandidateQueryScheduler(cache=cache, query_fn=mock_query)

    # Half services share image with another service (tests deduplication scaling)
    declared: dict[str, DeclaredServiceConfig] = {}
    containers: dict[str, list[Any]] = {}
    runtime_meta: dict[str, dict[str, Any]] = {}

    for i in range(service_count):
        svc_name = f"svc_{i:03d}"
        image_name = f"app_{i % 15}:v1"  # 15 distinct images across N services
        ref = parse_image_reference(image_name)

        declared[svc_name] = DeclaredServiceConfig(
            service_name=svc_name,
            image=image_name,
            image_ref=ref,
        )
        containers[svc_name] = [SimpleNamespace(name=f"c_{i}")]
        runtime_meta[svc_name] = {
            "running_img": image_name,
            "running_repo_digests": [f"{image_name}@sha256:{ref.repository}_digest"],
        }

    # Bounded budget of 10 logical queries
    max_lookups = 10
    budget = TwoLevelBudget(max_logical_lookups=max_lookups)

    start_t = time.perf_counter()
    resolutions = scheduler.schedule_and_resolve(
        all_service_names=sorted(declared.keys()),
        declared_services=declared,
        containers_by_service=containers,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta,
        budget=budget,
        query_registries=True,
    )
    elapsed = time.perf_counter() - start_t

    # All services must have resolutions
    assert len(resolutions) == service_count

    # Queries made must never exceed max_logical_lookups (10)
    assert queries_made <= max_lookups
    assert budget.logical_lookups_performed <= max_lookups

    # Performance: 250 services resolved in under 0.5s!
    assert elapsed < 0.5, f"Scaling test for {service_count} services took {elapsed:.3f}s (exceeds 0.5s)"


# ==============================================================================
# 8. MC-6.15-A.2-R2 Reconciliation & Accounting Invariant Tests
# ==============================================================================

def test_candidate_cache_peek_semantics() -> None:
    """Verify peek inspects cache without mutating hit/miss counters or LRU order."""
    cache = CandidateCache(max_capacity=2)
    ref1 = parse_image_reference("app1:latest")
    ref2 = parse_image_reference("app2:latest")
    ref3 = parse_image_reference("app3:latest")
    k1 = CandidateLookupKey.from_image_ref(ref1, target_arch="arm64", target_os="linux")
    k2 = CandidateLookupKey.from_image_ref(ref2, target_arch="arm64", target_os="linux")
    k3 = CandidateLookupKey.from_image_ref(ref3, target_arch="arm64", target_os="linux")

    r1 = RegistryCandidateResult(ServiceCandidateStatus.CURRENT, ServiceCandidateReason.UP_TO_DATE, "d1")
    r2 = RegistryCandidateResult(ServiceCandidateStatus.CURRENT, ServiceCandidateReason.UP_TO_DATE, "d2")
    r3 = RegistryCandidateResult(ServiceCandidateStatus.CURRENT, ServiceCandidateReason.UP_TO_DATE, "d3")

    # Peek on missing key
    assert cache.peek(k1) is None
    stats = cache.stats()
    assert stats["misses"] == 0  # peek must NOT count as a miss!
    assert stats["hits"] == 0

    # Put k1, k2
    cache.put(k1, r1)
    cache.put(k2, r2)

    # Peek on k1
    peeked = cache.peek(k1)
    assert peeked is not None
    assert peeked[0] == r1
    stats = cache.stats()
    assert stats["hits"] == 0  # peek must NOT count as a hit!
    assert stats["misses"] == 0

    # Put k3: since k1 was peeked (not get'ed), k1 remains the oldest entry and must be evicted!
    cache.put(k3, r3)
    assert cache.peek(k1) is None  # k1 was evicted
    assert cache.peek(k2) is not None  # k2 remains
    assert cache.peek(k3) is not None  # k3 remains


def test_deduplication_exact_accounting_invariants() -> None:
    """Verify exact 1:1 accounting between unique candidate queries and logical budget increments."""
    # Test 1: 10 services, 1 canonical candidate key => logical_lookups == 1
    cache = CandidateCache()
    query_count = 0

    def query_mock(ref: ImageReference, b: TwoLevelBudget) -> RegistryCandidateResult:
        nonlocal query_count
        query_count += 1
        return RegistryCandidateResult(
            status=ServiceCandidateStatus.CURRENT,
            reason=ServiceCandidateReason.UP_TO_DATE,
            index_digest="sha256:shared_single_digest",
        )

    scheduler = CandidateQueryScheduler(cache=cache, query_fn=query_mock)

    declared_10_1 = {
        f"web_{i}": DeclaredServiceConfig(
            service_name=f"web_{i}",
            image="nginx:alpine",
            image_ref=parse_image_reference("nginx:alpine"),
        )
        for i in range(10)
    }
    containers_10_1 = {f"web_{i}": [SimpleNamespace(name=f"c_{i}")] for i in range(10)}
    runtime_meta_10_1 = {
        f"web_{i}": {
            "running_img": "nginx:alpine",
            "running_repo_digests": ["nginx@sha256:shared_single_digest"],
        }
        for i in range(10)
    }
    budget_10_1 = TwoLevelBudget(max_logical_lookups=25)

    resolutions_10_1 = scheduler.schedule_and_resolve(
        all_service_names=sorted(declared_10_1.keys()),
        declared_services=declared_10_1,
        containers_by_service=containers_10_1,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta_10_1,
        budget=budget_10_1,
        query_registries=True,
    )

    assert len(resolutions_10_1) == 10
    assert query_count == 1
    assert budget_10_1.logical_lookups_performed == 1  # EXACTLY 1!

    # Test 2: 10 services, 5 unique candidate keys => logical_lookups == 5
    query_count = 0
    declared_10_5 = {
        f"svc_{i}": DeclaredServiceConfig(
            service_name=f"svc_{i}",
            image=f"img_{i % 5}:1.0",
            image_ref=parse_image_reference(f"img_{i % 5}:1.0"),
        )
        for i in range(10)
    }
    containers_10_5 = {f"svc_{i}": [SimpleNamespace(name=f"c_{i}")] for i in range(10)}
    runtime_meta_10_5 = {
        f"svc_{i}": {
            "running_img": f"img_{i % 5}:1.0",
            "running_repo_digests": [f"img_{i % 5}@sha256:digest_{i % 5}"],
        }
        for i in range(10)
    }
    budget_10_5 = TwoLevelBudget(max_logical_lookups=25)

    resolutions_10_5 = scheduler.schedule_and_resolve(
        all_service_names=sorted(declared_10_5.keys()),
        declared_services=declared_10_5,
        containers_by_service=containers_10_5,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta_10_5,
        budget=budget_10_5,
        query_registries=True,
    )

    assert len(resolutions_10_5) == 10
    assert query_count == 5
    assert budget_10_5.logical_lookups_performed == 5  # EXACTLY 5!

    # Test 3: 100 services, 15 unique candidate keys => logical_lookups == 15
    query_count = 0
    declared_100_15 = {
        f"app_{i}": DeclaredServiceConfig(
            service_name=f"app_{i}",
            image=f"stack_img_{i % 15}:latest",
            image_ref=parse_image_reference(f"stack_img_{i % 15}:latest"),
        )
        for i in range(100)
    }
    containers_100_15 = {f"app_{i}": [SimpleNamespace(name=f"c_{i}")] for i in range(100)}
    runtime_meta_100_15 = {
        f"app_{i}": {
            "running_img": f"stack_img_{i % 15}:latest",
            "running_repo_digests": [f"stack_img_{i % 15}@sha256:digest_{i % 15}"],
        }
        for i in range(100)
    }
    budget_100_15 = TwoLevelBudget(max_logical_lookups=25)

    resolutions_100_15 = scheduler.schedule_and_resolve(
        all_service_names=sorted(declared_100_15.keys()),
        declared_services=declared_100_15,
        containers_by_service=containers_100_15,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta_100_15,
        budget=budget_100_15,
        query_registries=True,
    )

    assert len(resolutions_100_15) == 100
    assert query_count == 15
    assert budget_100_15.logical_lookups_performed == 15  # EXACTLY 15!


def test_physical_vs_logical_budget_separation_and_exhaustion() -> None:
    """Verify 1 logical lookup consumes 3 physical network ops in a 401 challenge, and network exhaustion does not double-count logical ops."""
    # Simulate a 401 challenge flow with mock client
    budget = TwoLevelBudget(max_logical_lookups=10, max_network_ops=10)

    def auth_flow_mock(ref: ImageReference, b: TwoLevelBudget) -> RegistryCandidateResult:
        # Step 1: Initial manifest request (returns 401)
        assert b.check_network_op()[0]
        b.record_network_op(200)

        # Step 2: Token request (returns 200)
        assert b.check_network_op()[0]
        b.record_network_op(400)

        # Step 3: Authenticated manifest request (returns 200)
        assert b.check_network_op()[0]
        b.record_network_op(800)

        return RegistryCandidateResult(
            status=ServiceCandidateStatus.CURRENT,
            reason=ServiceCandidateReason.UP_TO_DATE,
            index_digest="sha256:auth_flow_digest",
        )

    scheduler = CandidateQueryScheduler(query_fn=auth_flow_mock)
    declared = {
        "svc1": DeclaredServiceConfig(service_name="svc1", image="app:1.0", image_ref=parse_image_reference("app:1.0"))
    }
    containers = {"svc1": [SimpleNamespace(name="c1")]}
    runtime_meta = {"svc1": {"running_img": "app:1.0", "running_repo_digests": ["app@sha256:auth_flow_digest"]}}

    resolutions = scheduler.schedule_and_resolve(
        all_service_names=["svc1"],
        declared_services=declared,
        containers_by_service=containers,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta,
        budget=budget,
        query_registries=True,
    )

    assert resolutions["svc1"].candidate_status == ServiceCandidateStatus.CURRENT
    # 1 logical candidate lookup, but 3 physical network operations!
    assert budget.logical_lookups_performed == 1
    assert budget.network_ops_performed == 3
    assert budget.aggregate_bytes_received == 1400

    # Test physical network exhaustion does NOT increment logical lookup more than once
    tight_network_budget = TwoLevelBudget(max_logical_lookups=5, max_network_ops=1)

    def exhausting_query(ref: ImageReference, b: TwoLevelBudget) -> RegistryCandidateResult:
        # 1st op succeeds
        assert b.check_network_op()[0]
        b.record_network_op(100)
        # 2nd op blocked by physical network budget
        allowed, reason, detail = b.check_network_op()
        assert not allowed
        return RegistryCandidateResult(
            status=ServiceCandidateStatus.UNKNOWN,
            reason=reason or ServiceCandidateReason.NETWORK_BUDGET_EXHAUSTED,
            detail=detail,
        )

    scheduler_tight = CandidateQueryScheduler(query_fn=exhausting_query)
    resolutions_tight = scheduler_tight.schedule_and_resolve(
        all_service_names=["svc1"],
        declared_services=declared,
        containers_by_service=containers,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta,
        budget=tight_network_budget,
        query_registries=True,
    )

    assert resolutions_tight["svc1"].candidate_reason == ServiceCandidateReason.NETWORK_BUDGET_EXHAUSTED
    assert tight_network_budget.logical_lookups_performed == 1  # Incremented exactly once!
    assert tight_network_budget.network_ops_performed == 1


def test_cache_miss_vs_hit_vs_dedup_budget_consumption() -> None:
    """Verify cache miss for fast-path services, cache hit, and deduplication consume zero logical budget."""
    cache = CandidateCache()
    cached_ref = parse_image_reference("cached-service:1.0")
    cached_key = CandidateLookupKey.from_image_ref(cached_ref, target_arch="arm64", target_os="linux")
    cache.put(cached_key, RegistryCandidateResult(
        status=ServiceCandidateStatus.CURRENT,
        reason=ServiceCandidateReason.UP_TO_DATE,
        index_digest="sha256:cached_known_digest",
    ))

    query_count = 0

    def query_mock(ref: ImageReference, b: TwoLevelBudget) -> RegistryCandidateResult:
        nonlocal query_count
        query_count += 1
        return RegistryCandidateResult(
            status=ServiceCandidateStatus.CURRENT,
            reason=ServiceCandidateReason.UP_TO_DATE,
            index_digest="sha256:queried_digest",
        )

    scheduler = CandidateQueryScheduler(cache=cache, query_fn=query_mock)

    declared = {
        # 1. Stopped service (cache miss, but skipped via fast-path)
        "stopped_svc": DeclaredServiceConfig(
            service_name="stopped_svc",
            image="stopped-app:1.0",
            image_ref=parse_image_reference("stopped-app:1.0"),
        ),
        # 2. Cache hit service (already in cache)
        "cached_svc": DeclaredServiceConfig(
            service_name="cached_svc",
            image="cached-service:1.0",
            image_ref=cached_ref,
        ),
        # 3. Fresh uncached service (triggers 1 logical lookup)
        "fresh_svc_1": DeclaredServiceConfig(
            service_name="fresh_svc_1",
            image="fresh-app:1.0",
            image_ref=parse_image_reference("fresh-app:1.0"),
        ),
        # 4. Duplicate of fresh service (coalesced; 0 additional logical budget)
        "fresh_svc_2": DeclaredServiceConfig(
            service_name="fresh_svc_2",
            image="fresh-app:1.0",
            image_ref=parse_image_reference("fresh-app:1.0"),
        ),
    }

    containers = {
        "cached_svc": [SimpleNamespace(name="c_cached")],
        "fresh_svc_1": [SimpleNamespace(name="c_fresh_1")],
        "fresh_svc_2": [SimpleNamespace(name="c_fresh_2")],
        # stopped_svc has no container
    }

    runtime_meta = {
        "cached_svc": {"running_img": "cached-service:1.0", "running_repo_digests": ["cached-service@sha256:cached_known_digest"]},
        "fresh_svc_1": {"running_img": "fresh-app:1.0", "running_repo_digests": ["fresh-app@sha256:queried_digest"]},
        "fresh_svc_2": {"running_img": "fresh-app:1.0", "running_repo_digests": ["fresh-app@sha256:queried_digest"]},
    }

    budget = TwoLevelBudget(max_logical_lookups=10)

    resolutions = scheduler.schedule_and_resolve(
        all_service_names=sorted(declared.keys()),
        declared_services=declared,
        containers_by_service=containers,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta,
        budget=budget,
        query_registries=True,
    )

    # 1. Stopped service: skipped via fast-path, 0 logical budget
    assert resolutions["stopped_svc"].candidate_reason == ServiceCandidateReason.NO_RUNNING_CONTAINER
    # 2. Cache hit: 0 logical budget consumed
    assert resolutions["cached_svc"].scheduling_state == "cache_hit_fresh"
    # 3. Fresh service: queried
    assert resolutions["fresh_svc_1"].scheduling_state == "queried"
    # 4. Duplicate service: deduplicated, 0 additional logical budget
    assert "deduplicated" in resolutions["fresh_svc_2"].scheduling_state

    # Across all 4 services, EXACTLY 1 logical lookup was performed!
    assert query_count == 1
    assert budget.logical_lookups_performed == 1


def test_fairness_running_services_budget_sufficiency() -> None:
    """Verify 21 running services with 15 unique keys all resolve with budget >= 15, and duplicates never consume budget."""
    # 21 running services sharing 15 unique image keys
    # Keys 0 to 5 are shared by 2 services each (12 services)
    # Keys 6 to 14 are used by 1 service each (9 services)
    # Total = 12 + 9 = 21 services
    cache = CandidateCache()
    query_count = 0

    def query_mock(ref: ImageReference, b: TwoLevelBudget) -> RegistryCandidateResult:
        nonlocal query_count
        query_count += 1
        return RegistryCandidateResult(
            status=ServiceCandidateStatus.CURRENT,
            reason=ServiceCandidateReason.UP_TO_DATE,
            index_digest=f"sha256:{ref.repository}_digest",
        )

    scheduler = CandidateQueryScheduler(cache=cache, query_fn=query_mock)

    declared: dict[str, DeclaredServiceConfig] = {}
    containers: dict[str, list[Any]] = {}
    runtime_meta: dict[str, dict[str, Any]] = {}

    service_names = []
    # 12 services sharing 6 keys
    for i in range(12):
        s_name = f"shared_svc_{i}"
        service_names.append(s_name)
        img_name = f"shared_img_{i % 6}:1.0"
        ref = parse_image_reference(img_name)
        declared[s_name] = DeclaredServiceConfig(service_name=s_name, image=img_name, image_ref=ref)
        containers[s_name] = [SimpleNamespace(name=f"c_{s_name}")]
        runtime_meta[s_name] = {
            "running_img": img_name,
            "running_repo_digests": [f"{img_name}@sha256:{ref.repository}_digest"],
        }

    # 9 single services using 9 distinct keys
    for i in range(6, 15):
        s_name = f"single_svc_{i}"
        service_names.append(s_name)
        img_name = f"single_img_{i}:1.0"
        ref = parse_image_reference(img_name)
        declared[s_name] = DeclaredServiceConfig(service_name=s_name, image=img_name, image_ref=ref)
        containers[s_name] = [SimpleNamespace(name=f"c_{s_name}")]
        runtime_meta[s_name] = {
            "running_img": img_name,
            "running_repo_digests": [f"{img_name}@sha256:{ref.repository}_digest"],
        }

    assert len(service_names) == 21
    budget = TwoLevelBudget(max_logical_lookups=15)

    resolutions = scheduler.schedule_and_resolve(
        all_service_names=service_names,
        declared_services=declared,
        containers_by_service=containers,
        resolved_active_profiles=set(),
        service_runtime_metadata=runtime_meta,
        budget=budget,
        query_registries=True,
    )

    assert len(resolutions) == 21
    # All 15 unique keys were queried exactly once
    assert query_count == 15
    assert budget.logical_lookups_performed == 15
    # Zero services were budget exhausted!
    budget_exhausted_count = sum(
        1 for r in resolutions.values() if r.candidate_reason == ServiceCandidateReason.BUDGET_EXHAUSTED
    )
    assert budget_exhausted_count == 0

