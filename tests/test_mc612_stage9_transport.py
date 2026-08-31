"""Shot 9 (authenticated localhost operator transport) tests.

Covers: binding safety, login/session/cookie semantics, CSRF enforcement on
every state-changing verb, the full HTTP success and rollback flows, bounded
errors, and the transport's structural isolation (no SQL, no providers, no
UpdateEngine, no subprocess/socket).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aipm.control_plane.audit import SQLiteAuditLedger
from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.models import LifecycleState
from aipm.control_plane.owner_auth import Argon2idVerifier, OwnerAuthenticator
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import AuthorizationPolicy
from aipm.control_plane.project_plan import Environment, ProjectPlan
from aipm.control_plane.service import OwnerControlPlaneService
from aipm.control_plane.session import OwnerSessionStore
from aipm.control_plane.storage import (
    ControlPlaneDatabase,
    SQLiteActionRepository,
    SQLiteProjectPlanStore,
)
from aipm.control_plane.transport import create_operator_app, validate_bind_address

VERIFIER = "$argon2id$v=19$m=65536,t=2,p=1$c3RhZ2UzLXNhbHQtMTIzNA$zho28DBNr2G2cGbxzr0Dl6AKwhbd8hEeTkti1pn7TW0"
SECRET = "test-owner-secret"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def __call__(self) -> datetime:
        return self.value


def db_path(tmp_path: Path) -> Path:
    return tmp_path / "control_plane.db"


def build_transport(tmp_path: Path, *, clock=None, with_kill_switch: bool = False):
    clock = clock or _Clock(NOW)
    db = ControlPlaneDatabase(db_path(tmp_path), clock=clock)
    targets = {"project-demo"}
    ledger = SQLiteAuditLedger(db)
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=clock)
    sessions = OwnerSessionStore(clock=clock)
    policy = AuthorizationPolicy(policy_version="policy-v1", allowed_scopes=frozenset({("project-demo", "staging")}))
    confirmations = OwnerConfirmationService(clock=clock)
    plans = SQLiteProjectPlanStore(db)
    plans.create(ProjectPlan.create(target_id="project-demo", environment=Environment.STAGING, title="Old title", objective="Objective", now=NOW))
    planner = PlanOnlyPlanner(clock=clock, target_allow_list=targets)
    actions = SQLiteActionRepository(db, audit=ledger)
    kill_switches = None
    if with_kill_switch:
        from aipm.control_plane.kill_switch import KillSwitchRegistry
        from aipm.control_plane.storage import SQLiteKillSwitchStore

        kill_switches = KillSwitchRegistry(clock=clock, store=SQLiteKillSwitchStore(db, audit=ledger), audit=ledger)
    service = OwnerControlPlaneService(
        authenticator=authenticator,
        sessions=sessions,
        policy=policy,
        confirmations=confirmations,
        plans=plans,
        planner=planner,
        audit=ledger,
        actions=actions,
        kill_switches=kill_switches,
        clock=clock,
        execution_mode='test',
    )
    app = create_operator_app(service, bind="127.0.0.1")
    return app, service, db, ledger, plans, clock


def login(client: TestClient) -> dict:
    response = client.post("/login", json={"secret": SECRET})
    assert response.status_code == 200
    session_view = client.get("/session").json()
    return {"csrf": session_view["csrf_token"], "subject": session_view["subject"]}


def csrf_headers(token: str) -> dict:
    return {"X-CSRF-Token": token}


# ---------------------------------------------------------------------------
# Binding safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["0.0.0.0", "::", "10.0.0.5", "192.168.1.9", "example.com", "::1"])
def test_unsafe_bind_addresses_are_refused(bad):
    with pytest.raises(ValueError, match="Unsafe"):
        validate_bind_address(bad)


def test_app_construction_refuses_unsafe_bind(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    with pytest.raises(ValueError, match="Unsafe"):
        create_operator_app(_service, bind="0.0.0.0")


def test_run_helper_refuses_public_bind(tmp_path: Path):
    from aipm.control_plane.transport import run_operator_transport

    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    with pytest.raises(ValueError, match="Unsafe"):
        run_operator_transport(_service, host="0.0.0.0", port=18787)


# ---------------------------------------------------------------------------
# Session security
# ---------------------------------------------------------------------------


def test_login_logout_and_session_flow(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    assert client.get("/session").status_code == 401
    auth = login(client)
    assert auth["subject"] == "local-owner"
    view = client.get("/session").json()
    assert view["csrf_token"] == auth["csrf"]
    response = client.post("/logout", headers=csrf_headers(auth["csrf"]))
    assert response.status_code == 200
    assert client.get("/session").status_code == 401


def test_invalid_login_is_generic_and_never_leaks(tmp_path: Path):
    app, service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    response = client.post("/login", json={"secret": "totally-wrong"})
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "unauthenticated"
    assert SECRET not in response.text
    assert "argon2" not in response.text.lower()


def test_malformed_login_body_is_422(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    assert client.post("/login", json={}).status_code == 422
    assert client.post("/login", json={"secret": "x" * 2000}).status_code == 422
    assert client.post("/login", content=b"not-json", headers={"Content-Type": "application/json"}).status_code == 422


def test_session_cookie_flags_are_secure(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    response = client.post("/login", json={"secret": SECRET})
    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie.lower() or "samesite=strict" in set_cookie.lower()
    assert "aipm_cp_session=" in set_cookie
    # The cookie must be opaque: no subject/verifier material inside.
    assert SECRET not in set_cookie
    assert "argon2" not in set_cookie.lower()


def test_revoked_session_is_rejected(tmp_path: Path):
    app, service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    auth = login(client)
    service.rotate_credentials()
    assert client.get("/session").status_code == 401


def test_expired_session_is_rejected(tmp_path: Path):
    clock = _Clock(NOW)
    app, _service, _db, _ledger, _plans, _clock2 = build_transport(tmp_path, clock=clock)
    client = TestClient(app)
    login(client)
    clock.value = NOW + timedelta(minutes=11)
    assert client.get("/session").status_code == 401


# ---------------------------------------------------------------------------
# CSRF enforcement
# ---------------------------------------------------------------------------


def _authorize_via_http(client: TestClient, csrf: str) -> dict:
    response = client.post(
        "/plans/project-demo/authorize",
        json={"fields": {"title": "New title"}, "idempotency_key": "idem-http-001"},
        headers=csrf_headers(csrf),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_state_changes_require_csrf(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    auth = login(client)
    # Missing token
    assert client.post("/plans/project-demo/authorize", json={"fields": {"title": "N"}, "idempotency_key": "k1"}).status_code == 403
    # Wrong token
    assert client.post("/plans/project-demo/authorize", json={"fields": {"title": "N"}, "idempotency_key": "k1"}, headers=csrf_headers("wrong")).status_code == 403
    # Correct token works
    assert _authorize_via_http(client, auth["csrf"])["allowed"] is True


def test_csrf_token_is_session_bound_and_not_replayable_across_sessions(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client_a = TestClient(app)
    client_b = TestClient(app)
    auth_a = login(client_a)
    login(client_b)  # second independent session
    # A's token used on B's session must fail.
    response = client_b.post(
        "/plans/project-demo/authorize",
        json={"fields": {"title": "N"}, "idempotency_key": "k2"},
        headers=csrf_headers(auth_a["csrf"]),
    )
    assert response.status_code == 403


def test_csrf_token_never_appears_in_audit(tmp_path: Path):
    app, _service, db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    auth = login(client)
    _authorize_via_http(client, auth["csrf"])
    rows = db.connection.execute("SELECT reason, result_code, event_type FROM control_plane_audit_ledger").fetchall()
    for row in rows:
        blob = " ".join(str(row[key]) for key in row.keys())
        assert auth["csrf"] not in blob


# ---------------------------------------------------------------------------
# HTTP success flow
# ---------------------------------------------------------------------------


def test_full_http_success_flow(tmp_path: Path):
    app, service, db, ledger, plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    auth = login(client)

    health = client.get("/health").json()
    assert health["status"] == "available"
    assert health["schema_version"] == 5

    plan = client.get("/plans/project-demo").json()
    assert plan["revision"] == 1

    decision = _authorize_via_http(client, auth["csrf"])
    assert decision["allowed"] is True and decision["confirmation_required"] is True
    action_id = decision["action_id"]

    confirmed = client.post(f"/actions/{action_id}/confirm", headers=csrf_headers(auth["csrf"])).json()
    assert confirmed["state"] == "snapshot_captured"

    executed = client.post(f"/actions/{action_id}/execute", headers=csrf_headers(auth["csrf"])).json()
    assert executed["outcome"] == "verification_succeeded"
    assert executed["lifecycle_state"] == "verified_success"

    view = client.get(f"/actions/{action_id}").json()
    assert view["state"] == "verified_success"
    assert view["outcome"] == "verification_succeeded"

    assert plans.read("project-demo").revision == 2
    assert plans.read("project-demo").title == "New title"

    audit = client.get(f"/actions/{action_id}/audit").json()
    types = [event["event_type"] for event in audit["events"]]
    assert "authorization_allowed" in types
    assert "owner_confirmed" in types
    assert "lease_acquired" in types
    assert "execution_succeeded" in types
    assert "verification_succeeded" in types

    chain = client.get("/audit/verify").json()
    assert chain["valid"] is True
    assert chain["sequence"] > 0


# ---------------------------------------------------------------------------
# HTTP failure + rollback flow
# ---------------------------------------------------------------------------


def test_full_http_rollback_flow(tmp_path: Path):
    app, service, db, ledger, plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    auth = login(client)

    decision = _authorize_via_http(client, auth["csrf"])
    action_id = decision["action_id"]
    client.post(f"/actions/{action_id}/confirm", headers=csrf_headers(auth["csrf"]))

    # Deterministic test-induced mismatch: the mutation writes different values
    # than the contract authorized, so independent verification must fail.
    from aipm.control_plane.storage.sqlite_store import SQLiteActionRepository

    original_execute = SQLiteActionRepository.execute_plan_mutation

    def mismatched(self, a_id, expected_version, *, expected_revision, mutation_fields, now, audit_drafts=()):
        return original_execute(self, a_id, expected_version, expected_revision=expected_revision, mutation_fields={"title": "Wrong title"}, now=now, audit_drafts=audit_drafts)

    SQLiteActionRepository.execute_plan_mutation = mismatched
    executed = client.post(f"/actions/{action_id}/execute", headers=csrf_headers(auth["csrf"])).json()
    SQLiteActionRepository.execute_plan_mutation = original_execute
    assert executed["outcome"] == "verification_failed"
    assert plans.read("project-demo").title == "Wrong title"

    rollback = client.post(f"/actions/{action_id}/rollback", headers=csrf_headers(auth["csrf"])).json()
    assert rollback["allowed"] is True
    rollback_action_id = rollback["rollback_action_id"]
    assert client.get(f"/actions/{action_id}").json()["state"] == "rollback_requested"

    confirmed_rollback = client.post(f"/actions/{rollback_action_id}/confirm", headers=csrf_headers(auth["csrf"])).json()
    assert confirmed_rollback["state"] == "snapshot_captured"

    executed_rollback = client.post(f"/actions/{rollback_action_id}/execute", headers=csrf_headers(auth["csrf"])).json()
    assert executed_rollback["outcome"] == "rollback_succeeded"

    assert client.get(f"/actions/{action_id}").json()["state"] == "rolled_back"
    restored = plans.read("project-demo")
    assert restored.title == "Old title" and restored.revision == 3
    assert client.get("/audit/verify").json()["valid"] is True


# ---------------------------------------------------------------------------
# Authorization boundaries and abuse
# ---------------------------------------------------------------------------


def test_every_state_change_denied_unauthenticated(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    action_id = "a" * 64
    assert client.post("/plans/project-demo/authorize", json={"fields": {"title": "N"}, "idempotency_key": "k"}).status_code == 401
    assert client.post(f"/actions/{action_id}/confirm").status_code in (401, 422)
    assert client.post(f"/actions/{action_id}/execute").status_code in (401, 422)
    assert client.post(f"/actions/{action_id}/reconcile").status_code in (401, 422)
    assert client.post(f"/actions/{action_id}/rollback").status_code in (401, 422)
    assert client.post("/kill-switch/engage", json={"reason": "x"}).status_code == 401
    assert client.post("/kill-switch/disengage", json={"reason": "x"}).status_code == 401
    assert client.post("/logout").status_code == 200  # logout is safe unauthenticated


def test_arbitrary_fields_and_ids_are_rejected(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    auth = login(client)
    response = client.post(
        "/plans/project-demo/authorize",
        json={"fields": {"command": "rm -rf /"}, "idempotency_key": "k"},
        headers=csrf_headers(auth["csrf"]),
    )
    assert response.status_code == 422
    response = client.post(
        "/plans/project-demo/authorize",
        json={"fields": {"title": "N"}, "idempotency_key": "k", "extra": "x"},
        headers=csrf_headers(auth["csrf"]),
    )
    assert response.status_code in (200, 422)  # extra body keys are ignored by the typed model
    for traversal in ("../../etc/passwd", "..%2f..%2f", "a/b", "a\\b"):
        assert client.get(f"/plans/{traversal}").status_code in (404, 422)
        assert client.post(
            f"/plans/{traversal}/authorize",
            json={"fields": {"title": "N"}, "idempotency_key": "k"},
            headers=csrf_headers(auth["csrf"]),
        ).status_code in (404, 422)


def test_method_confusion_is_refused(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    auth = login(client)
    assert client.get("/plans/project-demo/authorize").status_code == 405
    assert client.post("/plans/project-demo").status_code == 405
    assert client.post("/audit/verify").status_code == 405
    assert client.put("/plans/project-demo", json={}).status_code == 405
    assert client.delete("/plans/project-demo").status_code == 405


def test_no_generic_execution_routes_exist(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    auth = login(client)
    for route in ("/execute-anything", "/command", "/shell", "/update", "/sql", "/provider", "/execute-command"):
        assert client.post(route, json={}, headers=csrf_headers(auth["csrf"])).status_code == 404


def test_error_responses_never_leak_internals(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    auth = login(client)
    response = client.get("/plans/absent-target")
    assert response.status_code == 404
    body = response.text
    for forbidden in ("/home/", "/tmp/", ".db", "sqlite", "SELECT ", "Traceback"):
        assert forbidden not in body
    response = client.get("/plans/project-demo")  # exists; no path leakage either
    assert "db_path" not in response.text


def test_unknown_action_is_404_not_500(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    auth = login(client)
    response = client.post(f"/actions/{'f' * 64}/confirm", headers=csrf_headers(auth["csrf"]))
    assert response.status_code in (404, 409)
    response = client.get(f"/actions/{'f' * 64}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Kill switch transport verbs
# ---------------------------------------------------------------------------


def test_kill_switch_transport_verbs_are_authenticated_csrf_protected_and_staging_only(tmp_path: Path):
    app, service, db, ledger, _plans, _clock = build_transport(tmp_path, with_kill_switch=True)
    client = TestClient(app)
    auth = login(client)

    view = client.get("/kill-switch").json()
    assert {"staging", "production"} <= {row["environment"] for row in view["switches"]}

    disengaged = client.post("/kill-switch/disengage", json={"reason": "operator window"}, headers=csrf_headers(auth["csrf"])).json()
    assert disengaged["state"] == "disengaged"
    engaged = client.post("/kill-switch/engage", json={"reason": "window closed"}, headers=csrf_headers(auth["csrf"])).json()
    assert engaged["state"] == "engaged"

    # A missing CSRF token is refused; production stays permanently engaged.
    assert client.post("/kill-switch/engage", json={"reason": "x"}).status_code == 403
    assert db.connection.execute("SELECT state FROM kill_switch_state WHERE environment='production'").fetchone()["state"] == "permanent"

    types = [event.event_type.value for event in ledger.events()]
    assert "kill_switch_disengaged" in types
    assert "kill_switch_engaged" in types
    assert ledger.verify_chain().ok is True


# ---------------------------------------------------------------------------
# Structural isolation of the transport
# ---------------------------------------------------------------------------


def test_transport_source_has_no_forbidden_boundaries():
    from pathlib import Path as _Path

    source = (_Path("src/aipm/control_plane") / "transport.py").read_text(encoding="utf-8")
    for forbidden in (
        "import subprocess",
        "os.system",
        "sqlite3.connect",
        "UPDATE ",
        "INSERT ",
        "SELECT *",
        "UpdateEngine",
        "GitProvider",
        "DockerProvider",
        "import docker",
        "systemctl",
        "urllib",
        "requests.",
        "httpx.",
    ):
        assert forbidden not in source, forbidden


def test_transport_imports_pull_no_telemetry_or_mission_control_modules():
    import subprocess
    import sys

    code = (
        "import aipm.control_plane.transport as transport, sys;"
        "forbidden = sorted(m for m in sys.modules if m.startswith(('aipm.repositories', 'aipm.services', 'aipm.dashboard', 'aipm.capabilities')));"
        "print('FORBIDDEN=' + repr(forbidden))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert "FORBIDDEN=[]" in result.stdout
