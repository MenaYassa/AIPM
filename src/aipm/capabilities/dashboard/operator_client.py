"""Narrow client to the canonical operator transport.

This module is a TRANSPORT ADAPTER, not an authority. It owns no session
store, no CSRF algorithm, no approval/confirmation state, no lease, no
execution contract, and no engine access: it forwards a bounded request to
the canonical operator transport and returns the canonical response.

The canonical session cookie name is imported from the canonical transport
(never re-declared here), and only an explicit method+path allow-list may be
proxied, so the dashboard can never reach rollback, confirm, kill-switch,
login, plan-mutation, or any other canonical verb through this client.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from aipm.control_plane.transport import SESSION_COOKIE

CSRF_HEADER = "X-CSRF-Token"
MAX_RESPONSE_BYTES = 16_384
DEFAULT_TIMEOUT_SECONDS = 10.0

_PROJECT_SEGMENT = r"[0-9a-f]{24}"
_ID_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}"

#: The only canonical paths the dashboard proxy may ever reach. Each entry is
#: an exact (method, path) pair: the update approval/execute verbs and the
#: read-only status/action projections. Rollback, confirm, snapshot,
#: kill-switch, login, plan authorize, and audit verbs are absent by
#: construction, not by convention.
_ALLOWED_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(rf"^/updates/{_PROJECT_SEGMENT}/approval$")),
    ("POST", re.compile(rf"^/updates/{_PROJECT_SEGMENT}/execute$")),
    ("GET", re.compile(rf"^/updates/{_PROJECT_SEGMENT}/status$")),
    ("GET", re.compile(rf"^/actions/{_ID_SEGMENT}$")),
)


class OperatorTransportUnavailable(RuntimeError):
    """Raised when the canonical transport produced no usable response.

    The message is deliberately fixed and carries no transport, exception,
    filesystem, or payload detail; the proxy translates this into a bounded
    fail-closed response.
    """

    def __init__(self) -> None:
        super().__init__("control plane transport unavailable")


@dataclass(frozen=True, slots=True)
class OperatorResponse:
    """Canonical status code plus the parsed canonical JSON object."""

    status: int
    payload: dict[str, Any]


class OperatorTransportClient(Protocol):
    """The narrow surface the dashboard proxy is allowed to depend on."""

    session_cookie_name: str

    async def request(
        self,
        method: str,
        path: str,
        *,
        session_cookie: str | None,
        csrf_token: str | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> OperatorResponse: ...


def assert_allowed_route(method: str, path: str) -> None:
    """Fail closed unless (method, path) is on the canonical allow-list."""

    for allowed_method, pattern in _ALLOWED_ROUTES:
        if method == allowed_method and pattern.fullmatch(path) is not None:
            return
    raise OperatorTransportUnavailable()


class AsgiOperatorTransportClient:
    """In-process ASGI bridge to the canonical operator transport app.

    No socket, no new protocol, no serialization format of its own: the
    canonical operator app is called over ASGI with the browser's canonical
    session cookie and CSRF token forwarded verbatim, so the canonical
    transport remains the sole authority for authentication, CSRF, rate
    limiting, authorization, confirmation, and execution.

    Cross-process composition (a real loopback HTTP client against a running
    operator transport) is deliberately NOT implemented here: that is C6
    composition work.
    """

    __slots__ = ("_app", "_timeout", "session_cookie_name", "_initialized")

    def __init__(self, app: Any, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        if app is None:
            raise ValueError("a canonical operator transport app is required")
        if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 60:
            raise ValueError("timeout must be a bounded positive number of seconds")
        object.__setattr__(self, "_app", app)
        object.__setattr__(self, "_timeout", float(timeout))
        object.__setattr__(self, "session_cookie_name", SESSION_COOKIE)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_initialized", False):
            raise AttributeError("AsgiOperatorTransportClient configuration is immutable")
        object.__setattr__(self, name, value)

    async def request(
        self,
        method: str,
        path: str,
        *,
        session_cookie: str | None,
        csrf_token: str | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> OperatorResponse:
        import httpx

        assert_allowed_route(method, path)
        headers: dict[str, str] = {}
        if isinstance(csrf_token, str) and csrf_token:
            headers[CSRF_HEADER] = csrf_token
        cookies: dict[str, str] = {}
        if isinstance(session_cookie, str) and session_cookie:
            cookies[self.session_cookie_name] = session_cookie
        try:
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://operator.invalid",
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
        except Exception as exc:  # transport, decode, or canonical app failure
            raise OperatorTransportUnavailable() from exc
        if not isinstance(payload, dict):
            raise OperatorTransportUnavailable()
        return OperatorResponse(status=int(response.status_code), payload=payload)
