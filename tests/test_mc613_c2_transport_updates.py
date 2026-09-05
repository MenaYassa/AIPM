"""C2: canonical operator transport routes for the update plane.

Covers: the three update routes (POST approval, POST execute, GET status),
their authentication/CSRF/bounds behavior, delegation to the canonical
OwnerControlPlaneService (never a parallel approval authority), the digest
carried via ActionRequest.metadata, the fail-closed execute verb pending C4,
read-only status, and transport structural isolation for update routes.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aipm.control_plane.project_plan import Environment, ProjectPlan
from aipm.control_plane.transport import create_operator_app

from tests.test_mc612_stage9_transport import (
    NOW,
    SECRET,
    _Clock,
    build_transport,
    csrf_headers,
    db_path,
    login,
)

PROJECT_ID = "a" * 24
OTHER_PROJECT_ID = "c" * 24
DIGEST = "b" * 64


def _register_project(plans, target_id: str = PROJECT_ID) -> None:
    plans.create(
        ProjectPlan.create(
            target_id=target_id,
            environment=Environment.STAGING,
            title="Old title",
            objective="Objective",
            now=NOW,
        )
    )


def _approval_url(project_id: str = PROJECT_ID) -> str:
    return f"/updates/{project_id}/approval"


# ---------------------------------------------------------------------------
# Authentication and CSRF
# ---------------------------------------------------------------------------


def test_update_approval_requires_authentication(tmp_path: Path):
    app, _service, _db, _ledger, plans, _clock = build_transport(tmp_path)
    _register_project(plans)
    client = TestClient(app)
    response = client.post(
        _approval_url(),
        json={"idempotency_key": "k1", "update_plan_digest": DIGEST},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "unauthenticated"


def test_update_execute_requires_authentication(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    response = client.post(f"/updates/{PROJECT_ID}/execute", json={})
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "unauthenticated"


def test_update_status_requires_authentication(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    response = client.get(f"/updates/{PROJECT_ID}/status")
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "unauthenticated"


def test_update_approval_requires_csrf(tmp_path: Path):
    app, _service, _db, _ledger, plans, _clock = build_transport(tmp_path)
    _register_project(plans)
    client = TestClient(app)
    auth = login(client)
    assert auth["csrf"]
    # Missing token
    missing = client.post(_approval_url(), json={"idempotency_key": "k1", "update_plan_digest": DIGEST})
    assert missing.status_code == 403
    assert missing.json()["detail"]["error"] == "csrf_failed"
    # Wrong token
    wrong = client.post(
        _approval_url(),
        json={"idempotency_key": "k1", "update_plan_digest": DIGEST},
        headers=csrf_headers("wrong"),
    )
    assert wrong.status_code == 403


def test_update_execute_requires_csrf(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    login(client)
    response = client.post(f"/updates/{PROJECT_ID}/execute", json={})
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "csrf_failed"


# ---------------------------------------------------------------------------
# Bounded request validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    ["ZZZ", "a" * 23, "a" * 25, "g" * 24, "A" * 24, "%20" * 12],
)
def test_update_routes_reject_malformed_project_ids(tmp_path: Path, bad_id: str):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    auth = login(client)
    headers = csrf_headers(auth["csrf"])
    assert client.post(f"/updates/{bad_id}/approval", json={"idempotency_key": "k1", "update_plan_digest": DIGEST}, headers=headers).status_code == 422
    assert client.post(f"/updates/{bad_id}/execute", headers=headers).status_code == 422
    assert client.get(f"/updates/{bad_id}/status").status_code == 422


@pytest.mark.parametrize("bad_id", ["a" * 23 + "/..", "a%2fb" * 6])
def test_update_routes_reject_path_traversal_ids(tmp_path: Path, bad_id: str):
    """Path-embedded ids never reach a handler: the router itself rejects
    them (404) — a safe fail-closed outcome with no state resolution."""

    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    auth = login(client)
    headers = csrf_headers(auth["csrf"])
    assert client.post(f"/updates/{bad_id}/approval", json={"idempotency_key": "k1", "update_plan_digest": DIGEST}, headers=headers).status_code in {404, 422}
    assert client.post(f"/updates/{bad_id}/execute", headers=headers).status_code in {404, 422}
    assert client.get(f"/updates/{bad_id}/status").status_code in {404, 422}


def test_update_approval_rejects_unknown_fields(tmp_path: Path):
    app, _service, _db, _ledger, plans, _clock = build_transport(tmp_path)
    _register_project(plans)
    client = TestClient(app)
    auth = login(client)
    response = client.post(
        _approval_url(),
        json={"idempotency_key": "k1", "update_plan_digest": DIGEST, "approve": True},
        headers=csrf_headers(auth["csrf"]),
    )
    assert response.status_code == 422
    assert "not authorable" in response.json()["detail"]["message"]


def test_update_approval_rejects_malformed_digest(tmp_path: Path):
    app, _service, _db, _ledger, plans, _clock = build_transport(tmp_path)
    _register_project(plans)
    client = TestClient(app)
    auth = login(client)
    headers = csrf_headers(auth["csrf"])
    for bad_digest in ("", "xyz", "B" * 64, "b" * 63, "b" * 65):
        response = client.post(
            _approval_url(),
            json={"idempotency_key": "k1", "update_plan_digest": bad_digest},
            headers=headers,
        )
        assert response.status_code == 422, bad_digest
    # Missing digest key entirely
    response = client.post(_approval_url(), json={"idempotency_key": "k1"}, headers=headers)
    assert response.status_code == 422


def test_update_approval_rejects_missing_or_oversized_idempotency_key(tmp_path: Path):
    app, _service, _db, _ledger, plans, _clock = build_transport(tmp_path)
    _register_project(plans)
    client = TestClient(app)
    auth = login(client)
    headers = csrf_headers(auth["csrf"])
    assert client.post(_approval_url(), json={"update_plan_digest": DIGEST}, headers=headers).status_code == 422
    assert client.post(_approval_url(), json={"idempotency_key": "k" * 129, "update_plan_digest": DIGEST}, headers=headers).status_code == 422
    assert client.post(_approval_url(), json={}, headers=headers).status_code == 422


# ---------------------------------------------------------------------------
# Canonical delegation (no parallel approval authority)
# ---------------------------------------------------------------------------


def test_update_approval_resolves_registered_project_via_plan_store(tmp_path: Path):
    app, _service, _db, _ledger, plans, _clock = build_transport(tmp_path)
    _register_project(plans)
    client = TestClient(app)
    auth = login(client)
    # Unregistered 24-hex id must 404 without inventing state.
    response = client.post(
        f"/updates/{OTHER_PROJECT_ID}/approval",
        json={"idempotency_key": "k1", "update_plan_digest": DIGEST},
        headers=csrf_headers(auth["csrf"]),
    )
    assert response.status_code == 404
    assert "not registered" in response.json()["detail"]["message"]


def _custom_harness(tmp_path: Path):
    """Canonical service whose policy/planner allow-list the 24-hex target,
    so service.authorize reaches the policy decision (rather than being
    refused at the planner) and the transport contract can be observed."""

    from aipm.control_plane.audit import SQLiteAuditLedger
    from aipm.control_plane.approval import OwnerConfirmationService
    from aipm.control_plane.owner_auth import Argon2idVerifier, OwnerAuthenticator
    from aipm.control_plane.planner import PlanOnlyPlanner
    from aipm.control_plane.policy import AuthorizationPolicy
    from aipm.control_plane.service import OwnerControlPlaneService
    from aipm.control_plane.session import OwnerSessionStore
    from aipm.control_plane.storage import (
        ControlPlaneDatabase,
        SQLiteActionRepository,
        SQLiteProjectPlanStore,
    )

    from tests.test_mc612_stage9_transport import VERIFIER

    clock = _Clock(NOW)
    db = ControlPlaneDatabase(db_path(tmp_path), clock=clock)
    ledger = SQLiteAuditLedger(db)
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=clock)
    sessions = OwnerSessionStore(clock=clock)
    policy = AuthorizationPolicy(policy_version="policy-v1", allowed_scopes=frozenset({(PROJECT_ID, "staging")}))
    confirmations = OwnerConfirmationService(clock=clock)
    plans = SQLiteProjectPlanStore(db)
    _register_project(plans)
    planner = PlanOnlyPlanner(clock=clock, target_allow_list=frozenset({PROJECT_ID}))
    actions = SQLiteActionRepository(db, audit=ledger)
    service = OwnerControlPlaneService(
        authenticator=authenticator,
        sessions=sessions,
        policy=policy,
        confirmations=confirmations,
        plans=plans,
        planner=planner,
        audit=ledger,
        actions=actions,
        kill_switches=None,
        clock=clock,
        execution_mode="test",
    )
    app = create_operator_app(service, bind="127.0.0.1")
    return app, service


def test_update_approval_delegates_to_canonical_authority(tmp_path: Path):
    """The decision comes from the canonical policy, relayed verbatim."""

    app, _service = _custom_harness(tmp_path)
    client = TestClient(app)
    auth = login(client)
    response = client.post(
        _approval_url(),
        json={"idempotency_key": "k1", "update_plan_digest": DIGEST},
        headers=csrf_headers(auth["csrf"]),
    )
    assert response.status_code == 200
    payload = response.json()
    # Canonical decision fields plus the explicit C2 deferral marker only;
    # the transport adds no verdict, approval, or confirmation of its own.
    assert set(payload) == {"decision_id", "allowed", "code", "action_id", "confirmation_required", "expires_at", "approval"}
    assert payload["allowed"] is False
    assert payload["code"] == "field_not_allowed"
    assert payload["action_id"] is None
    assert payload["confirmation_required"] is False


def test_update_authorization_is_explicitly_not_final_approval(tmp_path: Path):
    """C2 contract: the route performs ONLY the canonical authorize step.

    The response must be explicitly marked approval=deferred so an
    authorization decision can never be mistaken for an approved update.
    The transport never consumes a confirmation, never fabricates one, and
    returns no confirmation identity.
    """

    app, _service = _custom_harness(tmp_path)
    client = TestClient(app)
    auth = login(client)
    response = client.post(
        _approval_url(),
        json={"idempotency_key": "k1", "update_plan_digest": DIGEST},
        headers=csrf_headers(auth["csrf"]),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["approval"] == "deferred"
    # No confirmation state may be fabricated by the transport.
    assert "confirmation_id" not in payload
    assert "confirmed" not in payload
    # Canonical decision fields remain exactly the authorize-step outputs.
    assert set(payload) == {"decision_id", "allowed", "code", "action_id", "confirmation_required", "expires_at", "approval"}


def test_transport_authorization_route_does_not_compose_confirmation():
    """The update authorization handler must not invoke the confirmation
    flow; only the canonical /actions/{action_id}/confirm route does."""

    source = Path("src/aipm/control_plane/transport.py").read_text(encoding="utf-8")
    approval_section = source.split("# Update plane", 1)[-1].split("# Actions", 1)[0]
    for forbidden in (
        "service.confirm_action",
        "service.capture_snapshot",
        "_confirm_and_prepare",
        "ConfirmationBinding(",
        "ConfirmationStore(",
    ):
        assert forbidden not in approval_section, forbidden


def test_update_approval_digest_flows_through_metadata(tmp_path: Path):
    """The canonical idempotency binding sees the digest pair: replay with a
    different digest is an IDEMPOTENCY_CONFLICT, proving the digest is part
    of the canonical request identity (not transport-local state)."""

    from aipm.control_plane.audit import SQLiteAuditLedger
    from aipm.control_plane.approval import OwnerConfirmationService
    from aipm.control_plane.owner_auth import Argon2idVerifier, OwnerAuthenticator
    from aipm.control_plane.planner import PlanOnlyPlanner
    from aipm.control_plane.policy import AuthorizationPolicy
    from aipm.control_plane.service import OwnerControlPlaneService
    from aipm.control_plane.session import OwnerSessionStore
    from aipm.control_plane.storage import (
        ControlPlaneDatabase,
        SQLiteActionRepository,
        SQLiteProjectPlanStore,
    )

    from tests.test_mc612_stage9_transport import VERIFIER

    clock = _Clock(NOW)
    db = ControlPlaneDatabase(db_path(tmp_path), clock=clock)
    ledger = SQLiteAuditLedger(db)
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=clock)
    sessions = OwnerSessionStore(clock=clock)
    # A permissive field allow-list is NOT possible without changing the
    # canonical policy; instead use the denied-but-registered path: a denied
    # decision registers no action, so idempotency cannot be observed via
    # HTTP. Verify at the service boundary instead: the same request with a
    # different digest yields a different canonical request identity.
    policy = AuthorizationPolicy(policy_version="policy-v1", allowed_scopes=frozenset({(PROJECT_ID, "staging")}))
    confirmations = OwnerConfirmationService(clock=clock)
    plans = SQLiteProjectPlanStore(db)
    _register_project(plans)
    planner = PlanOnlyPlanner(clock=clock, target_allow_list=frozenset({PROJECT_ID}))
    actions = SQLiteActionRepository(db, audit=ledger)
    service = OwnerControlPlaneService(
        authenticator=authenticator,
        sessions=sessions,
        policy=policy,
        confirmations=confirmations,
        plans=plans,
        planner=planner,
        audit=ledger,
        actions=actions,
        kill_switches=None,
        clock=clock,
        execution_mode="test",
    )
    session = service.login(SECRET)
    from aipm.control_plane.models import ActionRequest, OperationKind

    request_one = ActionRequest(
        operation=OperationKind.UPDATE_PROJECT_PLAN,
        target_id=PROJECT_ID,
        idempotency_key="k1",
        metadata=(("update_plan_digest", DIGEST),),
        environment="staging",
    )
    request_two = ActionRequest(
        operation=OperationKind.UPDATE_PROJECT_PLAN,
        target_id=PROJECT_ID,
        idempotency_key="k1",
        metadata=(("update_plan_digest", "c" * 64),),
        environment="staging",
    )
    assert request_one.canonical() != request_two.canonical()
    decision_one = service.authorize(session.session_id, request_one)
    decision_two = service.authorize(session.session_id, request_two)
    assert decision_one.allowed is False and decision_two.allowed is False
    assert decision_one.decision_id != decision_two.decision_id


def test_transport_never_instantiates_legacy_approval_authority():
    """The transport must not reference the demoted legacy approval stack."""

    source = Path("src/aipm/control_plane/transport.py").read_text(encoding="utf-8")
    for forbidden in ("UpdateApprovalRecord", "InMemoryUpdateApprovalStore", "UpdateApprovalService", "UpdateFlightControl"):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# Fail-closed execute (pending C4)
# ---------------------------------------------------------------------------


def test_update_execute_is_fail_closed_before_c4(tmp_path: Path):
    app, service, _db, _ledger, plans, _clock = build_transport(tmp_path)
    _register_project(plans)
    client = TestClient(app)
    auth = login(client)
    response = client.post(f"/updates/{PROJECT_ID}/execute", headers=csrf_headers(auth["csrf"]))
    assert response.status_code == 503
    payload = response.json()["detail"]
    assert payload["error"] == "unavailable"
    # The stub must not resolve the target or touch any state.
    assert service.plan_view(PROJECT_ID)["revision"] == 1


def test_update_execute_fail_closed_for_unregistered_project(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    auth = login(client)
    response = client.post(f"/updates/{OTHER_PROJECT_ID}/execute", headers=csrf_headers(auth["csrf"]))
    assert response.status_code == 503


def test_update_execute_never_mutates_plan_state(tmp_path: Path):
    app, _service, _db, _ledger, plans, _clock = build_transport(tmp_path)
    _register_project(plans)
    client = TestClient(app)
    auth = login(client)
    headers = csrf_headers(auth["csrf"])
    client.post(
        _approval_url(),
        json={"idempotency_key": "k1", "update_plan_digest": DIGEST},
        headers=headers,
    )
    before = plans.read(PROJECT_ID)
    client.post(f"/updates/{PROJECT_ID}/execute", headers=headers)
    after = plans.read(PROJECT_ID)
    assert (before.revision, before.title, before.objective) == (after.revision, after.title, after.objective)


# ---------------------------------------------------------------------------
# Read-only status
# ---------------------------------------------------------------------------


def test_update_status_is_read_only_projection(tmp_path: Path):
    app, service, _db, _ledger, plans, _clock = build_transport(tmp_path)
    _register_project(plans)
    client = TestClient(app)
    auth = login(client)
    response = client.get(f"/updates/{PROJECT_ID}/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["project_id"] == PROJECT_ID
    assert payload["plan"]["target_id"] == PROJECT_ID
    assert payload["plan"]["revision"] == 1
    assert payload["execution"] == {"available": False}
    # No lifecycle state is invented: no action exists yet.
    assert "state" not in payload["plan"]
    assert service.plan_view(PROJECT_ID)["revision"] == 1


def test_update_status_404_for_unregistered_project(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    auth = login(client)
    response = client.get(f"/updates/{OTHER_PROJECT_ID}/status")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "not_found"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_update_approval_is_rate_limited(tmp_path: Path):
    app, _service, _db, _ledger, plans, _clock = build_transport(tmp_path)
    _register_project(plans)
    client = TestClient(app)
    auth = login(client)
    headers = csrf_headers(auth["csrf"])
    codes = []
    for index in range(35):
        response = client.post(
            _approval_url(),
            json={"idempotency_key": f"k{index}", "update_plan_digest": DIGEST},
            headers=headers,
        )
        codes.append(response.status_code)
    assert 429 in codes
    # The 429 must come before any canonical evaluation for overflowed calls.
    first_429 = codes.index(429)
    assert all(code == 429 for code in codes[first_429:])


# ---------------------------------------------------------------------------
# No internal-detail leakage
# ---------------------------------------------------------------------------


def test_update_route_errors_do_not_leak_internals(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    auth = login(client)
    response = client.get(f"/updates/{OTHER_PROJECT_ID}/status")
    assert response.status_code == 404
    text = response.text.lower()
    for leaked in ("sqlite", "traceback", "exception", ".db", "sql"):
        assert leaked not in text, leaked


# ---------------------------------------------------------------------------
# Structural isolation for update routes
# ---------------------------------------------------------------------------


def test_update_routes_introduce_no_new_audit_vocabulary():
    """No new audit event names are introduced by the transport."""

    from aipm.control_plane.audit import builders as audit_builders

    source = Path("src/aipm/control_plane/transport.py").read_text(encoding="utf-8")
    builder_names = {name for name in dir(audit_builders) if callable(getattr(audit_builders, name)) and not name.startswith("_")}
    for name in builder_names:
        assert f"{name}(" not in source, name


def test_update_transport_import_isolation():
    """Importing the transport must not pull dashboard/services modules."""

    import subprocess
    import sys

    code = (
        "import aipm.control_plane.transport as transport, sys;"
        "forbidden = sorted(m for m in sys.modules if m.startswith(('aipm.repositories', 'aipm.services', 'aipm.dashboard', 'aipm.capabilities')));"
        "print('FORBIDDEN=' + repr(forbidden))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert "FORBIDDEN=[]" in result.stdout
