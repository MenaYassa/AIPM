"""HTTP client to the canonical operator transport service.

Cross-process client for production deployments where the operator transport
runs as a separate systemd service (aipm-operator-transport.service) on
loopback port 8789.
"""

from __future__ import annotations

import httpx

from aipm.capabilities.dashboard.operator_client import (
    OperatorResponse,
    OperatorTransportUnavailable,
    assert_allowed_route,
    CSRF_HEADER,
    MAX_RESPONSE_BYTES,
)
from aipm.control_plane.transport import SESSION_COOKIE


class HttpOperatorTransportClient:
    """HTTP client to a running operator transport service.

    Connects to the canonical operator transport over HTTP (loopback only in
    production). Forwards the browser's session cookie and CSRF token verbatim,
    so the canonical transport remains the sole authority for authentication,
    CSRF verification, rate limiting, authorization, confirmation, and execution.

    This is the C6 production composition: the operator transport runs as a
    separate systemd service (aipm-operator-transport.service) on 127.0.0.1:8789,
    and the dashboard (aipm-dashboard.service) connects to it via this client.
    """

    __slots__ = ("_base_url", "_timeout", "session_cookie_name", "_initialized")

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8789",
        timeout: float = 10.0,
    ) -> None:
        """Initialize HTTP client to operator transport.

        Args:
            base_url: Operator transport base URL (default: http://127.0.0.1:8789).
                MUST be loopback-only in production. Never accepts external addresses.
            timeout: Request timeout in seconds (default: 10.0).

        Raises:
            ValueError: If base_url is not loopback or timeout is invalid.
        """

        if not isinstance(base_url, str) or not base_url.startswith("http://127.0.0.1:"):
            raise ValueError("base_url must be loopback-only (http://127.0.0.1:PORT)")
        if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 60:
            raise ValueError("timeout must be a bounded positive number of seconds")

        object.__setattr__(self, "_base_url", base_url.rstrip("/"))
        object.__setattr__(self, "_timeout", float(timeout))
        object.__setattr__(self, "session_cookie_name", SESSION_COOKIE)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_initialized", False):
            raise AttributeError("HttpOperatorTransportClient configuration is immutable")
        object.__setattr__(self, name, value)

    async def request(
        self,
        method: str,
        path: str,
        *,
        session_cookie: str | None,
        csrf_token: str | None = None,
        json_body: dict[str, object] | None = None,
    ) -> OperatorResponse:
        """Forward one bounded request to the canonical operator transport.

        Args:
            method: HTTP method (POST or GET only, per allow-list).
            path: Request path (must match canonical allow-list).
            session_cookie: Browser's canonical session cookie value or None.
            csrf_token: Browser's X-CSRF-Token header value or None.
            json_body: Request body as dict (for POST) or None.

        Returns:
            OperatorResponse with status code and parsed JSON payload.

        Raises:
            OperatorTransportUnavailable: If the transport is unreachable,
                returns a non-dict response, or exceeds MAX_RESPONSE_BYTES.
        """

        assert_allowed_route(method, path)

        headers: dict[str, str] = {}
        if isinstance(csrf_token, str) and csrf_token:
            headers[CSRF_HEADER] = csrf_token

        cookies: dict[str, str] = {}
        if isinstance(session_cookie, str) and session_cookie:
            cookies[self.session_cookie_name] = session_cookie

        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    headers=headers,
                    cookies=cookies,
                    json=json_body,
                )
                body = response.content
                if len(body) > MAX_RESPONSE_BYTES:
                    raise OperatorTransportUnavailable()
                payload = response.json()
        except OperatorTransportUnavailable:
            raise
        except Exception as exc:
            # Transport, decode, or operator service failure: fail closed.
            # No exception text, type, or traceback ever escapes to browser.
            raise OperatorTransportUnavailable() from exc

        if not isinstance(payload, dict):
            raise OperatorTransportUnavailable()

        return OperatorResponse(
            status=int(response.status_code),
            payload=payload,
            headers=dict(response.headers),
        )
