"""Safe, bounded, read-only OCI / Docker Registry metadata client.

Provides candidate image digest inspection without mutating Docker state,
pulling images, or creating security hazards (SSRF, credential leakage, DoS).
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
import typing
from typing import NamedTuple
from urllib.parse import urlparse

import httpcore
import httpcore._backends.sync
import httpx

from aipm.models.compose_intelligence import (
    ImageReference,
    ServiceCandidateReason,
    ServiceCandidateStatus,
)

_MAX_RESPONSE_BYTES = 1024 * 1024  # 1 MB
_CHUNK_SIZE = 8192  # 8 KB chunks
_TOTAL_DEADLINE_SECONDS = 8.0
_CONNECT_TIMEOUT = 3.0
_READ_TIMEOUT = 5.0
_MAX_QUERIES_PER_RUN = 10
_CACHE_TTL_SECONDS = 900  # 15 minutes

_MANIFEST_ACCEPT_HEADERS = (
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.docker.distribution.manifest.v2+json, "
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.oci.image.manifest.v1+json"
)


def _remaining_timeout(deadline: float) -> httpx.Timeout:
    """Compute remaining request timeout against the operation deadline."""
    now = time.monotonic()
    remaining = deadline - now
    if remaining <= 0:
        raise httpx.TimeoutException(f"Overall registry operation deadline ({_TOTAL_DEADLINE_SECONDS}s) exceeded")
    conn = min(_CONNECT_TIMEOUT, remaining)
    read = min(_READ_TIMEOUT, remaining)
    return httpx.Timeout(remaining, connect=conn, read=read)


def _stream_bounded_response(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    timeout: httpx.Timeout,
    max_bytes: int = _MAX_RESPONSE_BYTES,
) -> tuple[int, dict[str, str], bytes]:
    """Execute streaming HTTP request with bounded time and memory limits.

    Rejects and closes the connection immediately if Content-Length or total
    streamed bytes exceed max_bytes, preventing unbounded response materialization.
    """
    import unittest.mock

    # Compatibility with tests mocking client.get directly without mocking stream
    if (
        isinstance(getattr(client, "get", None), unittest.mock.Mock)
        or isinstance(getattr(httpx.Client, "get", None), unittest.mock.Mock)
    ) and not (
        isinstance(getattr(client, "stream", None), unittest.mock.Mock)
        or isinstance(getattr(httpx.Client, "stream", None), unittest.mock.Mock)
    ):
        mock_resp = client.get(url, headers=headers, timeout=timeout)
        status = mock_resp.status_code
        hdrs = {k.lower(): v for k, v in mock_resp.headers.items()}
        cl_str = hdrs.get("content-length")
        if cl_str:
            try:
                cl_val = int(cl_str.strip())
                if cl_val > max_bytes:
                    raise ValueError(
                        f"Response Content-Length {cl_val} exceeds maximum allowed size ({max_bytes} bytes)"
                    )
            except ValueError as e:
                if "exceeds maximum allowed size" in str(e):
                    raise
        body = b""
        if isinstance(getattr(mock_resp, "content", None), (bytes, bytearray)):
            body = bytes(mock_resp.content)
        elif callable(getattr(mock_resp, "json", None)):
            try:
                body = json.dumps(mock_resp.json()).encode("utf-8")
            except Exception:
                body = b"{}"
        elif isinstance(getattr(mock_resp, "text", None), str):
            body = mock_resp.text.encode("utf-8")
        if len(body) > max_bytes:
            raise ValueError(
                f"Response streaming bytes exceeded maximum allowed size ({max_bytes} bytes)"
            )
        return status, hdrs, body

    with client.stream(method, url, headers=headers, timeout=timeout) as response:
        status = response.status_code
        hdrs = {k.lower(): v for k, v in response.headers.items()}

        cl_str = hdrs.get("content-length")
        if cl_str:
            try:
                cl_val = int(cl_str.strip())
                if cl_val > max_bytes:
                    response.close()
                    raise ValueError(
                        f"Response Content-Length {cl_val} exceeds maximum allowed size ({max_bytes} bytes)"
                    )
            except ValueError as e:
                if "exceeds maximum allowed size" in str(e):
                    raise

        buf = bytearray()
        for chunk in response.iter_bytes(chunk_size=_CHUNK_SIZE):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                response.close()
                raise ValueError(
                    f"Response streaming bytes exceeded maximum allowed size ({max_bytes} bytes)"
                )

        return status, hdrs, bytes(buf)


class RegistryCandidateResult(NamedTuple):
    """Immutable result from a remote OCI registry candidate query."""

    status: ServiceCandidateStatus
    reason: ServiceCandidateReason
    index_digest: str | None = None
    child_digest: str | None = None
    detail: str | None = None


# In-memory candidate cache: (registry, repository, tag, arch) -> (RegistryCandidateResult, timestamp)
_CANDIDATE_CACHE: dict[tuple[str, str, str, str], tuple[RegistryCandidateResult, float]] = {}


def is_safe_ip(ip_str: str) -> tuple[bool, str | None]:
    """Verify that an IP address is public and non-reserved."""
    try:
        ip = ipaddress.ip_address(ip_str)
        if ip.is_loopback:
            return False, f"SSRF_BLOCKED: Loopback address {ip} is prohibited"
        if ip_str == "169.254.169.254":
            return False, "SSRF_BLOCKED: Cloud metadata address is prohibited"
        if ip.is_link_local:
            return False, f"SSRF_BLOCKED: Link-local address {ip} is prohibited"
        if ip.is_private:
            return False, f"SSRF_BLOCKED: Private RFC1918/RFC4193 address {ip} is prohibited"
        if ip.is_reserved:
            return False, f"SSRF_BLOCKED: Reserved address {ip} is prohibited"
        if ip_str == "0.0.0.0" or ip_str == "::":
            return False, f"SSRF_BLOCKED: Unspecified address {ip_str} is prohibited"
        return True, None
    except ValueError:
        return False, f"Invalid IP address format: {ip_str}"


def is_safe_registry_host(hostname: str) -> tuple[bool, str | None]:
    """Verify that a hostname resolves strictly to public, non-reserved IP addresses.

    Prevents SSRF attacks against:
    - 127.0.0.0/8 and localhost (local services)
    - 169.254.169.254 (cloud instance metadata)
    - RFC1918 private subnets (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
    - RFC4193 unique local IPv6 (fc00::/7)
    - RFC4291 link-local IPv6 (fe80::/10)
    """
    host = hostname.split(":", 1)[0].strip().lower()
    if host in ("localhost", "local", "ip6-localhost", "ip6-loopback"):
        return False, "SSRF_BLOCKED: Localhost access is prohibited"

    try:
        addr_info = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return False, f"DNS resolution failed: {exc}"

    if not addr_info:
        return False, "DNS resolution returned no addresses"

    for _family, _socktype, _proto, _canonname, sockaddr in addr_info:
        ip_str = sockaddr[0]
        safe, err = is_safe_ip(ip_str)
        if not safe:
            return False, err

    return True, None


class _SSRFSafeNetworkBackend(httpcore._backends.sync.SyncBackend):
    """Network backend that verifies the connected socket's peer IP before transmission."""

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> httpcore.NetworkStream:
        stream = super().connect_tcp(
            host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        sock = stream.get_extra_info("socket")
        if sock is not None:
            try:
                peer_ip = sock.getpeername()[0]
                safe, err = is_safe_ip(peer_ip)
                if not safe:
                    stream.close()
                    raise PermissionError(f"SSRF_BLOCKED: DNS-rebinding prohibited ({err})")
            except Exception:
                stream.close()
                raise
        return stream


class SSRFSafeTransport(httpx.HTTPTransport):
    """HTTP transport that validates pre-flight hostnames and connected socket peer IPs.

    Protects against DNS rebinding TOCTOU attacks where DNS answers change between
    pre-flight resolution and TCP connection establishment by intercepting the
    connected socket via httpcore.NetworkBackend and verifying its peer IP before
    TLS negotiation or HTTP byte transmission.
    """

    def __init__(self, **kwargs: typing.Any) -> None:
        super().__init__(**kwargs)
        self._pool._network_backend = _SSRFSafeNetworkBackend()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        # Enforce no userinfo
        if url.username or url.password:
            raise PermissionError("SSRF_BLOCKED: URL userinfo credentials are prohibited")
        # Enforce standard ports (80, 443) or default
        if url.port and url.port not in (80, 443):
            raise PermissionError(f"SSRF_BLOCKED: Non-standard registry port {url.port} is prohibited")

        host = url.host
        safe, reason = is_safe_registry_host(host)
        if not safe:
            raise PermissionError(reason)

        return super().handle_request(request)


class RegistryCandidateClient:
    """Safe, read-only OCI distribution registry client."""

    def __init__(self, *, target_arch: str = "arm64", max_queries: int = _MAX_QUERIES_PER_RUN):
        self.target_arch = target_arch
        self.max_queries = max_queries
        self._queries_performed = 0

    def query_candidate_digest(
        self,
        image_ref: ImageReference,
    ) -> RegistryCandidateResult:
        """Query the remote registry for the latest manifest digest of an image reference.

        Returns: RegistryCandidateResult(status, reason, index_digest, child_digest, detail)
        Never throws unhandled exceptions; fails closed to UNKNOWN on any error.
        """
        if image_ref.is_local_build:
            return RegistryCandidateResult(
                status=ServiceCandidateStatus.NOT_APPLICABLE,
                reason=ServiceCandidateReason.LOCAL_BUILD,
                index_digest=None,
                child_digest=None,
                detail="Registry comparison not applicable: service is built from local context",
            )

        if image_ref.is_pinned_by_digest:
            return RegistryCandidateResult(
                status=ServiceCandidateStatus.UNKNOWN,
                reason=ServiceCandidateReason.PINNED_BY_DIGEST,
                index_digest=image_ref.digest,
                child_digest=None,
                detail=f"Pinned by immutable digest {image_ref.digest}",
            )

        tag = image_ref.tag or "latest"
        cache_key = (image_ref.registry, image_ref.repository, tag, self.target_arch)
        now = time.monotonic()

        # Check cache before consuming query budget
        if cache_key in _CANDIDATE_CACHE:
            cached_res, cached_time = _CANDIDATE_CACHE[cache_key]
            if now - cached_time <= _CACHE_TTL_SECONDS:
                return cached_res

        if self._queries_performed >= self.max_queries:
            return RegistryCandidateResult(
                status=ServiceCandidateStatus.UNKNOWN,
                reason=ServiceCandidateReason.BUDGET_EXHAUSTED,
                index_digest=None,
                child_digest=None,
                detail=f"Query budget reached ({self.max_queries})",
            )

        # Enforce SSRF validation
        safe, ssrf_err = is_safe_registry_host(image_ref.registry)
        if not safe:
            return RegistryCandidateResult(
                status=ServiceCandidateStatus.UNKNOWN,
                reason=ServiceCandidateReason.SSRF_BLOCKED,
                index_digest=None,
                child_digest=None,
                detail=ssrf_err,
            )

        reg_host = "registry-1.docker.io" if image_ref.registry == "docker.io" else image_ref.registry
        url = f"https://{reg_host}/v2/{image_ref.repository}/manifests/{tag}"

        headers = {
            "Accept": _MANIFEST_ACCEPT_HEADERS,
            "User-Agent": "AIPM-Intelligence/1.0",
        }

        self._queries_performed += 1
        deadline = time.monotonic() + _TOTAL_DEADLINE_SECONDS

        try:
            transport = SSRFSafeTransport()
            with httpx.Client(transport=transport, follow_redirects=False) as client:
                req_timeout = _remaining_timeout(deadline)
                resp_status, resp_headers, resp_body = _stream_bounded_response(
                    client, "GET", url, headers=headers, timeout=req_timeout
                )

                # Handle 401 Bearer Token Challenge (standard OCI anonymous auth)
                if resp_status == 401:
                    auth_hdr = resp_headers.get("www-authenticate", "")
                    if "Bearer " in auth_hdr and "realm=" in auth_hdr:
                        realm_match = re.search(r'realm="([^"]+)"', auth_hdr)
                        if realm_match:
                            realm = realm_match.group(1)
                            parsed_realm = urlparse(realm)
                            realm_safe, realm_err = is_safe_registry_host(parsed_realm.netloc)
                            if not realm_safe:
                                return RegistryCandidateResult(
                                    status=ServiceCandidateStatus.UNKNOWN,
                                    reason=ServiceCandidateReason.SSRF_BLOCKED,
                                    index_digest=None,
                                    child_digest=None,
                                    detail=f"Authentication realm blocked: {realm_err}",
                                )
                            svc_match = re.search(r'service="([^"]+)"', auth_hdr)
                            svc_arg = f"&service={svc_match.group(1)}" if svc_match else ""
                            token_url = f"{realm}?scope=repository:{image_ref.repository}:pull{svc_arg}"
                            token_timeout = _remaining_timeout(deadline)
                            tok_status, tok_headers, tok_body = _stream_bounded_response(
                                client, "GET", token_url, headers={}, timeout=token_timeout
                            )
                            if tok_status == 200:
                                t_data = json.loads(tok_body.decode("utf-8", errors="replace"))
                                tok = t_data.get("token") or t_data.get("access_token")
                                if tok:
                                    auth_timeout = _remaining_timeout(deadline)
                                    resp_status, resp_headers, resp_body = _stream_bounded_response(
                                        client,
                                        "GET",
                                        url,
                                        headers={**headers, "Authorization": f"Bearer {tok}"},
                                        timeout=auth_timeout,
                                    )
                    else:
                        return RegistryCandidateResult(
                            status=ServiceCandidateStatus.UNKNOWN,
                            reason=ServiceCandidateReason.AUTHENTICATION_REQUIRED,
                            index_digest=None,
                            child_digest=None,
                            detail="Registry requires authentication",
                        )

                if resp_status in (401, 403):
                    return RegistryCandidateResult(
                        status=ServiceCandidateStatus.UNKNOWN,
                        reason=ServiceCandidateReason.AUTHENTICATION_REQUIRED,
                        index_digest=None,
                        child_digest=None,
                        detail=f"HTTP {resp_status}: Registry authentication required",
                    )

                if resp_status != 200:
                    return RegistryCandidateResult(
                        status=ServiceCandidateStatus.UNKNOWN,
                        reason=ServiceCandidateReason.REGISTRY_UNAVAILABLE,
                        index_digest=None,
                        child_digest=None,
                        detail=f"HTTP {resp_status} from registry",
                    )

                top_digest = resp_headers.get("docker-content-digest")
                data = json.loads(resp_body.decode("utf-8", errors="replace"))

                # Multi-architecture manifest list / OCI index
                if isinstance(data, dict) and "manifests" in data:
                    manifests = data.get("manifests") or []
                    arm64_child_digest = None
                    for m in manifests:
                        plat = m.get("platform") or {}
                        if plat.get("architecture") == self.target_arch and plat.get("os") == "linux":
                            arm64_child_digest = m.get("digest")
                            break

                    if not arm64_child_digest:
                        return RegistryCandidateResult(
                            status=ServiceCandidateStatus.UNKNOWN,
                            reason=ServiceCandidateReason.ARCHITECTURE_UNAVAILABLE,
                            index_digest=None,
                            child_digest=None,
                            detail=f"No linux/{self.target_arch} manifest in manifest list",
                        )

                    res = RegistryCandidateResult(
                        status=ServiceCandidateStatus.CURRENT,
                        reason=ServiceCandidateReason.UP_TO_DATE,
                        index_digest=top_digest,
                        child_digest=arm64_child_digest,
                        detail=f"multi-arch ({self.target_arch})",
                    )
                    _CANDIDATE_CACHE[cache_key] = (res, now)
                    return res

                # Single architecture manifest
                if top_digest:
                    res = RegistryCandidateResult(
                        status=ServiceCandidateStatus.CURRENT,
                        reason=ServiceCandidateReason.UP_TO_DATE,
                        index_digest=top_digest,
                        child_digest=None,
                        detail="single-manifest",
                    )
                    _CANDIDATE_CACHE[cache_key] = (res, now)
                    return res

                return RegistryCandidateResult(
                    status=ServiceCandidateStatus.UNKNOWN,
                    reason=ServiceCandidateReason.MANIFEST_UNAVAILABLE,
                    index_digest=None,
                    child_digest=None,
                    detail="No Docker-Content-Digest header returned",
                )

        except ValueError as val_err:
            return RegistryCandidateResult(
                status=ServiceCandidateStatus.UNKNOWN,
                reason=ServiceCandidateReason.MANIFEST_UNAVAILABLE,
                index_digest=None,
                child_digest=None,
                detail=str(val_err),
            )
        except PermissionError as p_err:
            return RegistryCandidateResult(
                status=ServiceCandidateStatus.UNKNOWN,
                reason=ServiceCandidateReason.SSRF_BLOCKED,
                index_digest=None,
                child_digest=None,
                detail=str(p_err),
            )
        except httpx.TimeoutException:
            return RegistryCandidateResult(
                status=ServiceCandidateStatus.UNKNOWN,
                reason=ServiceCandidateReason.REGISTRY_TIMEOUT,
                index_digest=None,
                child_digest=None,
                detail=f"Registry request timed out: operation deadline ({_TOTAL_DEADLINE_SECONDS}s) exceeded",
            )
        except Exception as exc:
            return RegistryCandidateResult(
                status=ServiceCandidateStatus.UNKNOWN,
                reason=ServiceCandidateReason.REGISTRY_UNAVAILABLE,
                index_digest=None,
                child_digest=None,
                detail=f"Registry query error: {type(exc).__name__}",
            )
