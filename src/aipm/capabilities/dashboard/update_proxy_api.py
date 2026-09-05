"""Authenticated dashboard proxy to the canonical operator update verbs.

This façade is a THIN PROXY. It owns no authority of any kind:

* authentication      → the canonical owner session (cookie forwarded verbatim)
* CSRF                → the canonical session-bound token (header forwarded)
* authorization       → the canonical control-plane policy
* approval            → the canonical ConfirmationBinding/ConfirmationStore
* lease/fencing       → the canonical action repository
* execution contract  → the canonical service/executor
* rate limiting       → the canonical operator transport limiter
* audit               → the canonical control-plane audit ledger

What lives here, and nothing more: strict project identity validation,
bounded request bodies with closed key allow-lists, forwarding through the
narrow operator transport client, and a bounded whitelist projection of the
canonical response. Every unknown or unexpected condition fails closed.

A browser request alone can never cause an update mutation: it can only cause
this proxy to replay the request to the canonical transport, which performs the
full authorize → confirm → snapshot → lease → contract → gate → executor
chain. If any canonical layer is missing (for example the C6 production
composition adapters), the canonical failure is relayed unchanged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from aipm.capabilities.dashboard.operator_client import (
    OperatorResponse,
    OperatorTransportUnavailable,
)
from aipm.capabilities.dashboard.safety import assert_safe_payload

MAX_BODY_BYTES = 4_096
MAX_BODY_FIELDS = 4
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_ACTION_ID_LENGTH = 128
MAX_RELAYED_TEXT_LENGTH = 256
MAX_RELAYED_FIELDS = 16

_PROJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
# Action identifiers are canonical hex/opaque identity tokens: no slashes, no
# path separators, no whitespace, no shell metacharacters can appear.
_ACTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")

_APPROVE_KEYS = frozenset({"update_plan_digest", "idempotency_key"})
_EXECUTE_KEYS = frozenset({"action_id"})

#: Canonical safe error codes this proxy may relay verbatim. Codes outside
#: this set collapse to ``upstream_rejected`` so no unknown upstream string
#: can ever reach a browser.
_RELAYABLE_ERROR_CODES = frozenset(
    {
        "unauthenticated",
        "csrf_failed",
        "forbidden",
        "invalid_request",
        "not_found",
        "conflict",
        "confirmation_required",
        "stale_plan",
        "expired",
        "locked",
        "execution_refused",
        "internal_error",
    }
)

#: Status codes the canonical transport is allowed to express through the
#: proxy. Anything else is reported as a bounded fail-closed conflict.
_RELAYABLE_STATUS = frozenset({200, 401, 403, 404, 409, 410, 422, 423, 429, 500, 503})

_APPROVAL_FIELDS = ("allowed", "code", "decision_id", "action_id", "confirmation_required", "confirmation_id", "approval")
_EXECUTION_FIELDS = ("action_id", "executed", "outcome", "lifecycle_state")
_PLAN_FIELDS = ("target_id", "environment", "revision", "enabled", "canonical_digest")


class _ProxyRejection(Exception):
    """Internal bounded rejection raised before any upstream call."""

    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class DashboardProxyResult:
    """A canonical status code plus a bounded, sanitized dashboard payload."""

    status: int
    payload: dict[str, Any]


class DashboardUpdateProxyApi:
    """Proxy the three canonical update verbs for the operator UI.

    ``client`` is the narrow operator transport client. When it is not
    composed (the default), every verb fails closed with a bounded
    ``control_plane_unavailable`` response: the dashboard never becomes an
    alternate mutation authority just because the canonical transport is
    absent.
    """

    __slots__ = ("client",)

    def __init__(self, client: Any | None = None) -> None:
        self.client = client

    # ------------------------------------------------------------------
    # Mutation verbs (canonical session + canonical CSRF required upstream)
    # ------------------------------------------------------------------

    async def approve(
        self,
        project_id: str,
        *,
        body: bytes | None,
        session_cookie: str | None,
        csrf_token: str | None,
    ) -> DashboardProxyResult:
        """Relay one approval request; the canonical service is the authority.

        The presented plan digest is forwarded as opaque bounded material.
        This proxy never decides that a digest is valid, never derives one,
        and never records approval state: the canonical service re-verifies
        the digest against the authoritative plan identity and the canonical
        confirmation remains the sole approval authority.
        """

        try:
            identifier = self._project_id(project_id)
            payload = self._json_object(body)
            self._closed_keys(payload, _APPROVE_KEYS)
            digest = self._digest(payload.get("update_plan_digest"))
            idempotency_key = self._idempotency_key(payload.get("idempotency_key"))
        except _ProxyRejection as rejection:
            return self._error(rejection.status, rejection.code)
        return await self._forward(
            "POST",
            f"/updates/{identifier}/approval",
            session_cookie=session_cookie,
            csrf_token=csrf_token,
            json_body={"update_plan_digest": digest, "idempotency_key": idempotency_key},
            section="update_approval",
            fields=_APPROVAL_FIELDS,
        )

    async def execute(
        self,
        project_id: str,
        *,
        body: bytes | None,
        session_cookie: str | None,
        csrf_token: str | None,
    ) -> DashboardProxyResult:
        """Relay one execution request carrying only a canonical action id.

        No plan data, digest-as-authorization, filesystem path, archive path,
        command string, runtime parameter, snapshot reference, or rollback
        target is accepted: the body must be exactly ``{"action_id": ...}``.
        The canonical service consumes the confirmation, acquires the lease,
        builds the execution contract, and runs the final gate.
        """

        try:
            identifier = self._project_id(project_id)
            payload = self._json_object(body)
            self._closed_keys(payload, _EXECUTE_KEYS)
            action_id = self._action_id(payload.get("action_id"))
        except _ProxyRejection as rejection:
            return self._error(rejection.status, rejection.code)
        return await self._forward(
            "POST",
            f"/updates/{identifier}/execute",
            session_cookie=session_cookie,
            csrf_token=csrf_token,
            json_body={"action_id": action_id},
            section="update_execution",
            fields=_EXECUTION_FIELDS,
        )

    # ------------------------------------------------------------------
    # Read-only verb
    # ------------------------------------------------------------------

    async def status(self, project_id: str, *, session_cookie: str | None) -> DashboardProxyResult:
        """Relay the canonical read-only update status projection.

        GET only, no body, no CSRF token, no state change anywhere: the
        canonical handler is a projection over already-recorded control-plane
        state.
        """

        try:
            identifier = self._project_id(project_id)
        except _ProxyRejection as rejection:
            return self._error(rejection.status, rejection.code)
        return await self._forward(
            "GET",
            f"/updates/{identifier}/status",
            session_cookie=session_cookie,
            csrf_token=None,
            json_body=None,
            section="update_status",
            fields=None,
        )

    # ------------------------------------------------------------------
    # Forwarding and projection
    # ------------------------------------------------------------------

    async def _forward(
        self,
        method: str,
        path: str,
        *,
        session_cookie: str | None,
        csrf_token: str | None,
        json_body: dict[str, Any] | None,
        section: str,
        fields: tuple[str, ...] | None,
    ) -> DashboardProxyResult:
        if self.client is None:
            # C6 composition is absent: fail closed, never substitute a
            # dashboard-local execution or approval path.
            return self._error(503, "control_plane_unavailable", section=section)
        try:
            response = await self.client.request(
                method,
                path,
                session_cookie=session_cookie,
                csrf_token=csrf_token,
                json_body=json_body,
            )
        except OperatorTransportUnavailable:
            return self._error(503, "control_plane_unavailable", section=section)
        except Exception:
            # No upstream exception text, type, or traceback ever escapes.
            return self._error(503, "control_plane_unavailable", section=section)
        return self._project(response, section=section, fields=fields)

    def _project(self, response: OperatorResponse, *, section: str, fields: tuple[str, ...] | None) -> DashboardProxyResult:
        status = response.status if isinstance(response.status, int) else 0
        if status not in _RELAYABLE_STATUS:
            return self._error(409, "conflict", section=section)
        if status != 200:
            return self._error(status, self._error_code(response.payload), section=section)
        if section == "update_status":
            body = self._status_body(response.payload)
        else:
            body = self._whitelist(response.payload, fields or ())
        payload = {"available": True, "status": "ok", "error": None, section: body}
        assert_safe_payload(payload)
        return DashboardProxyResult(status=200, payload=payload)

    def _status_body(self, payload: dict[str, Any]) -> dict[str, Any]:
        plan = payload.get("plan")
        execution = payload.get("execution")
        return {
            "project_id": self._scalar(payload.get("project_id")),
            "plan": self._whitelist(plan if isinstance(plan, dict) else {}, _PLAN_FIELDS),
            "execution": {"available": bool(execution.get("available")) if isinstance(execution, dict) else False},
        }

    def _whitelist(self, payload: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        source = payload if isinstance(payload, dict) else {}
        body: dict[str, Any] = {}
        for name in fields[:MAX_RELAYED_FIELDS]:
            body[name] = self._scalar(source.get(name))
        return body

    @staticmethod
    def _scalar(value: Any) -> Any:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            return value[:MAX_RELAYED_TEXT_LENGTH]
        return None

    @staticmethod
    def _error_code(payload: dict[str, Any]) -> str:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        code = detail.get("error") if isinstance(detail, dict) else payload.get("error") if isinstance(payload, dict) else None
        if isinstance(code, str) and code in _RELAYABLE_ERROR_CODES:
            return code
        return "upstream_rejected"

    @staticmethod
    def _error(status: int, code: str, *, section: str | None = None) -> DashboardProxyResult:
        payload: dict[str, Any] = {"available": False, "status": "error", "error": code}
        if section:
            payload[section] = None
        assert_safe_payload(payload)
        return DashboardProxyResult(status=status, payload=payload)

    # ------------------------------------------------------------------
    # Bounded request validation
    # ------------------------------------------------------------------

    @staticmethod
    def _project_id(value: Any) -> str:
        if not isinstance(value, str) or _PROJECT_ID_PATTERN.fullmatch(value) is None:
            raise _ProxyRejection(422, "invalid_request")
        return value

    @staticmethod
    def _json_object(body: bytes | None) -> dict[str, Any]:
        if body is None:
            raise _ProxyRejection(422, "invalid_request")
        if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_BODY_BYTES:
            raise _ProxyRejection(422, "invalid_request")
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            raise _ProxyRejection(422, "invalid_request") from None
        if not isinstance(payload, dict) or not payload or len(payload) > MAX_BODY_FIELDS:
            raise _ProxyRejection(422, "invalid_request")
        return payload

    @staticmethod
    def _closed_keys(payload: dict[str, Any], allowed: frozenset[str]) -> None:
        if set(payload) - allowed:
            raise _ProxyRejection(422, "invalid_request")

    @staticmethod
    def _digest(value: Any) -> str:
        if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
            raise _ProxyRejection(422, "invalid_request")
        return value

    @staticmethod
    def _idempotency_key(value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise _ProxyRejection(422, "invalid_request")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise _ProxyRejection(422, "invalid_request")
        return value

    @staticmethod
    def _action_id(value: Any) -> str:
        if not isinstance(value, str) or len(value) > MAX_ACTION_ID_LENGTH:
            raise _ProxyRejection(422, "invalid_request")
        if _ACTION_ID_PATTERN.fullmatch(value) is None:
            raise _ProxyRejection(422, "invalid_request")
        return value
