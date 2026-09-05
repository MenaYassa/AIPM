"""Authenticated localhost-only operator transport for the control plane.

This module is an ADAPTER, not an authority: every operation delegates to the
``OwnerControlPlaneService``; no policy, lifecycle, identity, CAS, or SQL
logic lives here. The app binds to loopback only — unsafe bind addresses are
refused at construction, not merely discouraged.

Surface: login/logout with the canonical owner authenticator and opaque
server-side sessions; CSRF-protected operator verbs (authorize, confirm,
execute, reconcile, rollback, kill switch); bounded update-plane verbs
(authorization request, fail-closed execution, status); bounded read views;
and audit chain verification. There is deliberately no generic
command/execution route and no path to legacy mutation machinery, providers,
or process execution.

Update-plane contract (C2): the update authorization route performs ONLY the
canonical authorize step of authorize → confirmation-required → confirm. The
canonical ConfirmationBinding/ConfirmationStore remains the sole approval
authority; this transport never consumes a confirmation, never fabricates
one, and stores no approval state. Because the authorize→confirm composition
is not wired into this transport yet (C4), every authorization response is
explicitly marked "approval": "deferred" so an authorization decision can
never be mistaken for an approved update.
"""
from __future__ import annotations

import ipaddress
import re
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response

from aipm.control_plane.executor import ExecutionRefused
from aipm.control_plane.models import (
    ActionRequest,
    ControlPlaneError,
    OperationKind,
    PlanningErrorCode,
)

SESSION_COOKIE = "aipm_cp_session"
_MAX_BODY_FIELDS = 16
_MAX_FIELD_LENGTH = 2000
_RATE_WINDOW_SECONDS = 60.0
_RATE_LIMIT_AUTHORIZE = 30
_RATE_LIMIT_CONFIRM = 30
_RATE_LIMIT_UPDATE_APPROVAL = 30

# Dashboard project identifiers are 24 lowercase hex characters (the
# Mission Control inventory identity). A registered staging control-plane
# target may carry this identifier as its target_id; the transport only
# ever resolves it against the canonical plan store via the service.
_PROJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")

# Canonical update-plan digest binding: the operator presents the digest of
# the update plan they reviewed; it travels inside ActionRequest.metadata so
# the canonical identity/confirmation/contract machinery binds the approval
# to the exact plan content. The transport never interprets it as authority.
_UPDATE_PLAN_DIGEST_KEY = "update_plan_digest"

_SAFE_ERROR_CODES = {
    PlanningErrorCode.INVALID_REQUEST: (422, "invalid_request"),
    PlanningErrorCode.UNSUPPORTED_OPERATION: (403, "forbidden"),
    PlanningErrorCode.UNAVAILABLE_TARGET: (403, "forbidden"),
    PlanningErrorCode.INVALID_PLAN: (422, "invalid_request"),
    PlanningErrorCode.EXPIRED_PLAN: (410, "expired"),
    PlanningErrorCode.CONFIRMATION_MISMATCH: (409, "confirmation_required"),
    PlanningErrorCode.SESSION_INVALID: (401, "unauthenticated"),
    PlanningErrorCode.AUTHENTICATION_REJECTED: (401, "unauthenticated"),
    PlanningErrorCode.IDEMPOTENCY_CONFLICT: (409, "conflict"),
    PlanningErrorCode.STATE_CONFLICT: (409, "conflict"),
    PlanningErrorCode.STALE_EVIDENCE: (409, "stale_plan"),
    PlanningErrorCode.STORAGE_CORRUPT: (500, "internal_error"),
    PlanningErrorCode.UNAVAILABLE_EVIDENCE: (409, "conflict"),
}


