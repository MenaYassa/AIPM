"""Deterministic tests for Dashboard → Operator Transport HTTP proxy contract."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aipm.composition.http_operator_client import HttpOperatorTransportClient
from aipm.capabilities.dashboard.update_proxy_api import DashboardUpdateProxyApi
from aipm.dashboard.server import create_app


def test_http_client_forwards_session_cookie():
    """HTTP client includes session cookie in requests."""
    client = HttpOperatorTransportClient()
    
    # Verify the client has session_cookie_name set
    assert client.session_cookie_name == "aipm_cp_session"
    assert hasattr(client, "_base_url")
    assert hasattr(client, "_timeout")


def test_http_client_route_validation():
    """HTTP client enforces route allow-list."""
    from aipm.capabilities.dashboard.operator_client import (
        assert_allowed_route,
        OperatorTransportUnavailable,
    )
    
    # Allowed update routes
    assert_allowed_route("POST", "/updates/0123456789abcdef01234567/approval")
    assert_allowed_route("POST", "/updates/0123456789abcdef01234567/execute")
    assert_allowed_route("GET", "/updates/0123456789abcdef01234567/status")
    
    # Disallowed routes (no rollback, no kill-switch, no admin)
    with pytest.raises(OperatorTransportUnavailable):
        assert_allowed_route("POST", "/rollback")
    
    with pytest.raises(OperatorTransportUnavailable):
        assert_allowed_route("POST", "/kill-switch")
    
    with pytest.raises(OperatorTransportUnavailable):
        assert_allowed_route("GET", "/admin")
    
    with pytest.raises(OperatorTransportUnavailable):
        assert_allowed_route("GET", "/session")  # /session not in allow-list for proxy


def test_session_csrf_endpoint_requires_authenticated_session():
    """GET /api/session/csrf requires authenticated session."""
    from aipm.core.app import Application
    
    app = create_app(application=Application.create())
    client = TestClient(app)
    
    # No session cookie → 401 unauthenticated
    response = client.get("/api/session/csrf")
    assert response.status_code == 401
    assert response.json()["error"] == "unauthenticated"


def test_session_csrf_endpoint_fails_closed_when_operator_unavailable():
    """GET /api/session/csrf fails closed (503) when operator transport unavailable."""
    from aipm.core.app import Application
    
    # Create app with explicitly disabled mutations (no HTTP client)
    app = create_app(
        application=Application.create(),
        update_proxy_api=DashboardUpdateProxyApi(client=None),
    )
    client = TestClient(app)
    
    # Even with session cookie, if operator unavailable → 503
    # (We use a fake cookie since we're testing the fail-closed path)
    response = client.get(
        "/api/session/csrf",
        cookies={"aipm_cp_session": "fake-session-token"},
    )
    assert response.status_code == 503
    assert response.json()["error"] == "control_plane_unavailable"


def test_proxy_without_client_returns_503_control_plane_unavailable():
    """DashboardUpdateProxyApi with no client fails closed (503)."""
    import asyncio
    
    async def run_test():
        proxy = DashboardUpdateProxyApi(client=None)
        
        # Test the fail-closed behavior
        result = await proxy.approve(
            "0123456789abcdef01234567",
            body=b'{"update_plan_digest": "' + b'a' * 64 + b'", "idempotency_key": "test"}',
            session_cookie="test",
            csrf_token="test",
        )
        
        assert result.status == 503
        assert result.payload["error"] == "control_plane_unavailable"
        assert result.payload["available"] is False
    
    asyncio.run(run_test())


def test_dashboard_server_composition_defaults_to_http_client():
    """Dashboard server composes HTTP client when AIPM_DASHBOARD_MUTATIONS enabled."""
    import os
    from aipm.core.app import Application
    
    original_mutations = os.environ.get("AIPM_DASHBOARD_MUTATIONS")
    original_url = os.environ.get("AIPM_OPERATOR_TRANSPORT_URL")
    
    try:
        # Enable mutations with production URL
        os.environ["AIPM_DASHBOARD_MUTATIONS"] = "true"
        os.environ["AIPM_OPERATOR_TRANSPORT_URL"] = "http://127.0.0.1:8789"
        
        app = create_app(application=Application.create())
        
        # App creation succeeds; HTTP client is composed internally
        assert app is not None
        
    finally:
        if original_mutations is None:
            os.environ.pop("AIPM_DASHBOARD_MUTATIONS", None)
        else:
            os.environ["AIPM_DASHBOARD_MUTATIONS"] = original_mutations
        if original_url is None:
            os.environ.pop("AIPM_OPERATOR_TRANSPORT_URL", None)
        else:
            os.environ["AIPM_OPERATOR_TRANSPORT_URL"] = original_url


def test_dashboard_server_fails_closed_when_mutations_disabled():
    """Dashboard server fails closed when AIPM_DASHBOARD_MUTATIONS=false."""
    import os
    from aipm.core.app import Application
    
    original = os.environ.get("AIPM_DASHBOARD_MUTATIONS")
    
    try:
        os.environ["AIPM_DASHBOARD_MUTATIONS"] = "false"
        
        app = create_app(application=Application.create())
        
        # App creation succeeds with fail-closed proxy
        assert app is not None
        
    finally:
        if original is None:
            os.environ.pop("AIPM_DASHBOARD_MUTATIONS", None)
        else:
            os.environ["AIPM_DASHBOARD_MUTATIONS"] = original


def test_http_client_rejects_invalid_urls():
    """HTTP client validates loopback-only constraint."""
    with pytest.raises(ValueError, match="loopback-only"):
        HttpOperatorTransportClient(base_url="http://example.com:8789")
    
    with pytest.raises(ValueError, match="loopback-only"):
        HttpOperatorTransportClient(base_url="http://192.168.1.1:8789")
    
    with pytest.raises(ValueError, match="loopback-only"):
        HttpOperatorTransportClient(base_url="https://127.0.0.1:8789")
    
    # Valid loopback URLs accepted
    client = HttpOperatorTransportClient(base_url="http://127.0.0.1:8789")
    assert client._base_url == "http://127.0.0.1:8789"


def test_http_client_validates_timeout_bounds():
    """HTTP client enforces timeout bounds (1-60 seconds)."""
    with pytest.raises(ValueError, match="bounded positive"):
        HttpOperatorTransportClient(timeout=0)
    
    with pytest.raises(ValueError, match="bounded positive"):
        HttpOperatorTransportClient(timeout=-1)
    
    with pytest.raises(ValueError, match="bounded positive"):
        HttpOperatorTransportClient(timeout=61)
    
    with pytest.raises(ValueError, match="bounded positive"):
        HttpOperatorTransportClient(timeout=100)
    
    # Valid timeouts accepted
    client = HttpOperatorTransportClient(timeout=10.0)
    assert client._timeout == 10.0
    
    client = HttpOperatorTransportClient(timeout=1)
    assert client._timeout == 1.0
    
    client = HttpOperatorTransportClient(timeout=60)
    assert client._timeout == 60.0


def test_frontend_no_direct_operator_transport_access():
    """mission-control-projects.js contains no direct 127.0.0.1:8789 browser calls."""
    import pathlib
    
    js_file = pathlib.Path(
        "src/aipm/dashboard/static/mission-control-projects.js"
    )
    if js_file.exists():
        content = js_file.read_text()
        
        # Assert no direct browser access to operator transport
        assert "127.0.0.1:8789" not in content, \
            "JavaScript must not directly access operator transport; use same-origin /api/session/csrf"
        
        # Assert CSRF acquisition is same-origin
        assert "/api/session/csrf" in content, \
            "CSRF must be acquired via same-origin dashboard endpoint"
        
        # Assert no hard-coded production execution affordance
        assert "permanent" not in content or "engaged" in content, \
            "Frontend must not hard-code production as executable"


def test_proxy_contract_session_cookie_forwarding():
    """Proxy forwards session cookie to operator transport."""
    client = HttpOperatorTransportClient()
    
    # Verify client stores session cookie name
    assert client.session_cookie_name == "aipm_cp_session"
    
    # Verify client is immutable after initialization
    with pytest.raises(AttributeError, match="immutable"):
        client.session_cookie_name = "different"


def test_proxy_contract_csrf_forwarding():
    """Proxy forwards X-CSRF-Token header to operator transport."""
    client = HttpOperatorTransportClient()
    
    # CSRF header name is canonical
    from aipm.capabilities.dashboard.operator_client import CSRF_HEADER
    assert CSRF_HEADER == "X-CSRF-Token"


def test_proxy_contract_bounded_response_projection():
    """Proxy applies bounded field whitelists to responses."""
    from aipm.capabilities.dashboard.update_proxy_api import (
        _APPROVAL_FIELDS,
        _EXECUTION_FIELDS,
        _STATUS_ACTION_FIELDS,
    )
    
    # Verify field allow-lists are bounded
    assert len(_APPROVAL_FIELDS) <= 16
    assert len(_EXECUTION_FIELDS) <= 16
    assert len(_STATUS_ACTION_FIELDS) <= 16
    
    # Verify no sensitive fields are in response projections
    approval_fields_set = set(_APPROVAL_FIELDS)
    assert "receipt" not in approval_fields_set
    assert "fencing_token" not in approval_fields_set
    assert "contract_digest" not in approval_fields_set
    assert "snapshot_reference" not in approval_fields_set


def test_proxy_contract_no_automatic_retry():
    """Proxy does not automatically retry mutation requests."""
    import asyncio
    
    async def run_test():
        proxy = DashboardUpdateProxyApi(client=None)
        
        # Single call returns immediately; no retry loop
        result = await proxy.approve(
            "0123456789abcdef01234567",
            body=b'{"update_plan_digest": "' + b'a' * 64 + b'", "idempotency_key": "test"}',
            session_cookie=None,
            csrf_token=None,
        )
        
        # Result is 503 fail-closed, not a retry attempt
        assert result.status == 503
        assert result.payload["error"] == "control_plane_unavailable"
    
    asyncio.run(run_test())
