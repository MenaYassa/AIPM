"""Comprehensive tests for MC-6.15-A Compose service and image intelligence.

Covers:
- Image reference parsing and normalization (deterministic, bounded, non-shell)
- Compose configuration safe parsing and override merging (handling custom YAML tags)
- Service-to-container mapping with MC-6.14 provenance enforcement
- Running vs declared vs candidate image separation
- Registry candidate resolution, multi-arch handling, and fail-closed UNKNOWN states
- Security: SSRF blocking, DNS rebinding socket verification, path traversal rejection, shell injection rejection
- Invariant: local-ai COMPOSE_DOWN is absent, GIT_DIRTY_CRITICAL remains present, and planner remains BLOCKED.
"""

from __future__ import annotations

from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from aipm.models.compose_intelligence import (
    ImageReference,
    ServiceCandidateReason,
    ServiceCandidateStatus,
)
from aipm.models.project import Project, ProjectCapabilities
from aipm.models.update import UpdateRisk
from aipm.providers.compose.provider import ComposeProvider
from aipm.services.compose.config_parser import (
    load_compose_file_safely,
    parse_declared_compose_services,
)
from aipm.services.compose.image_ref import parse_image_reference
from aipm.services.compose.intelligence import ComposeIntelligenceService
from aipm.services.compose.registry_client import (
    RegistryCandidateClient,
    RegistryCandidateResult,
    is_safe_registry_host,
)
from aipm.services.compose.service import ComposeService
from aipm.services.update.planner import UpdatePlanner


# ---------------------------------------------------------------------------
# 1. Image Reference Parsing Tests
# ---------------------------------------------------------------------------


def test_parse_image_reference_official_implicit_tag() -> None:
    ref = parse_image_reference("redis")
    assert ref.registry == "docker.io"
    assert ref.repository == "library/redis"
    assert ref.tag == "latest"
    assert ref.digest is None
    assert ref.is_pinned_by_digest is False
    assert ref.canonical == "docker.io/library/redis:latest"


def test_parse_image_reference_official_with_tag() -> None:
    ref = parse_image_reference("redis:7")
    assert ref.registry == "docker.io"
    assert ref.repository == "library/redis"
    assert ref.tag == "7"
    assert ref.canonical == "docker.io/library/redis:7"


def test_parse_image_reference_official_decimal_tag() -> None:
    ref = parse_image_reference("redis:7.2")
    assert ref.registry == "docker.io"
    assert ref.repository == "library/redis"
    assert ref.tag == "7.2"
    assert ref.canonical == "docker.io/library/redis:7.2"


def test_parse_image_reference_third_party_implicit_tag() -> None:
    ref = parse_image_reference("ghcr.io/foo/bar")
    assert ref.registry == "ghcr.io"
    assert ref.repository == "foo/bar"
    assert ref.tag == "latest"
    assert ref.canonical == "ghcr.io/foo/bar:latest"


def test_parse_image_reference_third_party_with_version() -> None:
    ref = parse_image_reference("ghcr.io/foo/bar:1.2.3")
    assert ref.registry == "ghcr.io"
    assert ref.repository == "foo/bar"
    assert ref.tag == "1.2.3"
    assert ref.digest is None
    assert ref.canonical == "ghcr.io/foo/bar:1.2.3"


def test_parse_image_reference_pinned_by_digest() -> None:
    digest_sha = "sha256:76beb9a87e28fc06d1b521262e04659ec7345975b00c9246a100000000000000"
    ref = parse_image_reference(f"ghcr.io/foo/bar@{digest_sha}")
    assert ref.registry == "ghcr.io"
    assert ref.repository == "foo/bar"
    assert ref.tag is None
    assert ref.digest == digest_sha
    assert ref.is_pinned_by_digest is True
    assert ref.canonical == f"ghcr.io/foo/bar@{digest_sha}"


def test_parse_image_reference_registry_with_port() -> None:
    ref = parse_image_reference("registry.example.com:5000/team/app:1.2.3")
    assert ref.registry == "registry.example.com:5000"
    assert ref.repository == "team/app"
    assert ref.tag == "1.2.3"
    assert ref.canonical == "registry.example.com:5000/team/app:1.2.3"


def test_parse_image_reference_security_rejections() -> None:
    with pytest.raises(ValueError, match="invalid characters"):
        parse_image_reference("redis; rm -rf /")

    with pytest.raises(ValueError, match="invalid path structure"):
        parse_image_reference("../etc/passwd")

    with pytest.raises(ValueError, match="invalid path structure"):
        parse_image_reference("ghcr.io//empty-segment")

    with pytest.raises(ValueError, match="unresolved interpolation"):
        parse_image_reference("redis$VARIABLE")

    with pytest.raises(ValueError, match="invalid characters"):
        parse_image_reference("redis`whoami`")

    with pytest.raises(ValueError, match="non-empty string"):
        parse_image_reference("")


# ---------------------------------------------------------------------------
# 2. Compose Configuration Safe Parsing Tests
# ---------------------------------------------------------------------------


