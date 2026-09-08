"""MC-6.13 C6.5-C-B: dashboard update-status surface (observation only).

Covers the additive ``latest_update_action`` projection end to end:

* backend (1-7): SELECT-only projection, null when no action exists,
  deterministic newest-action selection, the exact approved field set,
  unregistered-project behavior, and repo/service read-only guarantees.
* proxy (8-13): only approved fields are relayed, disallowed and hostile
  material is dropped, malformed shapes collapse to null, bounds hold.
* semantics (14-16): the truth table — CP lifecycle state + durable outcome
  are the only authorities; receipt evidence can never promote UNKNOWN.
* frontend (17-21): source-level bans — only the existing update/status
  route, no mutation affordances, no direct executor/socket/receipt access,
  all interpolation escaped, canonical vocabulary only.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from aipm.control_plane.action_state import InMemoryActionRepository
from aipm.control_plane.models import (
    ActionLifecycle,
    ActionRequest,
    ActionScope,
    LifecycleState,
    OperationKind,
)
from aipm.control_plane.mutation_receipt import MutationReceiptStore
from aipm.control_plane.transport import create_operator_app
from aipm.dashboard.server import create_app

from tests.test_mc613_c4_composition import OTHER_PROJECT_ID, PROJECT_ID, _register_project
from tests.test_mc612_stage8_executor import NOW, _confirmation_id, prepared_action
from tests.test_mc612_stage9_transport import build_transport, login
from tests.test_mc613_c5_dashboard_proxy import _approved

APPROVED_ACTION_FIELDS = frozenset({"action_id", "operation", "state", "outcome", "plan_revision", "expires_at"})
FORBIDDEN_ACTION_FIELDS = frozenset(
    {
        "fencing_token",
        "contract_digest",
        "provider_code",
        "receipt_id",
        "capability_id",
        "idempotency_key",
        "snapshot_id",
        "requester_subject",
        "evidence_reference",
        "version",
        "rollback_of_action_id",
    }
)

DEMO = "project-demo"
PROJECTS_SOURCE = "src/aipm/dashboard/static/mission-control-projects.js"
INDEX_SOURCE = "src/aipm/dashboard/static/index.html"


# ---------------------------------------------------------------------------
# Backend (canonical transport + service)
# ---------------------------------------------------------------------------


def test_1_status_omits_latest_action_when_none_exists(tmp_path: Path):
    app, _service, _db, _ledger, plans, _clock = build_transport(tmp_path)
    _register_project(plans)
    client = TestClient(app)
    login(client)
    payload = client.get(f"/updates/{PROJECT_ID}/status").json()
    assert payload["latest_update_action"] is None


def test_2_status_surfaces_newest_action_with_exact_fields(tmp_path: Path):
    _dashboard, operator, service, _plans, _calls, _csrf, approval = _approved(tmp_path)
    response = operator.get(f"/updates/{PROJECT_ID}/status")
    assert response.status_code == 200
    action = response.json()["latest_update_action"]
    assert action is not None
    assert set(action) == APPROVED_ACTION_FIELDS
    assert not (set(action) & FORBIDDEN_ACTION_FIELDS)
    assert action["action_id"] == approval["action_id"]
    assert action["operation"] == "update_project_plan"
    assert action["state"] == "confirmed"
    # Durable outcome was recorded at registration; it is not a fabricated null.
    assert action["outcome"] == "mutation_not_started"
    assert action["plan_revision"] == 1
    assert isinstance(action["expires_at"], str)
    # The projection reads exactly what the repo exposes: no drift.
    assert service.latest_update_action_view(PROJECT_ID)["action_id"] == approval["action_id"]


def test_3_unregistered_project_still_404s(tmp_path: Path):
    app, _service, _db, _ledger, _plans, _clock = build_transport(tmp_path)
    client = TestClient(app)
    login(client)
    response = client.get(f"/updates/{OTHER_PROJECT_ID}/status")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "not_found"


def test_4_newest_action_wins_deterministically(tmp_path: Path):
    service, _db, _ledger, _plans, _clock, session, _decision, identity, _snapshot = prepared_action(tmp_path)
    later_decision = service.authorize(
        session.session_id,
        ActionRequest(
            operation=OperationKind.UPDATE_PROJECT_PLAN,
            target_id=DEMO,
            idempotency_key="idem-002",
            metadata=(("title", "Even newer title"),),
        ),
        now=NOW + timedelta(minutes=10),
    )
    assert later_decision.action_identity is not None
    view = service.latest_update_action_view(DEMO)
    assert view["action_id"] == later_decision.action_identity.action_id
    assert view["action_id"] != identity.action_id
    first = service._actions.get_action(identity.action_id)
    second = service._actions.get_action(later_decision.action_identity.action_id)
    assert first.created_at < second.created_at


def test_5_outcome_reflects_durable_state(tmp_path: Path):
    service, db, _ledger, _plans, _clock, _session, _decision, identity, _snapshot = prepared_action(tmp_path)
    repo = service._actions
    action = repo.get_action(identity.action_id)
    _lease, leased = repo.acquire_lease(identity.action_id, expected_version=action.version, now=NOW)
    running = repo.begin_execution(
        identity.action_id,
        expected_version=leased.version,
        confirmation_id=_confirmation_id(db, identity.action_id),
        now=NOW,
    )
    repo.mark_outcome(identity.action_id, expected_version=running.version, outcome="mutation_succeeded", now=NOW)
    view = service.latest_update_action_view(DEMO)
    assert view["state"] == "running"
    assert view["outcome"] == "mutation_succeeded"


def test_6_projection_is_select_only(tmp_path: Path):
    service, _db, _ledger, _plans, _clock, _session, _decision, identity, _snapshot = prepared_action(tmp_path)
    before = service._actions.get_action(identity.action_id)
    before_audit = len(service.audit_events(limit=4096))
    first = service.latest_update_action_view(DEMO)
    second = service.latest_update_action_view(DEMO)
    assert first == second
    after = service._actions.get_action(identity.action_id)
    assert after.state is before.state
    assert after.version == before.version
    assert len(service.audit_events(limit=4096)) == before_audit


def test_7_repo_projection_matches_service_projection(tmp_path: Path):
    service, _db, _ledger, _plans, _clock, _session, _decision, identity, _snapshot = prepared_action(tmp_path)
    repo_view = service._actions.latest_action_for_target(DEMO)
    assert repo_view is not None
    assert repo_view.action_id == identity.action_id
    view = service.latest_update_action_view(DEMO)
    assert view["action_id"] == repo_view.action_id
    assert view["state"] == repo_view.state.value


# ---------------------------------------------------------------------------
# Dashboard proxy
# ---------------------------------------------------------------------------


class _StaticClient:
    """Returns one fixed upstream response for every request."""

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def request(self, method, path, *, session_cookie=None, csrf_token=None, json_body=None):
        from aipm.capabilities.dashboard.operator_client import OperatorResponse

        return OperatorResponse(status=self.status, payload=self.payload)


class _UnreachableClient:
    async def request(self, method, path, **_):
        raise RuntimeError("upstream exploded")


class _StubReadApi:
    """Inert observation envelope for every unrelated dashboard route."""

    def __getattr__(self, name: str):
        def handler(*args, **kwargs):
            return {"available": False, "status": "error", "error": "not under test"}

        return handler


def _dashboard_with(client) -> TestClient:
    from aipm.capabilities.dashboard.update_proxy_api import DashboardUpdateProxyApi

    stub = _StubReadApi()
    proxy = DashboardUpdateProxyApi(client)
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


STATUS_BODY = {
    "project_id": "d" * 24,
    "plan": {"target_id": "d" * 24, "environment": "staging", "revision": 3, "enabled": True, "canonical_digest": "e" * 64},
    "execution": {"available": False},
    "latest_update_action": {
        "action_id": "action-1",
        "operation": "update_project_plan",
        "state": "verified_success",
        "outcome": "verification_succeeded",
        "plan_revision": 3,
        "expires_at": "2026-08-28T13:00:00+00:00",
    },
}


def test_8_proxy_relays_only_approved_action_fields():
    hostile = json.loads(json.dumps(STATUS_BODY))
    hostile["latest_update_action"].update(
        {
            "fencing_token": 7,
            "contract_digest": "c" * 64,
            "receipt_id": "r1",
            "capability_id": "x",
            "idempotency_key": "k",
            "snapshot_id": "s1",
            "requester_subject": "owner",
            "evidence_reference": "/etc/shadow",
            "provider_code": "boom",
        }
    )
    dashboard = _dashboard_with(_StaticClient(hostile))
    response = dashboard.get(f"/api/projects/{'d' * 24}/update/status")
    assert response.status_code == 200
    action = response.json()["update_status"]["latest_update_action"]
    assert set(action) == APPROVED_ACTION_FIELDS
    assert not (set(action) & FORBIDDEN_ACTION_FIELDS)


def test_9_proxy_relayed_values_are_bounded():
    bloated = json.loads(json.dumps(STATUS_BODY))
    bloated["latest_update_action"]["state"] = "x" * 10_000
    dashboard = _dashboard_with(_StaticClient(bloated))
    action = dashboard.get(f"/api/projects/{'d' * 24}/update/status").json()["update_status"]["latest_update_action"]
    assert len(action["state"]) <= 256


def test_10_proxy_malformed_action_shapes_collapse_to_null():
    for malformed in ("not-a-dict", 42, [["nested"]]):
        body = json.loads(json.dumps(STATUS_BODY))
        body["latest_update_action"] = malformed
        dashboard = _dashboard_with(_StaticClient(body))
        payload = dashboard.get(f"/api/projects/{'d' * 24}/update/status").json()
        assert payload["update_status"]["latest_update_action"] is None


def test_11_proxy_null_and_missing_action_relay_null():
    for body in (
        json.loads(json.dumps(STATUS_BODY)) | {"latest_update_action": None},
        {key: value for key, value in STATUS_BODY.items() if key != "latest_update_action"},
    ):
        dashboard = _dashboard_with(_StaticClient(body))
        payload = dashboard.get(f"/api/projects/{'d' * 24}/update/status").json()
        assert payload["update_status"]["latest_update_action"] is None


def test_12_proxy_transport_failure_fails_closed():
    dashboard = _dashboard_with(_UnreachableClient())
    payload = dashboard.get(f"/api/projects/{'d' * 24}/update/status").json()
    assert payload["available"] is False
    assert payload["error"] == "control_plane_unavailable"
    assert payload["update_status"] is None


def test_13_proxy_error_codes_still_collapse_safely():
    dashboard = _dashboard_with(_StaticClient({"detail": {"error": "weird_internal_label"}}, status=500))
    payload = dashboard.get(f"/api/projects/{'d' * 24}/update/status").json()
    assert payload["available"] is False
    assert payload["error"] == "upstream_rejected"


# ---------------------------------------------------------------------------
# Semantics (truth table via canonical service)
# ---------------------------------------------------------------------------


def test_14_verified_success_is_authoritative_success(tmp_path: Path):
    service, _db, _ledger, _plans, clock, session, _decision, identity, _snapshot = prepared_action(tmp_path)
    result = service.execute_action(session.session_id, identity.action_id, now=NOW + timedelta(minutes=3))
    assert result.lifecycle_state is LifecycleState.VERIFIED_SUCCESS
    view = service.latest_update_action_view(DEMO)
    assert view["state"] == "verified_success"
    assert view["outcome"] == "verification_succeeded"
    # The projection reports the AUTHORIZED revision (durable action state),
    # never a derived post-mutation revision.
    assert view["plan_revision"] == 1
    assert clock is not None


def test_15_unknown_stays_unknown_even_with_mutation_succeeded_receipt(tmp_path: Path):
    from tests.test_mc613_c65b_receipt_evidence import _bind_digest, _drive_to_unknown

    service, _db, _ledger, _plans, _clock, _session, identity, _running = _drive_to_unknown(tmp_path)
    digest = _bind_digest(service, identity, "f" * 64)
    # Executor evidence exists: a mutation_succeeded receipt row.
    receipts = MutationReceiptStore(tmp_path / "receipts.db")
    receipts.claim(
        action_id=identity.action_id,
        fencing_token=1,
        capability_id="execute_update_plan",
        target_id=DEMO,
        contract_digest=digest,
        now=(NOW + timedelta(minutes=4)).isoformat(),
    )
    # The projection still reports the CP truth: running + unknown, never success.
    view = service.latest_update_action_view(DEMO)
    assert view["state"] == "running"
    assert view["outcome"] == "unknown_outcome"
    # A mutation_succeeded receipt exists durably, yet none of its evidence
    # vocabulary appears in the projection.
    assert not (set(view) & FORBIDDEN_ACTION_FIELDS)


def test_16_newest_action_wins_regardless_of_operation():
    from datetime import timezone

    repo = InMemoryActionRepository()

    def _lifecycle(action_id: str, operation: OperationKind, state: LifecycleState, created_at):
        return ActionLifecycle(
            action_id=action_id,
            plan_id="plan-demo",
            plan_digest="d" * 64,
            operation=operation,
            scope=ActionScope(target_id=DEMO, environment="staging", policy_version="policy-v1"),
            state=state,
            requester_subject="owner",
            idempotency_key=f"idem-{action_id}",
            created_at=created_at,
            expires_at=created_at + timedelta(hours=1),
            plan_revision=1,
        )

    update = _lifecycle("b" * 8, OperationKind.UPDATE_PROJECT_PLAN, LifecycleState.REQUESTED, NOW)
    rollback = _lifecycle("a" * 8, OperationKind.ROLLBACK_PROJECT_PLAN, LifecycleState.REQUESTED, NOW)
    repo._actions[update.action_id] = update
    repo._actions[rollback.action_id] = rollback
    # Same created_at: the greatest action_id wins (action_id DESC tie-break).
    assert repo.latest_action_for_target(DEMO).action_id == update.action_id
    # A later action wins regardless of its operation kind.
    later = _lifecycle("c" * 8, OperationKind.ROLLBACK_PROJECT_PLAN, LifecycleState.REQUESTED, NOW + timedelta(minutes=5))
    repo._actions[later.action_id] = later
    assert repo.latest_action_for_target(DEMO).action_id == later.action_id
    # Other targets never see this target's actions.
    assert repo.latest_action_for_target("other-target") is None


# ---------------------------------------------------------------------------
# Frontend (source-level)
# ---------------------------------------------------------------------------


def test_17_status_section_offers_no_mutation_affordance():
    source = Path(PROJECTS_SOURCE).read_text(encoding="utf-8")
    assert "/update/status" in source
    for banned in ("/update/approve", "/update/execute", "onclick", "<form", "method:'POST'", "method: 'POST'"):
        assert banned not in source, banned
    section = source.split("const updateStatusSection", 1)[1].split("function projectCard", 1)[0]
    assert "<button" not in section
    assert "Observation only" in section


def test_18_receipt_vocabulary_never_reaches_the_frontend():
    banned_words = ("fencing_token", "contract_digest", "receipt", "idempotency_key", "requester_subject", "snapshot_id", "evidence_reference")
    for source_path in (PROJECTS_SOURCE, INDEX_SOURCE):
        source = Path(source_path).read_text(encoding="utf-8")
        for banned in banned_words:
            assert banned not in source, (source_path, banned)


def test_19_frontend_uses_only_the_existing_status_route():
    source = Path(PROJECTS_SOURCE).read_text(encoding="utf-8")
    assert "/update/status" in source
    for banned in ("/socket", "executor", "mutation_receipt", "/audit", "/actions/", "kill-switch"):
        assert banned not in source, banned


def test_20_section_interpolation_is_escaped_and_canonical():
    source = Path(PROJECTS_SOURCE).read_text(encoding="utf-8")
    section = source.split("const updateStatusSection", 1)[1].split("function projectCard", 1)[0]
    for fragment in ("escapeHtml(stateLabel(action.state))", "escapeHtml(action.outcome", "escapeHtml(stateLabel(action.operation))", "escapeHtml(action.plan_revision", "escapeHtml(action.action_id)"):
        assert fragment in section, fragment


def test_21_scheduler_reuses_the_existing_projects_resource():
    source = Path(INDEX_SOURCE).read_text(encoding="utf-8")
    assert "scheduler.register('projects',projectController.load,{intervalMs:60000})" in source