class _RateLimiter:
    """Bounded sliding-window limiter for abuse control (not a subsystem)."""

    def __init__(self, *, limit: int, window_seconds: float = _RATE_WINDOW_SECONDS, max_keys: int = 1024) -> None:
        self._limit = limit
        self._window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=limit * 4))
        self._max_keys = max_keys

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        events = self._events[key]
        while events and now - events[0] > self._window:
            events.popleft()
        if len(events) >= self._limit:
            return False
        events.append(now)
        if len(self._events) > self._max_keys:  # bound memory; drop oldest arbitrarily
            for stale in list(self._events)[: len(self._events) - self._max_keys]:
                self._events.pop(stale, None)
        return True


def validate_bind_address(bind: str) -> str:
    """Refuse non-loopback bind addresses; fail closed by default."""

    try:
        parsed = ipaddress.ip_address(bind)
    except ValueError as exc:
        raise ValueError(f"Unsafe operator transport bind address: {bind!r}") from exc
    if not parsed.is_loopback or not isinstance(parsed, ipaddress.IPv4Address):
        raise ValueError(f"Unsafe operator transport bind address: {bind!r}")
    return bind


def _error(code: str, message: str, status: int) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message[:256]})


def _translate_domain_error(exc: ControlPlaneError) -> HTTPException:
    status, code = _SAFE_ERROR_CODES.get(exc.code, (500, "internal_error"))
    return HTTPException(status_code=status, detail={"error": code, "message": str(exc)[:256]})


def _plan_payload(plan: dict) -> dict:
    return {
        "target_id": plan.get("target_id"),
        "environment": plan.get("environment"),
        "revision": plan.get("revision"),
        "title": plan.get("title"),
        "objective": plan.get("objective"),
        "enabled": plan.get("enabled"),
        "canonical_digest": plan.get("canonical_digest"),
    }