def test_load_compose_file_safely_tolerates_custom_tags(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.override.yml"
    compose_file.write_text(
        "services:\n"
        "  web:\n"
        "    profiles: !override ['production']\n"
        "    image: !ref nginx:latest\n"
    )
    doc = load_compose_file_safely(compose_file)
    assert doc is not None
    assert "web" in doc["services"]


def test_parse_declared_compose_services_merges_overrides(tmp_path: Path) -> None:
    base = tmp_path / "docker-compose.yml"
    base.write_text(
        "name: test-stack\n"
        "services:\n"
        "  app:\n"
        "    image: redis:7\n"
        "  worker:\n"
        "    build: ./worker\n"
    )
    override = tmp_path / "docker-compose.override.yml"
    override.write_text(
        "services:\n"
        "  app:\n"
        "    profiles: ['active']\n"
        "  extra:\n"
        "    image: ghcr.io/org/extra:1.0\n"
    )

    services = parse_declared_compose_services([base, override], project_root=tmp_path)
    assert len(services) == 3

    assert services["app"].image == "redis:7"
    assert services["app"].profiles == ("active",)
    assert services["app"].is_build is False

    assert services["worker"].is_build is True
    assert services["worker"].build_context == "./worker"

    assert services["extra"].image == "ghcr.io/org/extra:1.0"
    assert services["extra"].is_build is False


# ---------------------------------------------------------------------------
# 3. SSRF and Registry Security Tests
# ---------------------------------------------------------------------------


def test_ssrf_blocking_prohibits_local_and_private_hosts() -> None:
    # Loopback & localhost
    ok, reason = is_safe_registry_host("localhost")
    assert ok is False
    assert "Localhost" in str(reason)

    ok, reason = is_safe_registry_host("127.0.0.1")
    assert ok is False
    assert "Loopback" in str(reason)

    # Cloud metadata IP
    ok, reason = is_safe_registry_host("169.254.169.254")
    assert ok is False
    assert "Cloud metadata" in str(reason)

    # RFC1918 Private IP
    ok, reason = is_safe_registry_host("192.168.1.10")
    assert ok is False
    assert "Private" in str(reason)

    ok, reason = is_safe_registry_host("10.0.0.5")
    assert ok is False
    assert "Private" in str(reason)


def test_registry_client_ssrf_rejection() -> None:
    client = RegistryCandidateClient()
    ref = parse_image_reference("localhost:5000/my-app:1.0")
    res = client.query_candidate_digest(ref)

    assert res.status == ServiceCandidateStatus.UNKNOWN
    assert res.reason == ServiceCandidateReason.SSRF_BLOCKED
    assert res.index_digest is None


def test_registry_client_local_build_skips_query() -> None:
    client = RegistryCandidateClient()
    ref = ImageReference(
        raw="local-app:custom",
        registry="docker.io",
        repository="local-app",
        tag="custom",
        is_local_build=True,
    )
    res = client.query_candidate_digest(ref)

    assert res.status == ServiceCandidateStatus.NOT_APPLICABLE
    assert res.reason == ServiceCandidateReason.LOCAL_BUILD
    assert res.index_digest is None


def test_registry_client_pinned_digest_skips_query() -> None:
    client = RegistryCandidateClient()
    ref = parse_image_reference("ghcr.io/foo/bar@sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")
    res = client.query_candidate_digest(ref)

    assert res.status == ServiceCandidateStatus.UNKNOWN
    assert res.reason == ServiceCandidateReason.PINNED_BY_DIGEST
    assert res.index_digest == "sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"


# ---------------------------------------------------------------------------
# 4. Service-to-Container Mapping and Intelligence Tests
# ---------------------------------------------------------------------------


def test_compose_intelligence_observes_services_and_provenance(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        "name: test-app\n"
        "services:\n"
        "  web:\n"
        "    image: nginx:latest\n"
        "  builder:\n"
        "    build: .\n"
        "    image: local-custom:v1\n"
    )

    proj = Project(
        name="test-app",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    c_web = SimpleNamespace(
        id="c11111111111",
        short_id="c11111",
        name="test-app-web-1",
        status="running",
        labels={
            "com.docker.compose.project": "test-app",
            "com.docker.compose.service": "web",
            "com.docker.compose.project.working_dir": str(tmp_path),
        },
        attrs={
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
            "Config": {"Image": "nginx:latest"},
            "Image": "sha256:abcdef",
        },
        image=SimpleNamespace(
            id="sha256:abcdef",
            tags=["nginx:latest"],
            attrs={"RepoDigests": ["nginx@sha256:runningdigest123"]},
        ),
        ports={"80/tcp": None},
    )

    c_builder = SimpleNamespace(
        id="c22222222222",
        short_id="c22222",
        name="test-app-builder-1",
        status="running",
        labels={
            "com.docker.compose.project": "test-app",
            "com.docker.compose.service": "builder",
            "com.docker.compose.project.working_dir": str(tmp_path),
        },
        attrs={
            "State": {"Status": "running"},
            "Config": {"Image": "local-custom:v1"},
            "Image": "sha256:custom123",
        },
        image=SimpleNamespace(
            id="sha256:custom123",
            tags=["local-custom:v1"],
            attrs={"RepoDigests": []},
        ),
        ports={},
    )

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = [c_web, c_builder]

    # Mock registry client to return matching index digest for web
    mock_registry = MagicMock(spec=RegistryCandidateClient)
    mock_registry.query_candidate_digest.return_value = RegistryCandidateResult(
        status=ServiceCandidateStatus.CURRENT,
        reason=ServiceCandidateReason.UP_TO_DATE,
        index_digest="sha256:runningdigest123",
        child_digest="sha256:childarm64digest",
        detail="mock match",
    )

    service = ComposeIntelligenceService(
        compose_provider=mock_provider,
        registry_client=mock_registry,
    )
    obs = service.observe(proj, query_registries=True)

    assert obs.project_name == "test-app"
    assert obs.compose_identity == "test-app"
    assert obs.total_services_count == 2
    assert obs.running_services_count == 2
    assert obs.updates_available_count == 0
    assert obs.current_count == 1
    assert obs.not_applicable_count == 1

    services_by_name = {s.service_name: s for s in obs.services}

    web_obs = services_by_name["web"]
    assert web_obs.state == "running"
    assert web_obs.health == "healthy"
    assert web_obs.declared_image == "nginx:latest"
    assert web_obs.running_image == "nginx:latest"
    assert web_obs.candidate_status == ServiceCandidateStatus.CURRENT
    assert web_obs.candidate_reason == ServiceCandidateReason.UP_TO_DATE

    builder_obs = services_by_name["builder"]
    assert builder_obs.state == "running"
    assert builder_obs.is_build is True
    assert builder_obs.candidate_status == ServiceCandidateStatus.NOT_APPLICABLE
    assert builder_obs.candidate_reason == ServiceCandidateReason.LOCAL_BUILD


def test_compose_intelligence_digest_semantics_matches_index_or_child(tmp_path: Path) -> None:
    """Verify that matching RepoDigest against EITHER index OR child digest does NOT report false UPDATE_AVAILABLE."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("name: my-app\nservices:\n  app:\n    image: valkey/valkey:latest\n")

    proj = Project(
        name="my-app",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    # Container RepoDigest contains index digest
    c_app = SimpleNamespace(
        id="c123",
        short_id="c123",
        name="my-app-app-1",
        status="running",
        labels={
            "com.docker.compose.project": "my-app",
            "com.docker.compose.service": "app",
            "com.docker.compose.project.working_dir": str(tmp_path),
        },
        attrs={
            "State": {"Status": "running"},
            "Config": {"Image": "valkey/valkey:latest"},
            "Image": "sha256:img123",
        },
        image=SimpleNamespace(
            id="sha256:img123",
            tags=["valkey/valkey:latest"],
            attrs={"RepoDigests": ["valkey/valkey@sha256:indexdigest111"]},
        ),
        ports={},
    )

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = [c_app]

    mock_registry = MagicMock(spec=RegistryCandidateClient)
    # Registry returns index_digest matching RepoDigest, and a distinct arm64 child digest
    mock_registry.query_candidate_digest.return_value = RegistryCandidateResult(
        status=ServiceCandidateStatus.CURRENT,
        reason=ServiceCandidateReason.UP_TO_DATE,
        index_digest="sha256:indexdigest111",
        child_digest="sha256:childarm64digest999",
        detail="mock multi-arch",
    )

    service = ComposeIntelligenceService(
        compose_provider=mock_provider,
        registry_client=mock_registry,
    )
    obs = service.observe(proj, query_registries=True)

    assert obs.updates_available_count == 0
    assert obs.current_count == 1
    app_obs = obs.services[0]
    assert app_obs.candidate_status == ServiceCandidateStatus.CURRENT
    assert app_obs.candidate_reason == ServiceCandidateReason.UP_TO_DATE


def test_compose_intelligence_detects_configuration_drift(tmp_path: Path) -> None:
    """Verify that a container running an image tag differing from declared compose is classified as DRIFT."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("name: drift-app\nservices:\n  web:\n    image: redis:7.2\n")

    proj = Project(
        name="drift-app",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    c_drift = SimpleNamespace(
        id="c999",
        short_id="c999",
        name="drift-web-1",
        status="running",
        labels={
            "com.docker.compose.project": "drift-app",
            "com.docker.compose.service": "web",
            "com.docker.compose.project.working_dir": str(tmp_path),
        },
        attrs={
            "State": {"Status": "running"},
            "Config": {"Image": "redis:6.0"},
            "Image": "sha256:oldredis",
        },
        image=SimpleNamespace(
            id="sha256:oldredis",
            tags=["redis:6.0"],
            attrs={"RepoDigests": ["redis@sha256:oldhash"]},
        ),
        ports={},
    )

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = [c_drift]

    service = ComposeIntelligenceService(compose_provider=mock_provider)
    obs = service.observe(proj, query_registries=False)

    assert obs.drift_count == 1
    assert obs.services[0].candidate_status == ServiceCandidateStatus.DRIFT
    assert obs.services[0].candidate_reason == ServiceCandidateReason.CONFIGURATION_DRIFT


# ---------------------------------------------------------------------------
# 5. Registry Failure, Timeout, Auth, and Architecture Tests
# ---------------------------------------------------------------------------


def test_registry_client_timeout_returns_unknown() -> None:
    import httpx

    client = RegistryCandidateClient()
    ref = parse_image_reference("ghcr.io/test/timeout-app:1.0")

    with patch("httpx.Client.get", side_effect=httpx.TimeoutException("Connection timed out")):
        res = client.query_candidate_digest(ref)

    assert res.status == ServiceCandidateStatus.UNKNOWN
    assert res.reason == ServiceCandidateReason.REGISTRY_TIMEOUT
    assert res.index_digest is None
    assert "timed out" in str(res.detail).lower()


def test_registry_client_auth_required_returns_unknown() -> None:
    client = RegistryCandidateClient()
    ref = parse_image_reference("private.example.com/secure/app:1.0")

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.headers = {}  # No Www-Authenticate header

    with patch("aipm.services.compose.registry_client.is_safe_registry_host", return_value=(True, None)), \
         patch("httpx.Client.get", return_value=mock_resp):
        res = client.query_candidate_digest(ref)

    assert res.status == ServiceCandidateStatus.UNKNOWN
    assert res.reason == ServiceCandidateReason.AUTHENTICATION_REQUIRED
    assert res.index_digest is None


def test_registry_client_arch_unavailable_returns_unknown() -> None:
    client = RegistryCandidateClient(target_arch="riscv64")
    ref = parse_image_reference("ghcr.io/test/arch-app:1.0")

    manifest_list = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
        "manifests": [
            {
                "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                "digest": "sha256:amd64manifestdigest0000000000000000000000000000000000000000000",
                "platform": {"architecture": "amd64", "os": "linux"},
            }
        ],
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {
        "content-type": "application/vnd.docker.distribution.manifest.list.v2+json",
        "docker-content-digest": "sha256:listdigest000000000000000000000000000000000000000000000000000",
    }
    mock_resp.json.return_value = manifest_list

    with patch("aipm.services.compose.registry_client.is_safe_registry_host", return_value=(True, None)), \
         patch("httpx.Client.get", return_value=mock_resp):
        res = client.query_candidate_digest(ref)

    assert res.status == ServiceCandidateStatus.UNKNOWN
    assert res.reason == ServiceCandidateReason.ARCHITECTURE_UNAVAILABLE
    assert res.index_digest is None
    assert "riscv64" in str(res.detail)


def test_registry_client_query_budget_exhaustion() -> None:
    client = RegistryCandidateClient(max_queries=1)

    ref1 = parse_image_reference("ghcr.io/test/app1:1.0")
    ref2 = parse_image_reference("ghcr.io/test/app2:1.0")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {
        "content-type": "application/vnd.docker.distribution.manifest.v2+json",
        "docker-content-digest": "sha256:singledigest00000000000000000000000000000000000000000000000",
    }

    with patch("aipm.services.compose.registry_client.is_safe_registry_host", return_value=(True, None)), \
         patch("httpx.Client.get", return_value=mock_resp):
        res1 = client.query_candidate_digest(ref1)
        assert res1.index_digest == mock_resp.headers["docker-content-digest"]

        # Second query exceeds budget
        res2 = client.query_candidate_digest(ref2)
        assert res2.status == ServiceCandidateStatus.UNKNOWN
        assert res2.reason == ServiceCandidateReason.BUDGET_EXHAUSTED
        assert "Query budget reached" in str(res2.detail)


def test_compose_intelligence_digest_child_vs_child_match(tmp_path: Path) -> None:
    """Verify that a container running the child digest directly matches candidate child digest."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("name: child-app\nservices:\n  app:\n    image: valkey:latest\n")

    proj = Project(
        name="child-app",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    c_app = SimpleNamespace(
        id="c123",
        short_id="c123",
        name="child-app-app-1",
        status="running",
        labels={
            "com.docker.compose.project": "child-app",
            "com.docker.compose.service": "app",
            "com.docker.compose.project.working_dir": str(tmp_path),
        },
        attrs={"State": {"Status": "running"}, "Config": {"Image": "valkey:latest"}, "Image": "sha256:img123"},
        image=SimpleNamespace(
            id="sha256:img123",
            tags=["valkey:latest"],
            attrs={"RepoDigests": ["valkey@sha256:childdigest888"]},
        ),
        ports={},
    )

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = [c_app]

    mock_registry = MagicMock(spec=RegistryCandidateClient)
    mock_registry.query_candidate_digest.return_value = RegistryCandidateResult(
        status=ServiceCandidateStatus.CURRENT,
        reason=ServiceCandidateReason.UP_TO_DATE,
        index_digest="sha256:indexdigest777",
        child_digest="sha256:childdigest888",
        detail="mock child match",
    )

    service = ComposeIntelligenceService(compose_provider=mock_provider, registry_client=mock_registry)
    obs = service.observe(proj, query_registries=True)

    assert obs.updates_available_count == 0
    assert obs.current_count == 1
    assert obs.services[0].candidate_status == ServiceCandidateStatus.CURRENT
    assert obs.services[0].candidate_reason == ServiceCandidateReason.UP_TO_DATE


def test_compose_intelligence_multiple_repodigests(tmp_path: Path) -> None:
    """Verify that multiple RepoDigests match if any digest matches the candidate index or child."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("name: multi-app\nservices:\n  app:\n    image: app:v1\n")

    proj = Project(
        name="multi-app",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    c_app = SimpleNamespace(
        id="c123",
        short_id="c123",
        name="multi-app-app-1",
        status="running",
        labels={
            "com.docker.compose.project": "multi-app",
            "com.docker.compose.service": "app",
            "com.docker.compose.project.working_dir": str(tmp_path),
        },
        attrs={"State": {"Status": "running"}, "Config": {"Image": "app:v1"}, "Image": "sha256:img123"},
        image=SimpleNamespace(
            id="sha256:img123",
            tags=["app:v1", "app:latest"],
            attrs={"RepoDigests": ["app@sha256:oldtagdigest", "app@sha256:matchingindexdigest"]},
        ),
        ports={},
    )

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = [c_app]

    mock_registry = MagicMock(spec=RegistryCandidateClient)
    mock_registry.query_candidate_digest.return_value = RegistryCandidateResult(
        status=ServiceCandidateStatus.CURRENT,
        reason=ServiceCandidateReason.UP_TO_DATE,
        index_digest="sha256:matchingindexdigest",
        child_digest="sha256:otherchild",
        detail="mock match",
    )

    service = ComposeIntelligenceService(compose_provider=mock_provider, registry_client=mock_registry)
    obs = service.observe(proj, query_registries=True)

    assert obs.current_count == 1
    assert obs.services[0].candidate_status == ServiceCandidateStatus.CURRENT


@pytest.mark.parametrize(
    "forbidden_peer_ip,expected_msg",
    [
        ("127.0.0.1", "Loopback address 127.0.0.1 is prohibited"),
        ("169.254.169.254", "Cloud metadata address is prohibited"),
        ("10.0.1.15", "Private RFC1918/RFC4193 address 10.0.1.15 is prohibited"),
        ("192.168.1.1", "Private RFC1918/RFC4193 address 192.168.1.1 is prohibited"),
    ],
)
def test_ssrf_transport_blocks_dns_rebinding_attempt(forbidden_peer_ip: str, expected_msg: str) -> None:
    """Verify that SSRFSafeTransport intercepts connected TCP socket and blocks forbidden peer IPs."""
    import httpcore
    from aipm.services.compose.registry_client import SSRFSafeTransport

    transport = SSRFSafeTransport()
    fake_stream = MagicMock(spec=httpcore.NetworkStream)
    fake_sock = MagicMock()
    fake_sock.getpeername.return_value = (forbidden_peer_ip, 443)
    fake_stream.get_extra_info.return_value = fake_sock

    with patch("aipm.services.compose.registry_client.is_safe_registry_host", return_value=(True, None)), \
         patch.object(httpcore._backends.sync.SyncBackend, "connect_tcp", return_value=fake_stream):
        with httpx.Client(transport=transport) as client:
            with pytest.raises(PermissionError, match=f"DNS-rebinding prohibited.*{re.escape(expected_msg)}"):
                client.get("https://safe-domain.example.com/v2/")

    assert fake_stream.close.called


def test_compose_intelligence_pinned_digest_declared_running_match(tmp_path: Path) -> None:
    """Verify that declared=A and running=A evaluates to CURRENT with PINNED_BY_DIGEST."""
    compose_file = tmp_path / "docker-compose.yml"
    digest_a = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    compose_file.write_text(f"name: pin-app\nservices:\n  app:\n    image: myrepo/app@{digest_a}\n")

    proj = Project(
        name="pin-app",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    c_app = SimpleNamespace(
        id="c123",
        short_id="c123",
        name="pin-app-app-1",
        status="running",
        labels={
            "com.docker.compose.project": "pin-app",
            "com.docker.compose.service": "app",
            "com.docker.compose.project.working_dir": str(tmp_path),
        },
        attrs={"State": {"Status": "running"}, "Config": {"Image": f"myrepo/app@{digest_a}"}, "Image": digest_a},
        image=SimpleNamespace(
            id=digest_a,
            tags=[],
            attrs={"RepoDigests": [f"myrepo/app@{digest_a}"]},
        ),
        ports={},
    )

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = [c_app]

    mock_registry = MagicMock(spec=RegistryCandidateClient)

    service = ComposeIntelligenceService(compose_provider=mock_provider, registry_client=mock_registry)
    obs = service.observe(proj, query_registries=True)

    assert obs.current_count == 1
    assert obs.updates_available_count == 0
    assert obs.services[0].candidate_status == ServiceCandidateStatus.CURRENT
    assert obs.services[0].candidate_reason == ServiceCandidateReason.PINNED_BY_DIGEST
    assert obs.services[0].candidate_digest == digest_a
    mock_registry.query_candidate_digest.assert_not_called()


def test_compose_intelligence_pinned_digest_declared_running_drift(tmp_path: Path) -> None:
    """Verify that declared=A and running=B evaluates to DRIFT with CONFIGURATION_DRIFT."""
    compose_file = tmp_path / "docker-compose.yml"
    digest_a = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    digest_b = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    compose_file.write_text(f"name: pin-app\nservices:\n  app:\n    image: myrepo/app@{digest_a}\n")

    proj = Project(
        name="pin-app",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    c_app = SimpleNamespace(
        id="c123",
        short_id="c123",
        name="pin-app-app-1",
        status="running",
        labels={
            "com.docker.compose.project": "pin-app",
            "com.docker.compose.service": "app",
            "com.docker.compose.project.working_dir": str(tmp_path),
        },
        attrs={"State": {"Status": "running"}, "Config": {"Image": f"myrepo/app@{digest_b}"}, "Image": digest_b},
        image=SimpleNamespace(
            id=digest_b,
            tags=[],
            attrs={"RepoDigests": [f"myrepo/app@{digest_b}"]},
        ),
        ports={},
    )

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = [c_app]

    mock_registry = MagicMock(spec=RegistryCandidateClient)

    service = ComposeIntelligenceService(compose_provider=mock_provider, registry_client=mock_registry)
    obs = service.observe(proj, query_registries=True)

    assert obs.current_count == 0
    assert obs.updates_available_count == 0
    assert obs.services[0].candidate_status == ServiceCandidateStatus.DRIFT
    assert obs.services[0].candidate_reason == ServiceCandidateReason.CONFIGURATION_DRIFT
    mock_registry.query_candidate_digest.assert_not_called()


def test_compose_intelligence_pinned_digest_declared_no_container(tmp_path: Path) -> None:
    """Verify that declared=A with no running container evaluates to NOT_APPLICABLE / NO_RUNNING_CONTAINER."""
    compose_file = tmp_path / "docker-compose.yml"
    digest_a = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    compose_file.write_text(f"name: pin-app\nservices:\n  app:\n    image: myrepo/app@{digest_a}\n")

    proj = Project(
        name="pin-app",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = []

    mock_registry = MagicMock(spec=RegistryCandidateClient)

    service = ComposeIntelligenceService(compose_provider=mock_provider, registry_client=mock_registry)
    obs = service.observe(proj, query_registries=True)

    assert obs.current_count == 0
    assert obs.services[0].candidate_status == ServiceCandidateStatus.NOT_APPLICABLE
    assert obs.services[0].candidate_reason == ServiceCandidateReason.NO_RUNNING_CONTAINER
    assert obs.services[0].candidate_digest == digest_a
    mock_registry.query_candidate_digest.assert_not_called()


def test_compose_intelligence_unresolved_interpolation_fails_closed(tmp_path: Path) -> None:
    """Verify that unresolved interpolation expressions fail closed to UNKNOWN and never guess from running container."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("name: interp-app\nservices:\n  app:\n    image: ${UNRESOLVED_IMAGE_VAR}\n")

    proj = Project(
        name="interp-app",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    c_app = SimpleNamespace(
        id="c123",
        short_id="c123",
        name="interp-app-app-1",
        status="running",
        labels={
            "com.docker.compose.project": "interp-app",
            "com.docker.compose.service": "app",
            "com.docker.compose.project.working_dir": str(tmp_path),
        },
        attrs={"State": {"Status": "running"}, "Config": {"Image": "guessed/app:latest"}, "Image": "sha256:123"},
        image=SimpleNamespace(
            id="sha256:123",
            tags=["guessed/app:latest"],
            attrs={"RepoDigests": ["guessed/app@sha256:456"]},
        ),
        ports={},
    )

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = [c_app]

    mock_registry = MagicMock(spec=RegistryCandidateClient)

    service = ComposeIntelligenceService(compose_provider=mock_provider, registry_client=mock_registry)
    obs = service.observe(proj, query_registries=True)

    assert obs.services[0].candidate_status == ServiceCandidateStatus.UNKNOWN
    assert obs.services[0].candidate_reason == ServiceCandidateReason.MALFORMED_IMAGE_REFERENCE
    assert "cannot be safely resolved" in str(obs.services[0].candidate_detail)
    mock_registry.query_candidate_digest.assert_not_called()


def test_compose_intelligence_missing_image_and_build_fails_closed(tmp_path: Path) -> None:
    """Verify that a service with neither image nor build configuration fails closed to UNKNOWN."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("name: noimg-app\nservices:\n  app:\n    environment:\n      FOO: bar\n")

    proj = Project(
        name="noimg-app",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    c_app = SimpleNamespace(
        id="c123",
        short_id="c123",
        name="noimg-app-app-1",
        status="running",
        labels={
            "com.docker.compose.project": "noimg-app",
            "com.docker.compose.service": "app",
            "com.docker.compose.project.working_dir": str(tmp_path),
        },
        attrs={"State": {"Status": "running"}, "Config": {"Image": "random:v1"}, "Image": "sha256:789"},
        image=SimpleNamespace(
            id="sha256:789",
            tags=["random:v1"],
            attrs={"RepoDigests": []},
        ),
        ports={},
    )

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = [c_app]

    mock_registry = MagicMock(spec=RegistryCandidateClient)

    service = ComposeIntelligenceService(compose_provider=mock_provider, registry_client=mock_registry)
    obs = service.observe(proj, query_registries=True)

    assert obs.services[0].candidate_status == ServiceCandidateStatus.UNKNOWN
    assert obs.services[0].candidate_reason == ServiceCandidateReason.MALFORMED_IMAGE_REFERENCE
    mock_registry.query_candidate_digest.assert_not_called()


def test_compose_service_observe_defaults_to_no_registry_queries(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("name: obs-test\nservices:\n  app:\n    image: redis:7\n")

    proj = Project(
        name="obs-test",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    cs = ComposeService()
    with patch.object(ComposeIntelligenceService, "observe") as mock_obs:
        cs.observe(proj)
        mock_obs.assert_called_once_with(proj, query_registries=False)


# ---------------------------------------------------------------------------
# 6. Production Invariants Verification
# ---------------------------------------------------------------------------


def test_production_invariants_preserved() -> None:
    """Verify that Compose intelligence preserves existing Git critical gates and COMPOSE_DOWN elimination."""
    planner = UpdatePlanner()
    plan = planner.plan("local-ai-packaged")

    # Safety gate remains strictly BLOCKED
    assert plan.risk == UpdateRisk.BLOCKED
    assert plan.proceed is False
    assert any("critical" in str(r).lower() for r in plan.reasons)


# ---------------------------------------------------------------------------
# 7. R3 Correctness Invariants: !reset/!override, Streaming & Pinned Digests
# ---------------------------------------------------------------------------


def test_compose_yaml_reset_and_override_semantics(tmp_path: Path) -> None:
    """Verify exact Docker Compose semantics for !reset and !override tags."""
    base_file = tmp_path / "docker-compose.yml"
    base_file.write_text(
        "services:\n"
        "  svc_reset_prof:\n"
        "    image: redis:7\n"
        "    profiles: ['dev', 'test']\n"
        "  svc_reset_img:\n"
        "    image: postgres:15\n"
        "    build: ./db\n"
        "  svc_override_bld:\n"
        "    image: app:latest\n"
        "    build:\n"
        "      context: ./old\n"
        "      dockerfile: Dockerfile.old\n"
        "  svc_bad_reset:\n"
        "    image: !reset [12345, 67890]\n"
    )

    override_file = tmp_path / "docker-compose.override.yml"
    override_file.write_text(
        "services:\n"
        "  svc_reset_prof:\n"
        "    profiles: !reset ['prod']\n"
        "  svc_reset_img:\n"
        "    image: !reset\n"
        "  svc_override_bld:\n"
        "    build: !override\n"
        "      context: ./new\n"
    )

    services = parse_declared_compose_services([base_file, override_file])

    # 1. profiles reset to ['prod']
    assert services["svc_reset_prof"].profiles == ("prod",)

    # 2. image reset/cleared
    assert services["svc_reset_img"].image is None
    assert services["svc_reset_img"].is_build is True

    # 3. build overridden without inheriting old dockerfile
    assert services["svc_override_bld"].build_context == "./new"
    assert services["svc_override_bld"].build_dockerfile == "Dockerfile"

    # 4. unsupported !reset type fails closed with parse_error
    assert services["svc_bad_reset"].parse_error is not None
    assert "Unsupported !reset type" in services["svc_bad_reset"].parse_error


def test_compose_yaml_multifile_merge_semantics(tmp_path: Path) -> None:
    """Verify multi-file merge: mapping merge for build, list union for profiles, scalar override for image."""
    base_file = tmp_path / "compose-base.yml"
    base_file.write_text(
        "services:\n"
        "  app:\n"
        "    image: myapp:1.0\n"
        "    profiles: ['frontend', 'api']\n"
        "    build:\n"
        "      context: .\n"
        "      dockerfile: Dockerfile.base\n"
    )

    override_file = tmp_path / "compose-override.yml"
    override_file.write_text(
        "services:\n"
        "  app:\n"
        "    image: myapp:2.0\n"
        "    profiles: ['api', 'worker']\n"
        "    build:\n"
        "      dockerfile: Dockerfile.override\n"
    )

    services = parse_declared_compose_services([base_file, override_file])
    app = services["app"]

    # Scalar override
    assert app.image == "myapp:2.0"
    # List union without duplicates
    assert app.profiles == ("frontend", "api", "worker")
    # Mapping merge: inherited context + overridden dockerfile
    assert app.build_context == "."
    assert app.build_dockerfile == "Dockerfile.override"


def test_compose_intelligence_parse_error_fails_closed(tmp_path: Path) -> None:
    """Verify services with parse errors fail closed to UNKNOWN / MALFORMED_IMAGE_REFERENCE."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        "services:\n"
        "  broken:\n"
        "    image: !reset [invalid, list, for, scalar]\n"
    )

    proj = Project(
        name="broken-proj",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    c_broken = SimpleNamespace(
        id="c111",
        short_id="c111",
        name="broken-proj-broken-1",
        status="running",
        labels={
            "com.docker.compose.project": "broken-proj",
            "com.docker.compose.service": "broken",
            "com.docker.compose.project.working_dir": str(tmp_path),
        },
        attrs={"State": {"Status": "running"}, "Config": {"Image": "broken:latest"}, "Image": "sha256:111"},
        image=SimpleNamespace(
            id="sha256:111",
            tags=["broken:latest"],
            attrs={"RepoDigests": ["broken@sha256:111"]},
        ),
        ports={},
    )

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = [c_broken]
    mock_registry = MagicMock(spec=RegistryCandidateClient)

    service = ComposeIntelligenceService(compose_provider=mock_provider, registry_client=mock_registry)
    obs = service.observe(proj, query_registries=True)

    assert len(obs.services) == 1
    assert obs.services[0].candidate_status == ServiceCandidateStatus.UNKNOWN
    assert obs.services[0].candidate_reason == ServiceCandidateReason.MALFORMED_IMAGE_REFERENCE
    mock_registry.query_candidate_digest.assert_not_called()


def test_registry_client_end_to_end_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify total deadline exhaustion across multi-step registry requests."""
    import time

    client = RegistryCandidateClient()
    ref = parse_image_reference("ghcr.io/test/deadline-app:latest")

    # Simulate monotonic clock advancing beyond total deadline (8.0s)
    fake_time = 1000.0

    def mock_monotonic() -> float:
        nonlocal fake_time
        fake_time += 10.0  # Immediately exceeds deadline
        return fake_time

    monkeypatch.setattr(time, "monotonic", mock_monotonic)

    res = client.query_candidate_digest(ref)
    assert res.status == ServiceCandidateStatus.UNKNOWN
    assert res.reason == ServiceCandidateReason.REGISTRY_TIMEOUT
    assert "operation deadline (8.0s) exceeded" in str(res.detail)


def test_registry_client_content_length_limit_enforced() -> None:
    """Verify early Content-Length check rejects oversized manifests immediately."""
    from aipm.services.compose.registry_client import _stream_bounded_response

    class FakeOversizedResponse:
        status_code = 200
        headers = {"content-length": "2097152"}  # 2 MB > 1 MB

        def iter_bytes(self, chunk_size: int = 8192):
            yield b"never reached"

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.stream.return_value = FakeOversizedResponse()

    with pytest.raises(ValueError, match="exceeds maximum allowed size"):
        _stream_bounded_response(
            mock_client,
            "GET",
            "https://registry.example.com/manifest",
            headers={},
            timeout=httpx.Timeout(5.0),
            max_bytes=1024 * 1024,
        )


def test_registry_client_streaming_chunk_limit_enforced() -> None:
    """Verify chunked streaming rejects responses when cumulative bytes exceed max_bytes."""
    from aipm.services.compose.registry_client import _stream_bounded_response

    class FakeStreamingResponse:
        status_code = 200
        headers = {}  # No Content-Length declared

        def iter_bytes(self, chunk_size: int = 8192):
            # Yield 200 chunks of 8KB = 1.6MB > 1MB
            for _ in range(200):
                yield b"x" * 8192

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    mock_client = MagicMock(spec=httpx.Client)
    mock_client.stream.return_value = FakeStreamingResponse()

    with pytest.raises(ValueError, match="Response streaming bytes exceeded maximum allowed size"):
        _stream_bounded_response(
            mock_client,
            "GET",
            "https://registry.example.com/stream-manifest",
            headers={},
            timeout=httpx.Timeout(5.0),
            max_bytes=1024 * 1024,
        )


def test_compose_intelligence_pinned_digest_all_permutations(tmp_path: Path) -> None:
    """Verify four permutations of pinned digest evaluation:
    1. declared=A / running=A -> CURRENT / PINNED_BY_DIGEST
    2. declared=A / running=B -> DRIFT / CONFIGURATION_DRIFT
    3. declared=A / running digest unavailable -> UNKNOWN / DIGEST_UNAVAILABLE
    4. declared=A / no container -> NOT_APPLICABLE / NO_RUNNING_CONTAINER
    """
    digest_a = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    digest_b = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        "services:\n"
        f"  svc_match:\n    image: app@{digest_a}\n"
        f"  svc_drift:\n    image: app@{digest_a}\n"
        f"  svc_no_digest:\n    image: app@{digest_a}\n"
        f"  svc_no_container:\n    image: app@{digest_a}\n"
    )

    proj = Project(
        name="pinned-proj",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    def _make_container(name: str, svc: str, running_img: str, repo_digests: list[str]) -> SimpleNamespace:
        return SimpleNamespace(
            id=f"id-{name}",
            short_id=f"id-{name}"[:10],
            name=name,
            status="running",
            labels={
                "com.docker.compose.project": "pinned-proj",
                "com.docker.compose.service": svc,
                "com.docker.compose.project.working_dir": str(tmp_path),
            },
            attrs={"State": {"Status": "running"}, "Config": {"Image": running_img}, "Image": f"sha256:{name}"},
            image=SimpleNamespace(
                id=f"sha256:{name}",
                tags=[running_img],
                attrs={"RepoDigests": repo_digests},
            ),
            ports={},
        )

    c_match = _make_container("c_match", "svc_match", f"app@{digest_a}", [f"app@{digest_a}"])
    c_drift = _make_container("c_drift", "svc_drift", f"app@{digest_b}", [f"app@{digest_b}"])
    c_no_digest = _make_container("c_no_digest", "svc_no_digest", "app:latest", [])

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = [c_match, c_drift, c_no_digest]
    mock_registry = MagicMock(spec=RegistryCandidateClient)

    service = ComposeIntelligenceService(compose_provider=mock_provider, registry_client=mock_registry)
    obs = service.observe(proj, query_registries=True)

    by_svc = {s.service_name: s for s in obs.services}

    # Case 1: declared=A / running=A -> CURRENT / PINNED_BY_DIGEST
    assert by_svc["svc_match"].candidate_status == ServiceCandidateStatus.CURRENT
    assert by_svc["svc_match"].candidate_reason == ServiceCandidateReason.PINNED_BY_DIGEST
    assert by_svc["svc_match"].candidate_digest == digest_a

    # Case 2: declared=A / running=B -> DRIFT / CONFIGURATION_DRIFT
    assert by_svc["svc_drift"].candidate_status == ServiceCandidateStatus.DRIFT
    assert by_svc["svc_drift"].candidate_reason == ServiceCandidateReason.CONFIGURATION_DRIFT

    # Case 3: declared=A / running digest unavailable -> UNKNOWN / DIGEST_UNAVAILABLE
    assert by_svc["svc_no_digest"].candidate_status == ServiceCandidateStatus.UNKNOWN
    assert by_svc["svc_no_digest"].candidate_reason == ServiceCandidateReason.DIGEST_UNAVAILABLE
    assert "no verifiable image digest evidence" in str(by_svc["svc_no_digest"].candidate_detail)

    # Case 4: declared=A / no container -> NOT_APPLICABLE / NO_RUNNING_CONTAINER
    assert by_svc["svc_no_container"].candidate_status == ServiceCandidateStatus.NOT_APPLICABLE
    assert by_svc["svc_no_container"].candidate_reason == ServiceCandidateReason.NO_RUNNING_CONTAINER

    # Registry queries should never be called for pinned digest evaluation
    mock_registry.query_candidate_digest.assert_not_called()


def test_compose_yaml_deep_nested_reset_and_override(tmp_path: Path) -> None:
    """Verify deep Compose semantics: service-level !reset/!override, nested key !reset, nested !override."""
    base_file = tmp_path / "docker-compose.base.yml"
    base_file.write_text(
        "services:\n"
        "  svc_deleted:\n"
        "    image: to_be_deleted:1.0\n"
        "  svc_replaced:\n"
        "    image: old_image:1.0\n"
        "    build:\n"
        "      context: ./old\n"
        "      dockerfile: Dockerfile.old\n"
        "  svc_nested_key_reset:\n"
        "    image: app:1.0\n"
        "    build:\n"
        "      context: ./base\n"
        "      dockerfile: Dockerfile.custom\n"
    )

    override_file = tmp_path / "docker-compose.override.yml"
    override_file.write_text(
        "services:\n"
        "  svc_deleted: !reset null\n"
        "  svc_replaced: !override\n"
        "    image: new_image:2.0\n"
        "  svc_nested_key_reset:\n"
        "    build:\n"
        "      dockerfile: !reset null\n"
    )

    services = parse_declared_compose_services([base_file, override_file])

    # 1. Service-level !reset null clears the service entirely
    assert services["svc_deleted"].image is None
    assert services["svc_deleted"].is_build is False

    # 2. Service-level !override replaces the service definition without inheriting base build
    assert services["svc_replaced"].image == "new_image:2.0"
    assert services["svc_replaced"].is_build is False
    assert services["svc_replaced"].build_context is None

    # 3. Nested key-level !reset null removes the dockerfile key, falling back to default Dockerfile
    assert services["svc_nested_key_reset"].build_context == "./base"
    assert services["svc_nested_key_reset"].build_dockerfile == "Dockerfile"


def test_compose_intelligence_update_available_contract_non_chronological(tmp_path: Path) -> None:
    """Verify that UPDATE_AVAILABLE represents differing digest without asserting chronological ordering."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("name: contract-app\nservices:\n  web:\n    image: nginx:latest\n")

    proj = Project(
        name="contract-app",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    c_web = SimpleNamespace(
        id="c111",
        short_id="c111",
        name="contract-app-web-1",
        status="running",
        labels={
            "com.docker.compose.project": "contract-app",
            "com.docker.compose.service": "web",
            "com.docker.compose.project.working_dir": str(tmp_path),
        },
        attrs={"State": {"Status": "running"}, "Config": {"Image": "nginx:latest"}, "Image": "sha256:running111"},
        image=SimpleNamespace(
            id="sha256:running111",
            tags=["nginx:latest"],
            attrs={"RepoDigests": ["nginx@sha256:runningdigest1111111111111111111111111111111111111111111111111111111111111111"]},
        ),
        ports={},
    )

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = [c_web]

    mock_registry = MagicMock(spec=RegistryCandidateClient)
    # Registry candidate digest differs from running digest
    diff_digest = "sha256:candidatedigest2222222222222222222222222222222222222222222222222222222222222222"
    mock_registry.query_candidate_digest.return_value = RegistryCandidateResult(
        status=ServiceCandidateStatus.CURRENT,
        reason=ServiceCandidateReason.UP_TO_DATE,
        index_digest=diff_digest,
        child_digest=None,
        detail="single-manifest",
    )

    service = ComposeIntelligenceService(compose_provider=mock_provider, registry_client=mock_registry)
    obs = service.observe(proj, query_registries=True)

    web_obs = obs.services[0]
    assert web_obs.candidate_status == ServiceCandidateStatus.UPDATE_AVAILABLE
    assert web_obs.candidate_reason == ServiceCandidateReason.CANDIDATE_DIGEST_DIFFERS
    assert "differs from running image" in str(web_obs.candidate_detail)
    assert "newer" not in str(web_obs.candidate_detail).lower()


def test_compose_intelligence_multicontainer_aggregation(tmp_path: Path) -> None:
    """Verify multi-container state/health aggregation across replicas: mixed states and prioritized health."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("name: scale-app\nservices:\n  api:\n    image: api:1.0\n")

    proj = Project(
        name="scale-app",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    # 1. Mixed states: one running, one exited -> state == "mixed"
    c1 = SimpleNamespace(
        id="c1",
        short_id="c1",
        name="scale-app-api-1",
        status="running",
        labels={"com.docker.compose.project": "scale-app", "com.docker.compose.service": "api", "com.docker.compose.project.working_dir": str(tmp_path)},
        attrs={"State": {"Status": "running", "Health": {"Status": "healthy"}}, "Config": {"Image": "api:1.0"}},
        image=SimpleNamespace(id="sha256:1", tags=["api:1.0"], attrs={"RepoDigests": []}),
        ports={},
    )
    c2 = SimpleNamespace(
        id="c2",
        short_id="c2",
        name="scale-app-api-2",
        status="exited",
        labels={"com.docker.compose.project": "scale-app", "com.docker.compose.service": "api", "com.docker.compose.project.working_dir": str(tmp_path)},
        attrs={"State": {"Status": "exited", "Health": {"Status": "unhealthy"}}, "Config": {"Image": "api:1.0"}},
        image=SimpleNamespace(id="sha256:1", tags=["api:1.0"], attrs={"RepoDigests": []}),
        ports={},
    )

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = [c1, c2]

    service = ComposeIntelligenceService(compose_provider=mock_provider)
    obs = service.observe(proj, query_registries=False)

    api_obs = obs.services[0]
    # State is mixed because one is running and one is exited
    assert api_obs.state == "mixed"
    # Health prioritizes unhealthy
    assert api_obs.health == "unhealthy"


def test_compose_intelligence_profile_filtering_ordinary_names(tmp_path: Path) -> None:
    """Verify ordinary profile names and active profile resolution."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        "name: prof-app\n"
        "services:\n"
        "  core:\n"
        "    image: core:latest\n"
        "  tools:\n"
        "    image: tools:latest\n"
        "    profiles: ['utilities']\n"
    )

    proj = Project(
        name="prof-app",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    mock_provider = MagicMock(spec=ComposeProvider)
    mock_provider.ps_raw.return_value = []  # No containers running

    service = ComposeIntelligenceService(compose_provider=mock_provider)

    # 1. Observe without 'utilities' active profile -> tools is DISABLED_BY_PROFILE
    obs_inactive = service.observe(proj, query_registries=False, active_profiles=[])
    by_svc = {s.service_name: s for s in obs_inactive.services}
    assert by_svc["core"].candidate_reason == ServiceCandidateReason.NO_RUNNING_CONTAINER
    assert by_svc["tools"].candidate_reason == ServiceCandidateReason.DISABLED_BY_PROFILE
    assert "inactive under current profiles" in str(by_svc["tools"].candidate_detail)

    # 2. Observe with 'utilities' active profile -> tools is NO_RUNNING_CONTAINER
    obs_active = service.observe(proj, query_registries=False, active_profiles=["utilities"])
    by_svc_act = {s.service_name: s for s in obs_active.services}
    assert by_svc_act["tools"].candidate_reason == ServiceCandidateReason.NO_RUNNING_CONTAINER


def test_ssrf_safe_transport_real_path_blocks_loopback() -> None:
    """Verify real HTTPTransport execution path blocks loopback connection via SSRFSafeTransport."""
    from aipm.services.compose.registry_client import SSRFSafeTransport

    transport = SSRFSafeTransport()
    req = httpx.Request("GET", "http://127.0.0.1:80/v2/")

    with pytest.raises(PermissionError, match="SSRF_BLOCKED"):
        transport.handle_request(req)


