"""MC-6.13 C5: authenticated dashboard proxy → canonical operator transport.

The dashboard is a THIN PROXY. These tests prove it owns no authority:

* auth/CSRF (1-7): the canonical owner session and canonical session-bound
  CSRF token are the only credentials; no anonymous fallback, no second
  implementation, epoch rotation invalidates everything.
* project confinement (8-10): strict identity, unknown project, and
  cross-project action replay all fail closed upstream.
* approval (11-15): the canonical authorize→confirm composition and the
  canonical ConfirmationStore remain the sole approval authority; the digest
  is opaque pass-through material.
* execute (16-22): the canonical C4 service consumes the confirmation,
  acquires the lease, builds the contract, and runs the final gate; the
  dashboard cannot bypass any of it and relays a fail-closed C6 gap exactly.
* status (23-26): GET-only read projection that cannot mutate or execute.
* security (27-34): source-level architectural bans on engines, stores,
  rollback, arbitrary paths/commands, subprocess, audit vocabulary, and
  duplicate auth/CSRF implementations.
* deep audit (A-P): the explicit browser attack scenarios.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aipm.capabilities.dashboard import operator_client as operator_client_module
from aipm.capabilities.dashboard import update_proxy_api as update_proxy_module
from aipm.capabilities.dashboard.operator_client import (
    AsgiOperatorTransportClient,
    OperatorResponse,
    OperatorTransportUnavailable,
    assert_allowed_route,
)
from aipm.capabilities.dashboard.update_proxy_api import DashboardUpdateProxyApi
from aipm.control_plane.transport import SESSION_COOKIE, create_operator_app
from aipm.dashboard.server import create_app

from tests.test_mc613_c4_composition import (
    OTHER_PROJECT_ID,
    PROJECT_ID,
    _harness,
    _register_project,
)
from tests.test_mc612_stage9_transport import SECRET, csrf_headers

PROXY_SOURCES = (
    "src/aipm/capabilities/dashboard/update_proxy_api.py",
    "src/aipm/capabilities/dashboard/operator_client.py",
)
SERVER_SOURCE = "src/aipm/dashboard/server.py"


class _StubReadApi:
    """Inert observation envelope for every unrelated read route."""

    def __getattr__(self, name: str):
        def handler(*args, **kwargs):
            return {"available": False, "status": "error", "error": "not under test", "observation": {"state": "error"}}

        return handler


def _dashboard(proxy: DashboardUpdateProxyApi) -> TestClient:
    stub = _StubReadApi()
    return TestClient(
        create_app(
            dashboard_api=stub,
            incidents_api=stub,
            notifications_api=stub,
            service_health_api=stub,
            server_api=stub,
            docker_api=stub,
            project_api=stub,
            systemd_api=stub,
            logs_api=stub,
            settings_api=stub,
            update_api=stub,
            update_proxy_api=proxy,
        )
    )


def _stack(tmp_path: Path, *, compose_runtime: bool = True, with_kill_switch: bool = False, register_other: bool = False):
    """Compose dashboard → ASGI client → canonical operator transport → C4.

    One shared canonical service instance: the dashboard proxies into the
    real canonical transport, so every canonical guarantee is exercised
    end-to-end rather than mocked.
    """

    service, plans, runtime_calls = _harness(
        tmp_path,
        compose_runtime=compose_runtime,
        planner_allow_list=frozenset({PROJECT_ID, OTHER_PROJECT_ID}) if register_other else None,
    )
    if register_other:
        _register_project(plans, OTHER_PROJECT_ID)
    if with_kill_switch:
        from aipm.control_plane.kill_switch import KillSwitchRegistry
        from aipm.control_plane.storage import SQLiteKillSwitchStore

        registry = KillSwitchRegistry(
            clock=service._clock,
            store=SQLiteKillSwitchStore(service._plans._db, audit=service._audit),
            audit=service._audit,
        )
        object.__setattr__(service, "_kill_switches", registry)
    operator_app = create_operator_app(service, bind="127.0.0.1")
    operator = TestClient(operator_app)
    proxy = DashboardUpdateProxyApi(AsgiOperatorTransportClient(operator_app))
    dashboard = _dashboard(proxy)
    return dashboard, operator, service, plans, runtime_calls


def _login(operator: TestClient, dashboard: TestClient) -> str:
    """Authenticate once canonically and hand the browser the same session."""

    assert operator.post("/login", json={"secret": SECRET}).status_code == 200
    session_view = operator.get("/session").json()
    dashboard.cookies.set(SESSION_COOKIE, operator.cookies.get(SESSION_COOKIE))
    return session_view["csrf_token"]


def _approve_url(project_id: str = PROJECT_ID) -> str:
    return f"/api/projects/{project_id}/update/approve"


def _execute_url(project_id: str = PROJECT_ID) -> str:
    return f"/api/projects/{project_id}/update/execute"


def _status_url(project_id: str = PROJECT_ID) -> str:
    return f"/api/projects/{project_id}/update/status"


def _approve(dashboard: TestClient, csrf: str, digest: str, *, key: str = "k1", project_id: str = PROJECT_ID):
    return dashboard.post(
        _approve_url(project_id),
        json={"update_plan_digest": digest, "idempotency_key": key},
        headers=csrf_headers(csrf),
    )


def _approved(tmp_path: Path, **kwargs):
    dashboard, operator, service, plans, calls = _stack(tmp_path, **kwargs)
    csrf = _login(operator, dashboard)
    digest = plans.read(PROJECT_ID).canonical_digest
    response = _approve(dashboard, csrf, digest)
    assert response.status_code == 200, response.json()
    return dashboard, operator, service, plans, calls, csrf, response.json()["update_approval"]


def _sources() -> dict[Path, str]:
    return {Path(path): Path(path).read_text(encoding="utf-8") for path in PROXY_SOURCES}


def _code_only(source: str) -> str:
    """Return executable code with comments and string literals removed.

    Architectural bans must test real dependencies, not prose: a docstring
    that explains which canonical authority owns confirmations is not an
    access path to it. Stripping comments and strings makes the assertion
    exactly "this identifier is never used in code".
    """

    import io
    import tokenize

    kept: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in {tokenize.COMMENT, tokenize.STRING}:
            continue
        kept.append(token.string)
    return "\n".join(kept)


def _code_sources() -> dict[Path, str]:
    return {path: _code_only(source) for path, source in _sources().items()}



# ---------------------------------------------------------------------------
# 1-3 Authentication
# ---------------------------------------------------------------------------


def test_1_unauthenticated_approval_is_rejected(tmp_path: Path):
    dashboard, _operator, _service, plans, _calls = _stack(tmp_path)
    digest = plans.read(PROJECT_ID).canonical_digest
    response = dashboard.post(
        _approve_url(),
        json={"update_plan_digest": digest, "idempotency_key": "k1"},
        headers=csrf_headers("anything"),
    )
    # The canonical transport owns the decision: no session cookie means the
    # canonical CSRF check cannot resolve a session, so the request dies
    # before authorization. Either canonical code is a hard rejection.
    assert response.status_code in {401, 403}
    assert response.json()["available"] is False
    assert plans.read(PROJECT_ID).revision == 1


def test_2_unauthenticated_execute_is_rejected(tmp_path: Path):
    dashboard, _operator, _service, plans, calls = _stack(tmp_path)
    response = dashboard.post(
        _execute_url(),
        json={"action_id": "a" * 32},
        headers=csrf_headers("anything"),
    )
    assert response.status_code in {401, 403}
    assert calls == []
    assert plans.read(PROJECT_ID).revision == 1


def test_3_unauthenticated_status_is_rejected(tmp_path: Path):
    dashboard, _operator, _service, _plans, _calls = _stack(tmp_path)
    response = dashboard.get(_status_url())
    # Canonical policy for the update status projection is authenticated-only.
    assert response.status_code == 401
    assert response.json()["error"] == "unauthenticated"


# ---------------------------------------------------------------------------
# 4-7 CSRF
# ---------------------------------------------------------------------------


def test_4_missing_csrf_is_rejected(tmp_path: Path):
    dashboard, operator, _service, plans, _calls = _stack(tmp_path)
    _login(operator, dashboard)
    digest = plans.read(PROJECT_ID).canonical_digest
    response = dashboard.post(_approve_url(), json={"update_plan_digest": digest, "idempotency_key": "k1"})
    assert response.status_code == 403
    assert response.json()["error"] == "csrf_failed"
    assert plans.read(PROJECT_ID).revision == 1


def test_5_invalid_csrf_is_rejected(tmp_path: Path):
    dashboard, operator, _service, plans, _calls = _stack(tmp_path)
    _login(operator, dashboard)
    digest = plans.read(PROJECT_ID).canonical_digest
    response = _approve(dashboard, "not-the-token", digest)
    assert response.status_code == 403
    assert response.json()["error"] == "csrf_failed"


def test_6_wrong_session_csrf_is_rejected(tmp_path: Path):
    dashboard, operator, service, plans, _calls = _stack(tmp_path)
    csrf = _login(operator, dashboard)
    # A second canonical session with its own token; replaying that token
    # against the first session's cookie must fail (session-bound tokens).
    other = service.login(SECRET)
    assert other.csrf_token != csrf
    digest = plans.read(PROJECT_ID).canonical_digest
    response = _approve(dashboard, other.csrf_token, digest)
    assert response.status_code == 403
    assert response.json()["error"] == "csrf_failed"
    assert plans.read(PROJECT_ID).revision == 1


def test_7_valid_csrf_reaches_the_canonical_service(tmp_path: Path):
    _dash, _op, _service, _plans, _calls, _csrf, approval = _approved(tmp_path)
    assert approval["allowed"] is True
    assert approval["approval"] == "confirmed"
    assert approval["confirmation_id"]
    assert approval["action_id"]


# ---------------------------------------------------------------------------
# 8-10 Project confinement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["short", "A" * 24, "z" * 24, "a" * 23, "a" * 25, "../../etc/passwd", "a" * 12 + "/" + "a" * 11],
)
def test_8_malformed_project_id_is_rejected(tmp_path: Path, bad: str):
    dashboard, operator, _service, _plans, _calls = _stack(tmp_path)
    csrf = _login(operator, dashboard)
    response = _approve(dashboard, csrf, "b" * 64, project_id=bad)
    assert response.status_code in {404, 422}
    if response.status_code == 422:
        assert response.json()["error"] == "invalid_request"


def test_9_unknown_project_is_rejected(tmp_path: Path):
    dashboard, operator, _service, _plans, _calls = _stack(tmp_path)
    csrf = _login(operator, dashboard)
    response = _approve(dashboard, csrf, "b" * 64, project_id=OTHER_PROJECT_ID)
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_10_cross_project_action_is_rejected(tmp_path: Path):
    dashboard, _operator, _service, plans, calls, csrf, approval = _approved(tmp_path, register_other=True)
    # The action belongs to PROJECT_ID; executing it under another project's
    # URL must fail closed in the canonical transport.
    response = dashboard.post(
        _execute_url(OTHER_PROJECT_ID),
        json={"action_id": approval["action_id"]},
        headers=csrf_headers(csrf),
    )
    assert response.status_code == 409
    assert response.json()["error"] == "confirmation_required"
    assert calls == []
    assert plans.read(PROJECT_ID).revision == 1


# ---------------------------------------------------------------------------
# 11-15 Approval
# ---------------------------------------------------------------------------


def test_11_dashboard_delegates_to_the_canonical_approval_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    dashboard, operator, service, plans, _calls = _stack(tmp_path)
    csrf = _login(operator, dashboard)
    seen: list[tuple] = []
    original = type(service).approve_update_plan

    def _record(self, session_id, **kwargs):
        seen.append((kwargs["target_id"], kwargs["presented_digest"], kwargs["idempotency_key"]))
        return original(self, session_id, **kwargs)

    monkeypatch.setattr(type(service), "approve_update_plan", _record)
    digest = plans.read(PROJECT_ID).canonical_digest
    assert _approve(dashboard, csrf, digest, key="k9").status_code == 200
    # Exactly one canonical composition call carrying exactly the bounded
    # values the browser supplied; the dashboard added nothing.
    assert seen == [(PROJECT_ID, digest, "k9")]


def test_12_canonical_confirmation_remains_the_sole_approval_authority(tmp_path: Path):
    _dash, _op, service, _plans, _calls, _csrf, approval = _approved(tmp_path)
    bindings = [
        binding
        for binding in service._confirmations.store.values()
        if binding.action_id == approval["action_id"]
    ]
    # The approval record lives only in the canonical ConfirmationStore.
    assert len(bindings) == 1
    assert bindings[0].confirmation_id == approval["confirmation_id"]
    # The proxy's entire state is the injected transport client: no approval,
    # confirmation, session, lease, or lock state of its own.
    assert DashboardUpdateProxyApi.__slots__ == ("client",)



def test_13_digest_is_passed_through_without_local_authorization(tmp_path: Path):
    dashboard, operator, service, plans, _calls = _stack(tmp_path)
    csrf = _login(operator, dashboard)
    digest = plans.read(PROJECT_ID).canonical_digest
    response = _approve(dashboard, csrf, digest)
    assert response.status_code == 200
    decision = service._actions.get_decision(response.json()["update_approval"]["decision_id"])
    # The canonical decision — not the dashboard — binds the digest.
    assert dict(decision.request.metadata)["update_plan_digest"] == digest
    for source in _sources().values():
        for forbidden in ("UpdatePlanIdentity", "hashlib", "sha256(", "hexdigest", "canonical_digest ==", "digest() =="):
            assert forbidden not in source, forbidden


def test_14_stale_digest_returns_a_safe_conflict(tmp_path: Path):
    dashboard, operator, _service, plans, calls = _stack(tmp_path)
    csrf = _login(operator, dashboard)
    response = _approve(dashboard, csrf, "b" * 64)
    assert response.status_code == 409
    assert response.json()["error"] == "stale_plan"
    assert calls == []
    assert plans.read(PROJECT_ID).revision == 1


def test_15_duplicate_idempotency_follows_canonical_semantics(tmp_path: Path):
    dashboard, operator, _service, plans, _calls = _stack(tmp_path)
    csrf = _login(operator, dashboard)
    digest = plans.read(PROJECT_ID).canonical_digest
    first = _approve(dashboard, csrf, digest, key="same").json()["update_approval"]
    second = _approve(dashboard, csrf, digest, key="same").json()["update_approval"]
    # Canonical idempotent replay: same action, same confirmation, no second
    # approval record anywhere.
    assert first["action_id"] == second["action_id"]
    assert first["confirmation_id"] == second["confirmation_id"]


# ---------------------------------------------------------------------------
# 16-22 Execute
# ---------------------------------------------------------------------------


def test_16_execute_delegates_to_the_canonical_c4_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    dashboard, _operator, service, plans, calls, csrf, approval = _approved(tmp_path)
    seen: list[str] = []
    original = type(service).run_approved_update

    def _record(self, session_id, *, action_id, now=None):
        seen.append(action_id)
        return original(self, session_id, action_id=action_id, now=now)

    monkeypatch.setattr(type(service), "run_approved_update", _record)

    response = dashboard.post(
        _execute_url(),
        json={"action_id": approval["action_id"]},
        headers=csrf_headers(csrf),
    )
    assert response.status_code == 200
    body = response.json()["update_execution"]
    assert seen == [approval["action_id"]]
    assert body["executed"] is True
    assert body["lifecycle_state"] == "verified_success"
    assert len(calls) == 1
    assert plans.read(PROJECT_ID).revision == 2


def test_17_dashboard_never_constructs_an_execution_contract():
    for path, source in _code_sources().items():
        for forbidden in ("ExecutionContract", "EXECUTION_CONTRACT_VERSION", "ExecutorCapability", "contract_digest", "fencing_token"):
            assert forbidden not in source, (path, forbidden)


def test_18_dashboard_never_consumes_a_confirmation(tmp_path: Path):
    from aipm.control_plane.models import ConfirmationState

    _dash, _op, service, _plans, _calls, _csrf, approval = _approved(tmp_path)
    binding = next(b for b in service._confirmations.store.values() if b.action_id == approval["action_id"])
    # Approval alone never consumes: only canonical execution does.
    assert binding.state is ConfirmationState.CONFIRMED
    for path, source in _code_sources().items():
        for forbidden in ("ConfirmationStore", "ConfirmationBinding", "OwnerConfirmationService", "consume(", "_confirmations"):
            assert forbidden not in source, (path, forbidden)


def test_19_dashboard_never_acquires_a_lease(tmp_path: Path):
    _dash, _op, service, _plans, _calls, _csrf, approval = _approved(tmp_path)
    # No lease exists from approval alone; the canonical service grants it
    # inside execution.
    assert service._actions.active_lease(approval["action_id"], now=service._clock()) is None
    for path, source in _code_sources().items():
        for forbidden in ("acquire_lease", "active_lease", "fencing", "lease_id"):
            assert forbidden not in source, (path, forbidden)


def test_20_execution_cannot_bypass_the_final_execution_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from aipm.control_plane.gate import FinalExecutionGate

    dashboard, _operator, _service, plans, calls, csrf, approval = _approved(tmp_path)
    evaluated: list[str] = []
    original = FinalExecutionGate.evaluate

    def _spy(self, contract, *, now=None):
        evaluated.append(contract.action_id)
        return original(self, contract, now=now)

    monkeypatch.setattr(FinalExecutionGate, "evaluate", _spy)
    response = dashboard.post(
        _execute_url(),
        json={"action_id": approval["action_id"]},
        headers=csrf_headers(csrf),
    )
    assert response.status_code == 200

    # The gate ran for exactly this action before the mutation.
    assert evaluated == [approval["action_id"]]
    assert len(calls) == 1
    assert plans.read(PROJECT_ID).revision == 2
    for path, source in _code_sources().items():
        assert "FinalExecutionGate" not in source, path


def test_21_c6_not_composed_execution_fails_safely(tmp_path: Path):
    dashboard, _operator, _service, plans, calls, csrf, approval = _approved(tmp_path, compose_runtime=False)
    response = dashboard.post(
        _execute_url(),
        json={"action_id": approval["action_id"]},
        headers=csrf_headers(csrf),
    )
    # The canonical service fails closed on the missing C6 runtime port; the
    # dashboard relays a bounded error and adds no fallback path.
    assert response.status_code in {409, 500, 503}
    body = response.json()
    assert body["available"] is False
    assert body["error"] in {"conflict", "control_plane_unavailable", "internal_error", "upstream_rejected"}
    assert calls == []
    for path, source in _code_sources().items():
        for forbidden in ("UpdateEngine", "fallback", "retry"):
            assert forbidden not in source, (path, forbidden)


def test_22_successful_execution_result_is_rendered_safely(tmp_path: Path):
    dashboard, _operator, _service, _plans, _calls, csrf, approval = _approved(tmp_path)
    response = dashboard.post(
        _execute_url(),
        json={"action_id": approval["action_id"]},
        headers=csrf_headers(csrf),
    )
    payload = response.json()
    assert set(payload) == {"available", "status", "error", "update_execution"}
    assert set(payload["update_execution"]) == {"action_id", "executed", "outcome", "lifecycle_state"}
    from aipm.capabilities.dashboard.safety import assert_safe_payload

    assert_safe_payload(payload)
    text = response.text
    for forbidden in ("/home/", "/tmp/", "Traceback", "SELECT ", "sqlite", SESSION_COOKIE, csrf):
        assert forbidden not in text, forbidden


# ---------------------------------------------------------------------------
# 23-26 Status
# ---------------------------------------------------------------------------


def test_23_status_is_get_only(tmp_path: Path):
    dashboard, operator, _service, _plans, _calls = _stack(tmp_path)
    _login(operator, dashboard)
    routes = {getattr(route, "path", None): getattr(route, "methods", None) for route in dashboard.app.routes}
    assert routes["/api/projects/{project_id}/update/status"] == {"GET"}
    assert routes["/api/projects/{project_id}/update/approve"] == {"POST"}
    assert routes["/api/projects/{project_id}/update/execute"] == {"POST"}
    assert dashboard.post(_status_url()).status_code == 405


def test_24_status_does_not_mutate_state(tmp_path: Path):
    dashboard, _operator, service, plans, calls, _csrf, approval = _approved(tmp_path)
    before_plan = plans.read(PROJECT_ID)
    before_action = service._actions.get_action(approval["action_id"])
    before_audit = len(service.audit_events(limit=4096))
    assert dashboard.get(_status_url()).status_code == 200
    after_action = service._actions.get_action(approval["action_id"])
    assert plans.read(PROJECT_ID).revision == before_plan.revision
    assert plans.read(PROJECT_ID).canonical_digest == before_plan.canonical_digest
    assert after_action.state is before_action.state
    assert after_action.version == before_action.version
    assert len(service.audit_events(limit=4096)) == before_audit
    assert calls == []


def test_25_status_is_bounded_and_sanitized(tmp_path: Path):
    from aipm.capabilities.dashboard.safety import assert_safe_payload

    dashboard, operator, _service, _plans, _calls = _stack(tmp_path)
    _login(operator, dashboard)
    payload = dashboard.get(_status_url()).json()
    assert set(payload) == {"available", "status", "error", "update_status"}
    assert set(payload["update_status"]) == {"project_id", "plan", "execution"}
    assert set(payload["update_status"]["plan"]) == {"target_id", "environment", "revision", "enabled", "canonical_digest"}
    assert set(payload["update_status"]["execution"]) == {"available"}
    assert_safe_payload(payload)


def test_26_status_cannot_invoke_execution(tmp_path: Path):
    dashboard, _operator, _service, plans, calls, _csrf, _approval = _approved(tmp_path)
    for _ in range(3):
        assert dashboard.get(_status_url()).status_code == 200
    assert calls == []
    assert plans.read(PROJECT_ID).revision == 1
    source = Path(SERVER_SOURCE).read_text(encoding="utf-8")
    status_handler = source.split("async def project_update_status", 1)[1].split("@app.get", 1)[0]
    for forbidden in ("execute", "approve", "csrf_token"):
        assert forbidden not in status_handler, forbidden


# ---------------------------------------------------------------------------
# 27-34 Architectural boundaries (source-level)
# ---------------------------------------------------------------------------


def test_27_no_engine_or_provider_access_from_the_dashboard_proxy():
    for path, source in _code_sources().items():
        for forbidden in (
            "UpdateEngine",
            "UpdatePlanner",
            "GitProvider",
            "ComposeProvider",
            "DockerProvider",
            "from aipm.services",
            "import aipm.services",
            "from aipm.providers",
            "import aipm.providers",
        ):
            assert forbidden not in source, (path, forbidden)


def test_28_no_confirmation_or_approval_store_access():
    for path, source in _code_sources().items():
        for forbidden in (
            "UpdateApprovalService",
            "UpdateApprovalRecord",
            "UpdateApprovalStore",
            "InMemoryUpdateApprovalStore",
            "UpdateFlightControl",
            "SQLiteActionRepository",
            "ControlPlaneDatabase",
            "sqlite3",
        ):
            assert forbidden not in source, (path, forbidden)


def test_29_no_rollback_route_exists(tmp_path: Path):
    dashboard, operator, _service, _plans, _calls = _stack(tmp_path)
    _login(operator, dashboard)
    paths = {getattr(route, "path", "") for route in dashboard.app.routes}
    assert not any("rollback" in path or "restore" in path for path in paths)
    for path, source in _code_sources().items():
        for forbidden in ("rollback", "restore", "snapshot"):
            assert forbidden not in source.lower(), (path, forbidden)
    # The canonical rollback verb is unreachable through the proxy allow-list.
    with pytest.raises(OperatorTransportUnavailable):
        assert_allowed_route("POST", f"/actions/{'a' * 32}/rollback")


def test_30_no_arbitrary_command_or_path_fields_are_accepted(tmp_path: Path):
    dashboard, operator, _service, plans, calls = _stack(tmp_path)
    csrf = _login(operator, dashboard)
    digest = plans.read(PROJECT_ID).canonical_digest
    hostile_bodies = (
        {"update_plan_digest": digest, "idempotency_key": "k", "project_path": "/home/ubuntu/aipm"},
        {"update_plan_digest": digest, "idempotency_key": "k", "command": "rm -rf /"},
        {"update_plan_digest": digest, "idempotency_key": "k", "archive_path": "/tmp/x.tar"},
        {"update_plan_digest": digest, "idempotency_key": "k", "snapshot_path": "/tmp/s"},
    )
    for body in hostile_bodies:
        response = dashboard.post(_approve_url(), json=body, headers=csrf_headers(csrf))
        assert response.status_code == 422, body
        assert response.json()["error"] == "invalid_request"
    hostile_execute = (
        {"action_id": "a" * 32, "command": "ls"},
        {"action_id": "a" * 32, "project_path": "/home/ubuntu"},
        {"action_id": "../../etc/passwd"},
        {"action_id": "a/b"},
        {"plan": {"title": "x"}},
        {"update_plan_digest": digest},
    )
    for body in hostile_execute:
        response = dashboard.post(_execute_url(), json=body, headers=csrf_headers(csrf))
        assert response.status_code == 422, body
    assert calls == []
    assert plans.read(PROJECT_ID).revision == 1


def test_31_no_subprocess_or_process_execution_in_the_proxy():
    for path, source in _code_sources().items():
        for forbidden in ("subprocess", "os.system", "shell=True", "popen", "import docker", "os.exec"):
            assert forbidden not in source.lower(), (path, forbidden)


def test_32_no_second_audit_vocabulary():
    from aipm.control_plane.audit import builders as audit_builders

    builder_names = {name for name in dir(audit_builders) if callable(getattr(audit_builders, name)) and not name.startswith("_")}
    for path, source in _code_sources().items():
        for name in builder_names:
            assert f"{name}(" not in source, (path, name)
        for forbidden in ("AuditEventDraft", "AuditLedger", "append_in_transaction", "audit_drafts"):
            assert forbidden not in source, (path, forbidden)


def test_33_no_second_authentication_implementation():
    for path, source in _code_sources().items():
        for forbidden in (
            "Argon2id",
            "OwnerAuthenticator",
            "OwnerSessionStore",
            "OwnerPrincipal",
            "verify_csrf",
            "argon2",
            "jwt",
            "oauth",
            "password",
            "secret",
        ):
            assert forbidden not in source.lower().replace("session_cookie", "").replace("csrf_header", ""), (path, forbidden)
    # The canonical cookie name is imported, never redeclared.
    client_source = inspect.getsource(operator_client_module)
    assert "from aipm.control_plane.transport import SESSION_COOKIE" in client_source
    assert 'SESSION_COOKIE = "' not in client_source


def test_34_no_second_csrf_implementation():
    for path, source in _code_sources().items():
        for forbidden in ("compare_digest", "secrets.token", "hmac", "csrf_token =", "generate_csrf", "new_csrf"):
            assert forbidden not in source, (path, forbidden)
    server_source = Path(SERVER_SOURCE).read_text(encoding="utf-8")
    # The dashboard forwards the canonical header; it never verifies it.
    assert "verify_csrf" not in server_source
    assert "compare_digest" not in server_source
    assert 'CSRF_HEADER = "X-CSRF-Token"' in server_source


# ---------------------------------------------------------------------------
# Deep security audit: explicit browser attack scenarios (A-P)
# ---------------------------------------------------------------------------


def test_a_digest_for_another_project_is_refused(tmp_path: Path):
    dashboard, _operator, _service, plans, calls, csrf, _approval = _approved(tmp_path, register_other=True)
    other_digest = plans.read(OTHER_PROJECT_ID).canonical_digest
    # A valid digest, but not this project's: the canonical service compares
    # against the authoritative digest for the URL target and fails closed.
    response = _approve(dashboard, csrf, other_digest, key="cross")
    assert response.status_code == 409
    assert response.json()["error"] == "stale_plan"
    assert calls == []


def test_b_execute_for_another_projects_action_is_refused(tmp_path: Path):
    dashboard, _operator, _service, plans, calls, csrf, approval = _approved(tmp_path, register_other=True)
    response = dashboard.post(
        _execute_url(OTHER_PROJECT_ID),
        json={"action_id": approval["action_id"]},
        headers=csrf_headers(csrf),
    )
    assert response.status_code == 409
    assert calls == []
    assert plans.read(PROJECT_ID).revision == 1
    assert plans.read(OTHER_PROJECT_ID).revision == 1


def test_c_replayed_execution_reference_never_re_executes(tmp_path: Path):
    dashboard, _operator, _service, plans, calls, csrf, approval = _approved(tmp_path)
    body = {"action_id": approval["action_id"]}
    first = dashboard.post(_execute_url(), json=body, headers=csrf_headers(csrf))
    assert first.status_code == 200
    assert first.json()["update_execution"]["executed"] is True
    replay = dashboard.post(_execute_url(), json=body, headers=csrf_headers(csrf))
    assert replay.status_code == 200
    # Canonical terminal replay: the durable outcome stands, the runtime is
    # never fired twice, and the plan is mutated exactly once.
    assert replay.json()["update_execution"]["executed"] is False
    assert len(calls) == 1
    assert plans.read(PROJECT_ID).revision == 2


def test_d_digest_changed_after_viewing_the_plan_is_refused(tmp_path: Path):
    dashboard, operator, service, plans, calls = _stack(tmp_path)
    csrf = _login(operator, dashboard)
    viewed_digest = plans.read(PROJECT_ID).canonical_digest
    # The plan drifts after the operator reviewed it (TOCTOU).
    plans.update(PROJECT_ID, expected_revision=plans.read(PROJECT_ID).revision, fields={"title": "Drifted title"}, now=service._clock())
    response = _approve(dashboard, csrf, viewed_digest, key="drift")
    assert response.status_code == 409
    assert response.json()["error"] == "stale_plan"
    assert calls == []


def test_e_removing_the_csrf_token_is_refused(tmp_path: Path):
    dashboard, _operator, _service, plans, calls, _csrf, approval = _approved(tmp_path)
    response = dashboard.post(_execute_url(), json={"action_id": approval["action_id"]})
    assert response.status_code == 403
    assert response.json()["error"] == "csrf_failed"
    assert calls == []
    assert plans.read(PROJECT_ID).revision == 1


def test_f_csrf_token_from_another_session_is_refused(tmp_path: Path):
    dashboard, _operator, service, plans, calls, _csrf, approval = _approved(tmp_path)
    other = service.login(SECRET)
    response = dashboard.post(
        _execute_url(),
        json={"action_id": approval["action_id"]},
        headers=csrf_headers(other.csrf_token),
    )
    assert response.status_code == 403
    assert calls == []
    assert plans.read(PROJECT_ID).revision == 1


def test_g_auth_epoch_rotation_invalidates_the_browser_session(tmp_path: Path):
    dashboard, _operator, service, plans, calls, csrf, approval = _approved(tmp_path)
    # Canonical global revocation: every previously issued session dies.
    service.rotate_credentials()
    execute = dashboard.post(
        _execute_url(),
        json={"action_id": approval["action_id"]},
        headers=csrf_headers(csrf),
    )
    assert execute.status_code in {401, 403}
    assert dashboard.get(_status_url()).status_code == 401
    assert calls == []
    assert plans.read(PROJECT_ID).revision == 1


def test_h_arbitrary_filesystem_paths_are_refused(tmp_path: Path):
    dashboard, operator, _service, plans, calls = _stack(tmp_path)
    csrf = _login(operator, dashboard)
    digest = plans.read(PROJECT_ID).canonical_digest
    for body in (
        {"update_plan_digest": digest, "idempotency_key": "k", "path": "/home/ubuntu/aipm"},
        {"update_plan_digest": "/etc/passwd", "idempotency_key": "k"},
        {"update_plan_digest": digest, "idempotency_key": "../../etc/passwd"},
    ):
        assert dashboard.post(_approve_url(), json=body, headers=csrf_headers(csrf)).status_code == 422
    for body in ({"action_id": "/home/ubuntu/aipm"}, {"action_id": "..%2f..%2fetc"}, {"action_id": "\\\\server\\share"}):
        assert dashboard.post(_execute_url(), json=body, headers=csrf_headers(csrf)).status_code == 422
    assert calls == []
    assert plans.read(PROJECT_ID).revision == 1


def test_i_arbitrary_command_or_runtime_metadata_is_refused(tmp_path: Path):
    dashboard, operator, _service, plans, calls = _stack(tmp_path)
    csrf = _login(operator, dashboard)
    digest = plans.read(PROJECT_ID).canonical_digest
    for body in (
        {"update_plan_digest": digest, "idempotency_key": "k", "cmd": "systemctl restart aipm"},
        {"update_plan_digest": digest, "idempotency_key": "k", "env": {"PATH": "/tmp"}},
        {"update_plan_digest": digest, "idempotency_key": "k", "runtime": "engine"},
        {"update_plan_digest": digest, "idempotency_key": "k", "fields": {"title": "x"}},
    ):
        assert dashboard.post(_approve_url(), json=body, headers=csrf_headers(csrf)).status_code == 422
    assert calls == []
    assert plans.read(PROJECT_ID).revision == 1


def test_j_crafted_rollback_requests_have_no_route(tmp_path: Path):
    dashboard, operator, _service, plans, calls = _stack(tmp_path)
    csrf = _login(operator, dashboard)
    digest = plans.read(PROJECT_ID).canonical_digest
    for path in (
        f"/api/projects/{PROJECT_ID}/update/rollback",
        f"/api/projects/{PROJECT_ID}/update/restore",
        f"/api/projects/{PROJECT_ID}/rollback",
        f"/api/projects/{PROJECT_ID}/update/execute/rollback",
    ):
        response = dashboard.post(path, json={"action_id": "a" * 32}, headers=csrf_headers(csrf))
        assert response.status_code in {404, 405}, path
    # A rollback intent smuggled into the accepted bodies is rejected too.
    assert dashboard.post(
        _approve_url(),
        json={"update_plan_digest": digest, "idempotency_key": "k", "rollback": True},
        headers=csrf_headers(csrf),
    ).status_code == 422
    assert calls == []


def test_k_repeated_calls_cannot_bypass_the_canonical_rate_limit(tmp_path: Path):
    dashboard, operator, _service, plans, calls = _stack(tmp_path)
    csrf = _login(operator, dashboard)
    digest = plans.read(PROJECT_ID).canonical_digest
    statuses = [
        dashboard.post(
            _approve_url(),
            json={"update_plan_digest": digest, "idempotency_key": f"k{index}"},
            headers=csrf_headers(csrf),
        ).status_code
        for index in range(45)
    ]
    # The canonical limiter (30/min) is still the effective boundary through
    # the proxy: the excess calls are refused upstream.
    assert 429 in statuses
    assert statuses.count(429) >= 10
    # And the proxy adds no limiter of its own that could mask or replace it.
    for path, source in _code_sources().items():
        for forbidden in ("RateLimiter", "_RateLimiter", "deque", "monotonic"):
            assert forbidden not in source, (path, forbidden)


def test_l_engaged_kill_switch_blocks_the_dashboard_path(tmp_path: Path):
    dashboard, operator, service, plans, calls = _stack(tmp_path, with_kill_switch=True)
    csrf = _login(operator, dashboard)
    digest = plans.read(PROJECT_ID).canonical_digest
    # The staging switch is seeded ENGAGED; approval must fail closed.
    response = _approve(dashboard, csrf, digest, key="ks")
    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"
    assert calls == []
    assert plans.read(PROJECT_ID).revision == 1


def test_m_confirmation_cannot_be_bypassed(tmp_path: Path):
    dashboard, operator, service, plans, calls = _stack(tmp_path)
    csrf = _login(operator, dashboard)
    # Execute an action that was never approved/confirmed through any path.
    for candidate in ("a" * 64, "deadbeef" * 8, "0" * 32):
        response = dashboard.post(
            _execute_url(),
            json={"action_id": candidate},
            headers=csrf_headers(csrf),
        )
        assert response.status_code == 409, candidate
        assert response.json()["error"] == "confirmation_required"
    assert calls == []
    assert plans.read(PROJECT_ID).revision == 1


def test_n_dashboard_never_calls_the_update_engine(tmp_path: Path):
    dashboard, _operator, _service, _plans, calls, csrf, approval = _approved(tmp_path)
    response = dashboard.post(
        _execute_url(),
        json={"action_id": approval["action_id"]},
        headers=csrf_headers(csrf),
    )
    assert response.status_code == 200
    # The engine-facing runtime port was invoked by the canonical service (via
    # the composition root), exactly once, and never by the dashboard.
    assert len(calls) == 1
    server_source = _code_only(Path(SERVER_SOURCE).read_text(encoding="utf-8"))
    for path, source in list(_code_sources().items()) + [(Path(SERVER_SOURCE), server_source)]:
        for forbidden in ("UpdateEngine", "engine", "GitProvider", "ComposeProvider", "UpdateExecutionBinding"):
            assert forbidden not in source, (path, forbidden)


def test_o_dashboard_creates_no_second_approval_state(tmp_path: Path):
    dashboard, _operator, service, _plans, _calls, csrf, approval = _approved(tmp_path)
    dashboard.post(_execute_url(), json={"action_id": approval["action_id"]}, headers=csrf_headers(csrf))
    # Approval/confirmation counts are exactly the canonical ones.
    bindings = [b for b in service._confirmations.store.values() if b.action_id == approval["action_id"]]
    assert len(bindings) == 1
    proxy = dashboard.app.state if hasattr(dashboard.app, "state") else None
    assert not hasattr(proxy, "approvals")
    for path, source in _code_sources().items():
        for forbidden in ("self._records", "self._store", "self._approvals", "self._sessions", "self._locks", "Lock(", "global "):
            assert forbidden not in source, (path, forbidden)


def test_p_no_secret_session_or_csrf_material_is_logged_or_returned(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    import logging

    dashboard, _operator, _service, plans, _calls, csrf, approval = _approved(tmp_path)
    cookie = dashboard.cookies.get(SESSION_COOKIE)
    with caplog.at_level(logging.DEBUG):
        execute = dashboard.post(_execute_url(), json={"action_id": approval["action_id"]}, headers=csrf_headers(csrf))
        status = dashboard.get(_status_url())
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (SECRET, csrf, cookie):
        assert secret not in logged
        assert secret not in execute.text
        assert secret not in status.text
    # No log/print statements exist in the proxy at all.
    for path, source in _code_sources().items():
        for forbidden in ("logging", "logger", "print(", "warnings"):
            assert forbidden not in source, (path, forbidden)


# ---------------------------------------------------------------------------
# Proxy-boundary unit properties
# ---------------------------------------------------------------------------


def test_allow_list_rejects_every_non_update_canonical_route():
    for method, path in (
        ("POST", "/login"),
        ("POST", "/logout"),
        ("GET", "/session"),
        ("POST", f"/actions/{'a' * 32}/confirm"),
        ("POST", f"/actions/{'a' * 32}/execute"),
        ("POST", f"/actions/{'a' * 32}/reconcile"),
        ("POST", f"/actions/{'a' * 32}/rollback"),
        ("POST", "/kill-switch/engage"),
        ("POST", "/kill-switch/disengage"),
        ("POST", f"/plans/{PROJECT_ID}/authorize"),
        ("GET", "/audit/verify"),
        ("GET", f"/plans/{PROJECT_ID}"),
        ("POST", f"/updates/{PROJECT_ID}/status"),
        ("GET", f"/updates/{PROJECT_ID}/approval"),
        ("POST", f"/updates/{'A' * 24}/approval"),
        ("POST", f"/updates/{PROJECT_ID}/approval/../../login"),
    ):
        with pytest.raises(OperatorTransportUnavailable):
            assert_allowed_route(method, path)


def test_allow_list_admits_only_the_three_update_verbs_and_action_read():
    assert_allowed_route("POST", f"/updates/{PROJECT_ID}/approval")
    assert_allowed_route("POST", f"/updates/{PROJECT_ID}/execute")
    assert_allowed_route("GET", f"/updates/{PROJECT_ID}/status")
    assert_allowed_route("GET", f"/actions/{'a' * 32}")


def test_uncomposed_proxy_fails_closed_on_every_verb():
    import asyncio

    proxy = DashboardUpdateProxyApi()
    body = b'{"update_plan_digest": "%s", "idempotency_key": "k"}' % (b"a" * 64)
    approve = asyncio.run(proxy.approve(PROJECT_ID, body=body, session_cookie="s", csrf_token="c"))
    execute = asyncio.run(proxy.execute(PROJECT_ID, body=b'{"action_id": "abc"}', session_cookie="s", csrf_token="c"))
    status = asyncio.run(proxy.status(PROJECT_ID, session_cookie="s"))
    for result in (approve, execute, status):
        assert result.status == 503
        assert result.payload["available"] is False
        assert result.payload["error"] == "control_plane_unavailable"


def test_unknown_upstream_error_codes_are_collapsed():
    import asyncio

    class _HostileClient:
        session_cookie_name = SESSION_COOKIE

        async def request(self, method, path, *, session_cookie, csrf_token=None, json_body=None):
            return OperatorResponse(
                status=409,
                payload={"detail": {"error": "sqlite: /home/ubuntu/aipm/control_plane.db locked", "message": "Traceback..."}},
            )

    proxy = DashboardUpdateProxyApi(_HostileClient())
    result = asyncio.run(proxy.execute(PROJECT_ID, body=b'{"action_id": "abc"}', session_cookie="s", csrf_token="c"))
    assert result.status == 409
    assert result.payload["error"] == "upstream_rejected"
    assert "sqlite" not in str(result.payload)
    assert "/home/" not in str(result.payload)


def test_hostile_upstream_payload_cannot_smuggle_fields():
    import asyncio

    class _SmugglingClient:
        session_cookie_name = SESSION_COOKIE

        async def request(self, method, path, *, session_cookie, csrf_token=None, json_body=None):
            return OperatorResponse(
                status=200,
                payload={
                    "action_id": "a" * 32,
                    "executed": True,
                    "outcome": "verification_succeeded",
                    "lifecycle_state": "verified_success",
                    "csrf_token": "leaked",
                    "session_id": "leaked",
                    "project_path": "/home/ubuntu/aipm",
                    "nested": {"secret": "leaked"},
                },
            )

    proxy = DashboardUpdateProxyApi(_SmugglingClient())
    result = asyncio.run(proxy.execute(PROJECT_ID, body=b'{"action_id": "abc"}', session_cookie="s", csrf_token="c"))
    assert set(result.payload["update_execution"]) == {"action_id", "executed", "outcome", "lifecycle_state"}
    assert "leaked" not in str(result.payload)
    assert "/home/" not in str(result.payload)


def test_upstream_exception_never_escapes_as_transport_detail():
    import asyncio

    class _ExplodingClient:
        session_cookie_name = SESSION_COOKIE

        async def request(self, method, path, *, session_cookie, csrf_token=None, json_body=None):
            raise RuntimeError("connection to /run/aipm/operator.sock failed: Traceback")

    proxy = DashboardUpdateProxyApi(_ExplodingClient())
    result = asyncio.run(proxy.status(PROJECT_ID, session_cookie="s"))
    assert result.status == 503
    assert result.payload["error"] == "control_plane_unavailable"
    assert "sock" not in str(result.payload)
    assert "Traceback" not in str(result.payload)


def test_oversized_and_malformed_bodies_are_bounded(tmp_path: Path):
    import asyncio

    proxy = DashboardUpdateProxyApi(object())
    oversized = b'{"update_plan_digest": "' + b"a" * 8192 + b'", "idempotency_key": "k"}'
    for body in (None, b"", b"not-json", b"[]", b'"string"', b"null", oversized, b"{}"):
        result = asyncio.run(proxy.approve(PROJECT_ID, body=body, session_cookie="s", csrf_token="c"))
        assert result.status == 422
        assert result.payload["error"] == "invalid_request"


def test_operator_client_configuration_is_immutable_and_bounded():
    app_stub = object()
    client = AsgiOperatorTransportClient(app_stub)
    assert client.session_cookie_name == SESSION_COOKIE
    with pytest.raises(AttributeError):
        client.session_cookie_name = "other_cookie"
    with pytest.raises(ValueError):
        AsgiOperatorTransportClient(None)
    for bad_timeout in (0, -1, 120):
        with pytest.raises(ValueError):
            AsgiOperatorTransportClient(app_stub, timeout=bad_timeout)


def test_dashboard_default_composition_is_uncomposed(tmp_path: Path):
    from aipm.capabilities.dashboard.context import MissionControlContext

    stub = _StubReadApi()
    dashboard = _dashboard(DashboardUpdateProxyApi())
    response = dashboard.post(
        _approve_url(),
        json={"update_plan_digest": "a" * 64, "idempotency_key": "k"},
        headers=csrf_headers("token"),
    )
    # A dashboard composed without an operator client can never mutate.
    assert response.status_code == 503
    assert response.json()["error"] == "control_plane_unavailable"
    assert "update_proxy" in MissionControlContext.__annotations__