def create_operator_app(
    service,
    *,
    bind: str = "127.0.0.1",
    secure_cookies: bool = False,
    session_ttl_seconds: int = 1800,
) -> FastAPI:
    """Build the operator transport app for one control-plane service.

    ``bind`` is validated (loopback only) even though FastAPI itself binds
    where uvicorn is told to — refusing here means an unsafe bind can never
    be configured accidentally. ``secure_cookies`` must be enabled when the
    transport is terminated over TLS; plain loopback HTTP keeps it off.
    """

    validate_bind_address(bind)
    if service._sessions._inactivity_timeout.total_seconds() > session_ttl_seconds:  # noqa: SLF001 - adapter boundary
        raise ValueError("session store inactivity timeout exceeds the transport cookie budget")
    limiter_authorize = _RateLimiter(limit=_RATE_LIMIT_AUTHORIZE)
    limiter_confirm = _RateLimiter(limit=_RATE_LIMIT_CONFIRM)
    limiter_update_approval = _RateLimiter(limit=_RATE_LIMIT_UPDATE_APPROVAL)

    app = FastAPI(title="AIPM Control Plane Operator Transport", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return response

    # ------------------------------------------------------------------
    # Session resolution
    # ------------------------------------------------------------------

    def _resolve_session(request: Request):
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id:
            raise _error("unauthenticated", "Authentication required", 401)
        try:
            session = service.session(session_id)
        except ControlPlaneError:
            raise _error("unauthenticated", "Authentication required", 401)
        if session is None:
            raise _error("unauthenticated", "Authentication required", 401)
        return session

    def _require_csrf(request: Request) -> None:
        presented = request.headers.get("X-CSRF-Token", "")
        session_id = request.cookies.get(SESSION_COOKIE, "")
        try:
            session = service.session(session_id) if session_id else None
        except ControlPlaneError:
            session = None
        if session is None or not isinstance(presented, str) or not session.verify_csrf(presented):
            raise _error("csrf_failed", "CSRF token missing or invalid", 403)

    def _bounded_fields(payload: dict | None) -> list[tuple[str, str]]:
        if payload is None:
            return []
        if not isinstance(payload, dict) or len(payload) > _MAX_BODY_FIELDS:
            raise _error("invalid_request", "Malformed request body", 422)
        allowed = {"title", "objective"}
        fields: list[tuple[str, str]] = []
        for key, value in payload.items():
            if key not in allowed:
                raise _error("invalid_request", f"Field {key!r} is not authorable", 422)
            if not isinstance(value, str) or not value or len(value) > _MAX_FIELD_LENGTH:
                raise _error("invalid_request", f"Field {key!r} must be a bounded string", 422)
            fields.append((key, value))
        if not fields:
            raise _error("invalid_request", "At least one authorable field is required", 422)
        return fields

    async def _json_body(request: Request) -> dict:
        try:
            payload = await request.json()
        except Exception:
            raise _error("invalid_request", "Malformed JSON body", 422)
        if not isinstance(payload, dict):
            raise _error("invalid_request", "Malformed JSON body", 422)
        return payload

    def _bounded_id(value: str, name: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 128 or any(character in value for character in "/\\\0"):
            raise _error("invalid_request", f"Invalid {name}", 422)
        return value

    def _bounded_project_id(value: str) -> str:
        if not isinstance(value, str) or _PROJECT_ID_PATTERN.fullmatch(value) is None:
            raise _error("invalid_request", "Invalid project identifier", 422)
        return value

    def _update_plan_digest_pair(value) -> tuple[str, str]:
        if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise _error("invalid_request", "A 64-character hex update_plan_digest is required", 422)
        return (_UPDATE_PLAN_DIGEST_KEY, value)

    def _run(domain_callable):
        try:
            return domain_callable()
        except ControlPlaneError as exc:
            raise _translate_domain_error(exc)

    # ------------------------------------------------------------------
    # Health and session
    # ------------------------------------------------------------------

    @app.get("/health")
    def health():
        from aipm.control_plane.storage.schema import SCHEMA_VERSION
        from aipm.control_plane.build_identity import resolve_build_identity

        payload: dict[str, Any] = {
            "service": "aipm-control-plane-operator",
            "status": "available",
            "schema_version": SCHEMA_VERSION,
        }
        try:
            build = resolve_build_identity(production=False)
            payload["build"] = build.safe_dict()
        except Exception:
            payload["build"] = {"commit_sha": "", "version": "development", "environment": "development"}
        try:
            verification = service.verify_audit_chain()
            payload["audit_chain"] = {
                "valid": verification.ok,
                "events_checked": verification.events_checked,
            }
        except Exception:
            payload["audit_chain"] = {"valid": False, "events_checked": 0}
        try:
            payload["kill_switch"] = service.kill_switch_status()
        except Exception:
            payload["kill_switch"] = {"switches": []}
        return payload

    @app.post("/login")
    async def login(request: Request, response: Response):
        payload = await _json_body(request)
        secret = payload.get("secret")
        if not isinstance(secret, str) or not secret or len(secret) > 1024:
            raise _error("invalid_request", "Malformed login body", 422)
        client = request.client.host if request.client else "unknown"
        # Authentication failures are deliberately generic: the canonical
        # authenticator's bounded limiter is the abuse control.
        try:
            session = service.login(secret)
        except ControlPlaneError:
            raise _error("unauthenticated", "Authentication failed", 401)
        response.set_cookie(
            SESSION_COOKIE,
            session.session_id,
            httponly=True,
            samesite="strict",
            secure=secure_cookies,
            max_age=session_ttl_seconds,
            path="/",
        )
        return {"subject": session.principal.subject, "authenticated": True}

    @app.get("/session")
    def session_view(request: Request):
        session = _resolve_session(request)
        return {
            "subject": session.principal.subject,
            "authenticated": True,
            "auth_epoch": session.auth_epoch,
            "expires_at": session.expires_at.isoformat(),
            "csrf_token": session.csrf_token,
        }

    @app.post("/logout")
    def logout(request: Request, response: Response):
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id:
            _require_csrf(request)
            service.logout(session_id)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"authenticated": False}

    # ------------------------------------------------------------------
    # Plans
    # ------------------------------------------------------------------

    @app.get("/plans/{target_id}")
    def read_plan(target_id: str, request: Request):
        _resolve_session(request)
        target_id = _bounded_id(target_id, "plan target")
        plan = _run(lambda: service.plan_view(target_id))
        if plan is None:
            raise _error("not_found", "Plan not found", 404)
        return _plan_payload(plan)

    @app.post("/plans/{target_id}/authorize")
    async def authorize_plan(target_id: str, request: Request):
        session = _resolve_session(request)
        _require_csrf(request)
        if not limiter_authorize.allow(session.session_id):
            raise _error("conflict", "Too many authorization attempts; slow down", 429)
        target_id = _bounded_id(target_id, "plan target")
        payload = await _json_body(request)
        fields = _bounded_fields(payload.get("fields"))
        idempotency_key = payload.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 128:
            raise _error("invalid_request", "A bounded idempotency_key is required", 422)
        target_view = _run(lambda: service.plan_view(target_id))
        if target_view is None:
            raise _error("not_found", "Plan not found", 404)
        action_request = ActionRequest(
            operation=OperationKind.UPDATE_PROJECT_PLAN,
            target_id=target_id,
            idempotency_key=idempotency_key,
            metadata=tuple(fields),
            environment=target_view["environment"],
        )

        def _authorize():
            decision = service.authorize(session.session_id, action_request)
            identity = decision.action_identity
            return {
                "decision_id": decision.decision_id,
                "allowed": decision.allowed,
                "code": decision.code.value,
                "action_id": identity.action_id if identity else None,
                "confirmation_required": decision.confirmation_required,
                "expires_at": decision.expires_at.isoformat(),
            }

        return _run(_authorize)

    # ------------------------------------------------------------------
    # Update plane (canonical approval flow over update projects)
    # ------------------------------------------------------------------

    @app.post("/updates/{project_id}/approval")
    async def request_update_authorization(project_id: str, request: Request):
        # Authorize step ONLY. The canonical update approval composition is
        # authorize → confirmation-required → confirm, with the canonical
        # ConfirmationBinding/ConfirmationStore as the authoritative approval
        # state. This transport does not consume or fabricate confirmations
        # and keeps no approval state of its own, so the confirmation step is
        # deferred until C4 wires the composition. The response therefore
        # reports "approval": "deferred": an authorization decision is never
        # presented as an approved update. When C4 lands, this handler will
        # additionally delegate the confirmation step to the canonical
        # confirmation flow after authorization succeeds.
        session = _resolve_session(request)
        _require_csrf(request)
        if not limiter_update_approval.allow(session.session_id):
            raise _error("conflict", "Too many update approval attempts; slow down", 429)
        project_id = _bounded_project_id(project_id)
        payload = await _json_body(request)
        allowed_keys = {"idempotency_key", _UPDATE_PLAN_DIGEST_KEY}
        if not isinstance(payload, dict) or not payload or len(payload) > _MAX_BODY_FIELDS:
            raise _error("invalid_request", "Malformed request body", 422)
        for key in payload:
            if key not in allowed_keys:
                raise _error("invalid_request", f"Field {key!r} is not authorable", 422)
        idempotency_key = payload.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 128:
            raise _error("invalid_request", "A bounded idempotency_key is required", 422)
        digest_pair = _update_plan_digest_pair(payload.get(_UPDATE_PLAN_DIGEST_KEY))

        target_view = _run(lambda: service.plan_view(project_id))
        if target_view is None:
            raise _error("not_found", "Project is not registered with the control plane", 404)

        action_request = ActionRequest(
            operation=OperationKind.UPDATE_PROJECT_PLAN,
            target_id=project_id,
            idempotency_key=idempotency_key,
            metadata=(digest_pair,),
            environment=target_view["environment"],
        )

        def _authorize():
            decision = service.authorize(session.session_id, action_request)
            identity = decision.action_identity
            return {
                "decision_id": decision.decision_id,
                "allowed": decision.allowed,
                "code": decision.code.value,
                "action_id": identity.action_id if identity else None,
                "confirmation_required": decision.confirmation_required,
                "expires_at": decision.expires_at.isoformat(),
                # Explicit: this decision is NOT a confirmed approval. The
                # canonical confirmation step (authorize → confirm) is
                # composed in C4; until then the approval is deferred.
                "approval": "deferred",
            }

        return _run(_authorize)

    @app.post("/updates/{project_id}/execute")
    async def run_update_for_project(project_id: str, request: Request):
        # Execution of update-plane actions requires the C4 runtime wiring
        # (executor service over IPC with a plan-bound execution contract).
        # Until C4 lands, this verb is deliberately not mutation-capable and
        # fails closed: no target is resolved, no plan is read, no state is
        # touched, and no execution path exists in this transport. C4 will
        # replace this handler's body with delegation to the canonical
        # action execution flow (authorize → confirm → snapshot → lease →
        # contract → IPC executor) for the action this project's approval
        # registered.
        _resolve_session(request)
        _require_csrf(request)
        _bounded_project_id(project_id)
        raise _error("unavailable", "Update execution is not available in this composition", 503)

    @app.get("/updates/{project_id}/status")
    def update_status(project_id: str, request: Request):
        # Read-only projection over already-recorded control-plane state; it
        # never advances, invents, or fabricates lifecycle state. Action-level
        # progress remains on the existing /actions/{action_id} read view.
        _resolve_session(request)
        project_id = _bounded_project_id(project_id)
        plan_view = _run(lambda: service.plan_view(project_id))
        if plan_view is None:
            raise _error("not_found", "Project is not registered with the control plane", 404)
        return {
            "project_id": project_id,
            "plan": _plan_payload(plan_view),
            "execution": {"available": False},
        }

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    @app.get("/actions/{action_id}")
    def read_action(action_id: str, request: Request):
        _resolve_session(request)
        action_id = _bounded_id(action_id, "action id")
        view = _run(lambda: service.action_view(action_id))
        if view is None:
            raise _error("not_found", "Action not found", 404)
        return view

    @app.get("/actions/{action_id}/audit")
    def action_audit(action_id: str, request: Request):
        _resolve_session(request)
        action_id = _bounded_id(action_id, "action id")
        events = _run(lambda: service.audit_for_action(action_id))
        return {
            "action_id": action_id,
            "events": [
                {
                    "sequence": event.sequence,
                    "event_id": event.draft.event_id,
                    "event_type": event.event_type.value,
                    "occurred_at": event.draft.occurred_at.isoformat(),
                    "result_code": event.draft.result_code,
                }
                for event in events
            ],
        }

    def _confirm_and_prepare(action_id: str, session) -> dict:
        def _do():
            binding = service.confirm_action(session.session_id, action_id)
            action = service.lifecycle(action_id)
            if action is not None and action.state.value == "confirmed":
                service.capture_snapshot(session.session_id, action_id)
            view = service.action_view(action_id)
            return {"action_id": action_id, "confirmation_id": binding.confirmation_id, "state": view["state"] if view else None}

        return _run(_do)

    @app.post("/actions/{action_id}/confirm")
    async def confirm_action(action_id: str, request: Request):
        session = _resolve_session(request)
        _require_csrf(request)
        if not limiter_confirm.allow(session.session_id):
            raise _error("conflict", "Too many confirmation attempts; slow down", 429)
        action_id = _bounded_id(action_id, "action id")
        return _confirm_and_prepare(action_id, session)

    @app.post("/actions/{action_id}/execute")
    async def execute_action(action_id: str, request: Request):
        session = _resolve_session(request)
        _require_csrf(request)
        action_id = _bounded_id(action_id, "action id")

        def _do():
            result = service.execute_action(session.session_id, action_id)
            return {
                "action_id": result.action_id,
                "outcome": result.outcome.value,
                "lifecycle_state": result.lifecycle_state.value,
                "mutated_revision": result.mutated_revision,
                "verification_success": result.verification_success,
            }

        return _run(_do)

    @app.post("/actions/{action_id}/reconcile")
    async def reconcile_action(action_id: str, request: Request):
        session = _resolve_session(request)
        _require_csrf(request)
        action_id = _bounded_id(action_id, "action id")

        def _do():
            result = service.reconcile_action(session.session_id, action_id)
            return {
                "action_id": result.action_id,
                "outcome": result.outcome.value,
                "lifecycle_state": result.lifecycle_state.value,
            }

        return _run(_do)

    @app.post("/actions/{action_id}/rollback")
    async def request_rollback(action_id: str, request: Request):
        session = _resolve_session(request)
        _require_csrf(request)
        action_id = _bounded_id(action_id, "action id")

        def _do():
            decision = service.request_rollback(session.session_id, action_id)
            identity = decision.action_identity
            return {
                "decision_id": decision.decision_id,
                "allowed": decision.allowed,
                "rollback_action_id": identity.action_id if identity else None,
                "code": decision.code.value,
            }

        return _run(_do)

    # ------------------------------------------------------------------
    # Kill switch (staging only; production is permanently engaged)
    # ------------------------------------------------------------------

    @app.get("/kill-switch")
    def kill_switch_view(request: Request):
        _resolve_session(request)
        return _run(lambda: service.kill_switch_status())

    @app.post("/kill-switch/engage")
    async def engage_kill_switch(request: Request):
        session = _resolve_session(request)
        _require_csrf(request)
        payload = await _json_body(request)
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason or len(reason) > 256:
            raise _error("invalid_request", "A bounded reason is required", 422)
        return _run(lambda: service.engage_kill_switch(session.principal.subject, reason=reason))

    @app.post("/kill-switch/disengage")
    async def disengage_kill_switch(request: Request):
        session = _resolve_session(request)
        _require_csrf(request)
        payload = await _json_body(request)
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason or len(reason) > 256:
            raise _error("invalid_request", "A bounded reason is required", 422)
        return _run(lambda: service.disengage_kill_switch(session.principal.subject, reason=reason))

    # ------------------------------------------------------------------
    # Audit chain
    # ------------------------------------------------------------------

    @app.get("/audit/verify")
    def audit_verify(request: Request):
        _resolve_session(request)
        verification = _run(lambda: service.verify_audit_chain())
        return {
            "valid": verification.ok,
            "sequence": verification.events_checked,
            "error_sequence": verification.error_sequence,
        }

    # ------------------------------------------------------------------
    # Bounded error handling (never leak internals)
    # ------------------------------------------------------------------

    @app.exception_handler(ExecutionRefused)
    async def execution_refused_handler(request: Request, exc: ExecutionRefused):
        code_map = {
            "stale_plan": 409,
            "stale_action_version": 409,
            "lease_expired": 410,
            "lease_missing": 409,
            "stale_fencing_token": 409,
            "contract_expired": 410,
            "action_expired": 410,
            "confirmation_expired": 410,
            "confirmation_consumed": 409,
            "confirmation_missing": 404,
            "kill_switch_engaged": 423,
            "kill_switch_epoch_mismatch": 423,
        }
        status = code_map.get(exc.reason_code, 409)
        return HTTPException(status_code=status, detail={"error": "execution_refused", "message": str(exc)[:256]})

    return app


def run_operator_transport(service, *, host: str = "127.0.0.1", port: int, secure_cookies: bool = False) -> None:
    """Run the operator transport; refuses non-loopback binds fail-closed."""

    import uvicorn

    validate_bind_address(host)
    app = create_operator_app(service, bind=host, secure_cookies=secure_cookies)
    uvicorn.run(app, host=host, port=port, log_level="warning")
