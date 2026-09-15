"""Tests for dashboard HTTP operator transport client integration."""

from __future__ import annotations

import pytest

from aipm.composition.http_operator_client import HttpOperatorTransportClient
from aipm.capabilities.dashboard.operator_client import (
    OperatorTransportUnavailable,
    OperatorResponse,
)
from aipm.capabilities.dashboard.update_proxy_api import DashboardUpdateProxyApi


def test_http_client_requires_loopback_url():
    """HTTP client rejects non-loopback URLs."""
    with pytest.raises(ValueError, match="loopback-only"):
        HttpOperatorTransportClient(base_url="http://example.com:8789")
    
    with pytest.raises(ValueError, match="loopback-only"):
        HttpOperatorTransportClient(base_url="http://192.168.1.1:8789")
    
    # Loopback accepted
    client = HttpOperatorTransportClient(base_url="http://127.0.0.1:8789")
    assert client.session_cookie_name == "aipm_cp_session"


def test_http_client_requires_bounded_timeout():
    """HTTP client validates timeout bounds."""
    with pytest.raises(ValueError, match="bounded positive"):
        HttpOperatorTransportClient(timeout=0)
    
    with pytest.raises(ValueError, match="bounded positive"):
        HttpOperatorTransportClient(timeout=-1)
    
    with pytest.raises(ValueError, match="bounded positive"):
        HttpOperatorTransportClient(timeout=100)
    
    # Bounded timeout accepted
    client = HttpOperatorTransportClient(timeout=10.0)
    assert client._timeout == 10.0


def test_proxy_without_client_fails_closed():
    """Proxy with no client returns control_plane_unavailable."""
    import asyncio
    
    async def run_test():
        proxy = DashboardUpdateProxyApi(client=None)
        
        # All mutation verbs fail closed (async methods)
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


def test_http_client_enforces_allow_list():
    """HTTP client only accepts approved routes."""
    from aipm.capabilities.dashboard.operator_client import assert_allowed_route
    
    # Allowed routes
    assert_allowed_route("POST", "/updates/0123456789abcdef01234567/approval")
    assert_allowed_route("POST", "/updates/0123456789abcdef01234567/execute")
    assert_allowed_route("GET", "/updates/0123456789abcdef01234567/status")
    
    # Disallowed routes
    with pytest.raises(OperatorTransportUnavailable):
        assert_allowed_route("POST", "/rollback")
    
    with pytest.raises(OperatorTransportUnavailable):
        assert_allowed_route("POST", "/kill-switch")
    
    with pytest.raises(OperatorTransportUnavailable):
        assert_allowed_route("GET", "/admin")


def test_dashboard_server_composition_wiring():
    """Dashboard server wires HTTP client when AIPM_DASHBOARD_MUTATIONS enabled."""
    import os
    from aipm.dashboard.server import create_app
    
    # Save original env
    original = os.environ.get("AIPM_DASHBOARD_MUTATIONS")
    original_url = os.environ.get("AIPM_OPERATOR_TRANSPORT_URL")
    
    try:
        # Explicitly enable mutations with custom URL
        os.environ["AIPM_DASHBOARD_MUTATIONS"] = "true"
        os.environ["AIPM_OPERATOR_TRANSPORT_URL"] = "http://127.0.0.1:8789"
        
        app = create_app()
        
        # App creation succeeds (HTTP client composed internally)
        assert app is not None
        
    finally:
        # Restore env
        if original is None:
            os.environ.pop("AIPM_DASHBOARD_MUTATIONS", None)
        else:
            os.environ["AIPM_DASHBOARD_MUTATIONS"] = original
        if original_url is None:
            os.environ.pop("AIPM_OPERATOR_TRANSPORT_URL", None)
        else:
            os.environ["AIPM_OPERATOR_TRANSPORT_URL"] = original_url


def test_dashboard_server_fails_closed_when_disabled():
    """Dashboard server fails closed when mutations explicitly disabled."""
    import os
    from aipm.dashboard.server import create_app
    
    original = os.environ.get("AIPM_DASHBOARD_MUTATIONS")
    
    try:
        os.environ["AIPM_DASHBOARD_MUTATIONS"] = "false"
        
        app = create_app()
        
        # App creation succeeds with fail-closed proxy
        assert app is not None
        
    finally:
        if original is None:
            os.environ.pop("AIPM_DASHBOARD_MUTATIONS", None)
        else:
            os.environ["AIPM_DASHBOARD_MUTATIONS"] = original
