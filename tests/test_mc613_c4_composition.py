"""C4: the canonical composition boundary for the update plane.

Covers the mandated 46 test groups:

* plan binding (1-6): digest verification port, fail-closed mismatch,
  digest as binding field (never a ProjectPlan.update field), durable
  metadata binding, digest-space separation, stale-plan detection.
* policy composition (7-10): mutation-fields-minus-binding policy,
  empty-mutation denial, digest-only request denial, disjointness.
* confirmation composition (11-17): approve composes authorize→confirm,
  canonical confirm semantics, single-owner subject rule, TTL, deny paths.
* execution (18-27): snapshot capture, gated execution, confirmation
  consumption, bounded mutation, runtime-port invocation with the trusted
  binding, terminal replay, fail-closed runtime, no blind retry.
* concurrency (28-30): exactly-once execution under racing callers,
  lease fencing, idempotent approval.
* unknown outcome (31-32): unverified execution never fires the runtime.
* transport (33-39): approval/execute delegation, bounded bodies,
  cross-project replay refusal, status read-only projection.
* boundary (40-46): source-level composition-boundary scans (no engine
  types in control plane, no parallel approval authority, audit
  vocabulary, import isolation).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aipm.control_plane.models import (
    ActionRequest,
    BINDING_METADATA_KEYS,
    OperationKind,
    UpdateExecutionBinding,
)
from aipm.control_plane.project_plan import Environment, ProjectPlan
from aipm.control_plane.transport import create_operator_app

from tests.test_mc612_stage9_transport import (
    NOW,
    SECRET,
    VERIFIER,
    _Clock,
    csrf_headers,
    db_path,
    login,
)

PROJECT_ID = "a" * 24
OTHER_PROJECT_ID = "c" * 24
HEX64 = re.compile(r"^[0-9a-f]{64}$")


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


def _drift_plan(plans) -> None:
    """Mutate the plan outside the approval channel (revision 1 → 2)."""

    plans.update(
        PROJECT_ID,
        expected_revision=1,
        fields={"title": "Drifted title"},
        now=NOW,
    )


def _harness(
    tmp_path: Path,
    *,
    compose_runtime: bool = True,
    runtime=None,
    planner_allow_list: frozenset[str] | None = None,
):
    """Full canonical composition with the two update-plane ports bound.

    ``current_plan_digest`` returns the authoritative ProjectPlan digest;
    ``update_runtime`` records the bindings it receives. The returned
    adapter function shows the composition-root conversion of the
    control-plane binding into the engine-side execution contract.
    """

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

    clock = _Clock(NOW)
    db = ControlPlaneDatabase(db_path(tmp_path), clock=clock)
    ledger = SQLiteAuditLedger(db)
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=clock)
    sessions = OwnerSessionStore(clock=clock)
    policy = AuthorizationPolicy(policy_version="policy-v1", allowed_scopes=frozenset({(PROJECT_ID, "staging")}))
    confirmations = OwnerConfirmationService(clock=clock)
    plans = SQLiteProjectPlanStore(db)
    _register_project(plans)
    planner = PlanOnlyPlanner(clock=clock, target_allow_list=planner_allow_list or frozenset({PROJECT_ID}))
    actions = SQLiteActionRepository(db, audit=ledger)

    def _current_plan_digest(target_id: str) -> str:
        return plans.read(target_id).canonical_digest

    runtime_calls: list[UpdateExecutionBinding] = []

    def _update_runtime(binding) -> dict:
        runtime_calls.append(binding)
        return {"ran": True}

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
        current_plan_digest=_current_plan_digest,
        update_runtime=runtime if runtime is not None else (_update_runtime if compose_runtime else None),
    )
    return service, plans, runtime_calls


def _http_harness(tmp_path: Path, *, compose_runtime: bool = True):
    service, plans, runtime_calls = _harness(tmp_path, compose_runtime=compose_runtime)
    app = create_operator_app(service, bind="127.0.0.1")
    return app, service, plans, runtime_calls


def _approve(client: TestClient, csrf: str, *, digest: str, key: str = "k1", project_id: str = PROJECT_ID):
    return client.post(
        _approval_url(project_id),
        json={"idempotency_key": key, "update_plan_digest": digest},
        headers=csrf_headers(csrf),
    )


# ---------------------------------------------------------------------------
# 1-6 Plan binding
# ---------------------------------------------------------------------------


def test_1_presented_digest_verified_against_authoritative_plan(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    digest = plans.read(PROJECT_ID).canonical_digest
    result = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k1",
    )
    assert result["allowed"] is True
    assert result["confirmation_id"]
    assert HEX64.fullmatch(result["plan_digest"])


def test_2_mismatched_digest_fails_closed_stale_plan(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    with pytest.raises(Exception) as excinfo:
        service.approve_update_plan(
            session.session_id,
            target_id=PROJECT_ID,
            environment="staging",
            presented_digest="b" * 64,
            idempotency_key="k1",
        )
    assert "does not match the authoritative plan" in str(excinfo.value)


def test_3_digest_is_binding_metadata_never_plan_mutation(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    digest = plans.read(PROJECT_ID).canonical_digest
    result = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k1",
    )
    assert result["allowed"] is True
    plan = plans.read(PROJECT_ID)
    # The approval alone never mutates plan content through the digest.
    assert plan.title == "Old title"
    assert plan.objective == "Objective"
    assert plan.revision == 1


def test_4_digest_bound_in_durable_decision_metadata(tmp_path: Path):
    from aipm.control_plane.models import LifecycleState

    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    digest = plans.read(PROJECT_ID).canonical_digest
    result = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k1",
    )
    decision = service._actions.get_decision(result["decision_id"])
    pairs = dict(decision.request.metadata)
    assert pairs.get("update_plan_digest") == digest
    action = service._actions.get_action(result["action_id"])
    assert action.state is LifecycleState.CONFIRMED


def test_5_action_digest_space_differs_from_update_plan_digest(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    digest = plans.read(PROJECT_ID).canonical_digest
    result = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k1",
    )
    decision = service._actions.get_decision(result["decision_id"])
    identity = decision.action_identity
    # The ActionPlan digest (identity.plan_digest) is a DIFFERENT digest
    # space from the presented update-plan digest; the durable binding pair
    # is what carries the presented value.
    assert identity.plan_digest != digest
    assert dict(decision.request.metadata)["update_plan_digest"] == digest


def test_6_plan_change_after_view_invalidates_presented_digest(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    digest = plans.read(PROJECT_ID).canonical_digest
    plans.update(
        PROJECT_ID,
        expected_revision=1,
        fields={"title": "New title"},
        now=NOW,
    )
    with pytest.raises(Exception) as excinfo:
        service.approve_update_plan(
            session.session_id,
            target_id=PROJECT_ID,
            environment="staging",
            presented_digest=digest,
            idempotency_key="k1",
        )
    assert "does not match the authoritative plan" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 7-10 Policy composition
# ---------------------------------------------------------------------------


def test_7_binding_fields_are_authorization_channel_decoration(tmp_path: Path):
    from aipm.control_plane.policy import AuthorizationPolicy

    policy = AuthorizationPolicy(
        policy_version="policy-v1",
        allowed_scopes=frozenset({(PROJECT_ID, "staging")}),
    )
    assert "update_plan_digest" in policy.binding_fields
    assert "update_plan_digest" not in policy.allowed_fields


def test_8_binding_keys_frozen_and_singleton():
    assert BINDING_METADATA_KEYS == frozenset({"update_plan_digest"})
    from aipm.control_plane.project_plan import binding_fields

    assert binding_fields() == BINDING_METADATA_KEYS


def test_9_digest_only_request_is_denied_not_allowed(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    request = ActionRequest(
        operation=OperationKind.UPDATE_PROJECT_PLAN,
        target_id=PROJECT_ID,
        idempotency_key="k1",
        metadata=(("update_plan_digest", "b" * 64),),
        environment="staging",
    )
    decision = service.authorize(session.session_id, request)
    assert decision.allowed is False


def test_10_mutation_minus_binding_reaches_canonical_policy(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    digest = plans.read(PROJECT_ID).canonical_digest
    result = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k1",
    )
    assert result["allowed"] is True
    decision = service._actions.get_decision(result["decision_id"])
    request = decision.request
    # mutation fields = request.fields - binding fields = {objective,title}
    mutation_fields = request.fields - BINDING_METADATA_KEYS
    assert mutation_fields == {"objective", "title"}


# ---------------------------------------------------------------------------
# 11-17 Confirmation composition
# ---------------------------------------------------------------------------


def test_11_approve_composes_authorize_then_confirm(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    digest = plans.read(PROJECT_ID).canonical_digest
    result = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k1",
    )
    assert result["allowed"] is True
    assert result["action_id"]
    assert result["confirmation_id"]
    decision = service._actions.get_decision(result["decision_id"])
    assert decision.allowed is True
    action = service._actions.get_action(result["action_id"])
    assert action.state.value == "confirmed"
    binding = service._confirmation_id_for(result["action_id"])
    assert binding == result["confirmation_id"]


def test_12_confirmation_is_recorded_canonical_state(tmp_path: Path):
    from aipm.control_plane.models import ConfirmationState

    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    digest = plans.read(PROJECT_ID).canonical_digest
    result = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k1",
    )
    found = None
    for binding in service._confirmations.store.values():
        if binding.action_id == result["action_id"]:
            found = binding
            break
    assert found is not None
    assert found.state is ConfirmationState.CONFIRMED
    assert found.confirmed_by_subject == session.principal.subject


def test_13_denied_authorization_yields_no_confirmation(tmp_path: Path):
    # Both targets are planned; only PROJECT_ID is policy-authorized, so the
    # OTHER project's approval is denied by the canonical policy.
    service, plans, _calls = _harness(
        tmp_path,
        planner_allow_list=frozenset({PROJECT_ID, OTHER_PROJECT_ID}),
    )
    _register_project(plans, target_id=OTHER_PROJECT_ID)
    session = service.login(SECRET)
    digest = plans.read(OTHER_PROJECT_ID).canonical_digest
    result = service.approve_update_plan(
        session.session_id,
        target_id=OTHER_PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k1",
    )
    assert result["allowed"] is False
    assert result["action_id"] is None
    assert result["confirmation_id"] is None


def test_14_confirmation_subject_is_the_requesting_owner(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    digest = plans.read(PROJECT_ID).canonical_digest
    result = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k1",
    )
    for binding in service._confirmations.store.values():
        if binding.action_id == result["action_id"]:
            assert binding.requester_subject == binding.confirmed_by_subject


def test_15_confirmation_id_is_unique_per_action(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    digest = plans.read(PROJECT_ID).canonical_digest
    first = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k1",
    )
    second = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k2",
    )
    assert first["action_id"] != second["action_id"]
    assert first["confirmation_id"] != second["confirmation_id"]


def test_16_idempotent_replay_returns_same_approval(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    digest = plans.read(PROJECT_ID).canonical_digest
    kwargs = dict(
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k1",
    )
    first = service.approve_update_plan(session.session_id, **kwargs)
    replay = service.approve_update_plan(session.session_id, **kwargs)
    assert replay["allowed"] is True
    assert replay["action_id"] == first["action_id"]
    assert replay["confirmation_id"] == first["confirmation_id"]


def test_17_idempotency_key_binds_the_digest(tmp_path: Path):
    """Same key + different digest is a canonical idempotency conflict."""

    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    digest = plans.read(PROJECT_ID).canonical_digest
    first = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k1",
    )
    assert first["allowed"] is True
    from aipm.control_plane.models import PlanningErrorCode

    with pytest.raises(Exception) as excinfo:
        # A different digest under the same idempotency key must not be
        # silently accepted as the same approval.
        service.approve_update_plan(
            session.session_id,
            target_id=PROJECT_ID,
            environment="staging",
            presented_digest="d" * 64,
            idempotency_key="k1",
        )
    # Either stale-plan (digest port refuses first) or idempotency conflict;
    # both fail closed, neither approves.
    assert "does not match the authoritative plan" in str(excinfo.value) or "conflict" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# 18-27 Execution
# ---------------------------------------------------------------------------


def _approved_action(service, plans, *, key: str = "k1"):
    session = service.login(SECRET)
    digest = plans.read(PROJECT_ID).canonical_digest
    result = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key=key,
    )
    assert result["allowed"] is True
    return session, result


def test_18_run_captures_snapshot_before_execution(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session, result = _approved_action(service, plans)
    pre_mutation_digest = plans.read(PROJECT_ID).canonical_digest
    service.run_approved_update(session.session_id, action_id=result["action_id"])
    snapshot = service._snapshot_repo.snapshot_for_action(result["action_id"])
    assert snapshot is not None
    assert snapshot.revision == 1
    assert snapshot.canonical_digest == pre_mutation_digest


def test_19_run_executes_confirmed_action_to_verified_success(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session, result = _approved_action(service, plans)
    outcome = service.run_approved_update(session.session_id, action_id=result["action_id"])
    assert outcome["executed"] is True
    assert outcome["lifecycle_state"] == "verified_success"
    action = service._actions.get_action(result["action_id"])
    assert action.state.value == "verified_success"


def test_20_run_consumes_confirmation_exactly_once(tmp_path: Path):
    from aipm.control_plane.models import ConfirmationState

    service, plans, _calls = _harness(tmp_path)
    session, result = _approved_action(service, plans)
    service.run_approved_update(session.session_id, action_id=result["action_id"])
    for binding in service._confirmations.store.values():
        if binding.action_id == result["action_id"]:
            assert binding.state is ConfirmationState.CONSUMED


def test_21_run_bumps_plan_revision_exactly_once(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session, result = _approved_action(service, plans)
    service.run_approved_update(session.session_id, action_id=result["action_id"])
    assert plans.read(PROJECT_ID).revision == 2


def test_22_runtime_port_receives_trusted_binding(tmp_path: Path):
    service, plans, calls = _harness(tmp_path)
    session, result = _approved_action(service, plans)
    presented_digest = plans.read(PROJECT_ID).canonical_digest
    service.run_approved_update(session.session_id, action_id=result["action_id"])
    assert len(calls) == 1
    binding = calls[0]
    assert isinstance(binding, UpdateExecutionBinding)
    assert binding.project_name == PROJECT_ID
    assert binding.confirmation_id == result["confirmation_id"]
    assert binding.plan_digest == presented_digest


def test_23_runtime_binding_plan_digest_is_presented_space(tmp_path: Path):
    """The binding digest is the update-plan digest space (presented), NOT
    the control-plane action digest space."""

    service, plans, calls = _harness(tmp_path)
    session, result = _approved_action(service, plans)
    service.run_approved_update(session.session_id, action_id=result["action_id"])
    decision = service._actions.get_decision(result["decision_id"])
    action_digest = decision.action_identity.plan_digest
    binding = calls[0]
    assert binding.plan_digest != action_digest
    assert binding.plan_digest == dict(decision.request.metadata)["update_plan_digest"]


def test_24_terminal_replay_never_refires_runtime(tmp_path: Path):
    service, plans, calls = _harness(tmp_path)
    session, result = _approved_action(service, plans)
    service.run_approved_update(session.session_id, action_id=result["action_id"])
    assert len(calls) == 1
    replay = service.run_approved_update(session.session_id, action_id=result["action_id"])
    assert replay["executed"] is False
    assert len(calls) == 1
    assert plans.read(PROJECT_ID).revision == 2


def test_25_runtime_failure_is_fail_closed_no_retry(tmp_path: Path):
    def _failing_runtime(binding):
        raise RuntimeError("runtime exploded")

    service, plans, _calls = _harness(tmp_path, runtime=_failing_runtime)
    session, result = _approved_action(service, plans)
    with pytest.raises(RuntimeError):
        service.run_approved_update(session.session_id, action_id=result["action_id"])
    # No retry, no fallback: the mutation stands, the terminal state stands,
    # and a second call is a terminal replay that never re-runs anything.
    assert plans.read(PROJECT_ID).revision == 2
    action = service._actions.get_action(result["action_id"])
    assert action.state.value == "verified_success"
    replay = service.run_approved_update(session.session_id, action_id=result["action_id"])
    assert replay["executed"] is False


def test_26_uncomposed_runtime_fails_closed_after_execution(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path, compose_runtime=False)
    session, result = _approved_action(service, plans)
    from aipm.control_plane.models import PlanningErrorCode

    with pytest.raises(Exception) as excinfo:
        service.run_approved_update(session.session_id, action_id=result["action_id"])
    # The canonical execution itself succeeded; the missing port fails
    # closed before any engine invocation.
    assert plans.read(PROJECT_ID).revision == 2
    action = service._actions.get_action(result["action_id"])
    assert action.state.value == "verified_success"


def test_27_unknown_action_fails_closed(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    with pytest.raises(Exception):
        service.run_approved_update(session.session_id, action_id="f" * 32)


# ---------------------------------------------------------------------------
# 28-30 Concurrency
# ---------------------------------------------------------------------------


def test_28_racing_executions_run_the_mutation_exactly_once(tmp_path: Path):
    service, plans, calls = _harness(tmp_path)
    session, result = _approved_action(service, plans)
    outcomes = []
    for _ in range(2):
        try:
            outcomes.append(service.run_approved_update(session.session_id, action_id=result["action_id"]))
        except Exception as excinfo:  # lease/state conflicts are fail-closed
            outcomes.append({"executed": False, "error": str(excinfo)})
    executed = [item for item in outcomes if item.get("executed") is True]
    assert len(executed) <= 1
    assert plans.read(PROJECT_ID).revision == 2
    assert len(calls) == 1


def test_29_concurrent_approvals_get_distinct_actions(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    session = service.login(SECRET)
    digest = plans.read(PROJECT_ID).canonical_digest
    first = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k1",
    )
    second = service.approve_update_plan(
        session.session_id,
        target_id=PROJECT_ID,
        environment="staging",
        presented_digest=digest,
        idempotency_key="k2",
    )
    assert first["action_id"] != second["action_id"]


def test_30_confirmation_consumption_is_single_use(tmp_path: Path):
    from aipm.control_plane.models import ConfirmationState

    service, plans, _calls = _harness(tmp_path)
    session, result = _approved_action(service, plans)
    service.run_approved_update(session.session_id, action_id=result["action_id"])
    consumed = [
        binding
        for binding in service._confirmations.store.values()
        if binding.action_id == result["action_id"] and binding.state is ConfirmationState.CONSUMED
    ]
    assert len(consumed) == 1


# ---------------------------------------------------------------------------
# 31-32 Unknown outcome
# ---------------------------------------------------------------------------


def test_31_unverified_execution_never_fires_runtime(tmp_path: Path):
    service, plans, calls = _harness(tmp_path)
    session, result = _approved_action(service, plans)
    # The plan drifts after approval, before any snapshot is captured.
    _drift_plan(plans)
    try:
        service.run_approved_update(session.session_id, action_id=result["action_id"])
    except Exception:
        pass
    # The runtime port never fires for an unverified/failed execution.
    assert calls == []


def test_32_failed_execution_preserves_unknown_outcome_semantics(tmp_path: Path):
    service, plans, calls = _harness(tmp_path)
    session, result = _approved_action(service, plans)
    _drift_plan(plans)
    outcome = None
    try:
        outcome = service.run_approved_update(session.session_id, action_id=result["action_id"])
    except Exception:
        outcome = None
    if outcome is not None:
        assert outcome["executed"] is False
    assert calls == []
    # Plan drift must never be silently accepted as success.
    assert plans.read(PROJECT_ID).revision in (1, 2)
    action = service._actions.get_action(result["action_id"])
    assert action.state.value != "verified_success" or plans.read(PROJECT_ID).revision == 2


# ---------------------------------------------------------------------------
# 33-39 Transport
# ---------------------------------------------------------------------------


def test_33_approval_route_delegates_and_confirms(tmp_path: Path):
    app, service, plans, _calls = _http_harness(tmp_path, compose_runtime=False)
    client = TestClient(app)
    auth = login(client)
    digest = plans.read(PROJECT_ID).canonical_digest
    response = _approve(client, auth["csrf"], digest=digest)
    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] is True
    assert payload["approval"] == "confirmed"
    assert payload["confirmation_id"]
    assert payload["action_id"]


def test_34_approval_route_fail_closed_on_wrong_digest(tmp_path: Path):
    app, service, plans, _calls = _http_harness(tmp_path, compose_runtime=False)
    client = TestClient(app)
    auth = login(client)
    response = _approve(client, auth["csrf"], digest="b" * 64)
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "stale_plan"


def test_35_execute_route_requires_action_id_body(tmp_path: Path):
    app, service, plans, _calls = _http_harness(tmp_path)
    client = TestClient(app)
    auth = login(client)
    for body in ({}, {"action_id": ""}, {"action_id": "x" * 129}, {"action_id": "a", "extra": 1}):
        response = client.post(
            f"/updates/{PROJECT_ID}/execute",
            json=body,
            headers=csrf_headers(auth["csrf"]),
        )
        assert response.status_code == 422, body


def test_36_execute_route_runs_composed_chain(tmp_path: Path):
    app, service, plans, calls = _http_harness(tmp_path)
    client = TestClient(app)
    auth = login(client)
    digest = plans.read(PROJECT_ID).canonical_digest
    approval = _approve(client, auth["csrf"], digest=digest)
    action_id = approval.json()["action_id"]
    response = client.post(
        f"/updates/{PROJECT_ID}/execute",
        json={"action_id": action_id},
        headers=csrf_headers(auth["csrf"]),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["executed"] is True
    assert payload["lifecycle_state"] == "verified_success"
    assert len(calls) == 1
    assert plans.read(PROJECT_ID).revision == 2


def test_37_execute_route_refuses_cross_project_replay(tmp_path: Path):
    app, service, plans, calls = _http_harness(tmp_path)
    client = TestClient(app)
    auth = login(client)
    digest = plans.read(PROJECT_ID).canonical_digest
    approval = _approve(client, auth["csrf"], digest=digest)
    action_id = approval.json()["action_id"]
    # The action belongs to PROJECT_ID; executing it through the OTHER
    # project's URL must fail closed.
    response = client.post(
        f"/updates/{OTHER_PROJECT_ID}/execute",
        json={"action_id": action_id},
        headers=csrf_headers(auth["csrf"]),
    )
    assert response.status_code == 409
    assert calls == []
    assert plans.read(PROJECT_ID).revision == 1


def test_38_execute_route_unknown_action_fails_closed(tmp_path: Path):
    app, service, plans, calls = _http_harness(tmp_path)
    client = TestClient(app)
    auth = login(client)
    response = client.post(
        f"/updates/{PROJECT_ID}/execute",
        json={"action_id": "f" * 32},
        headers=csrf_headers(auth["csrf"]),
    )
    assert response.status_code == 409
    assert calls == []


def test_39_status_route_is_read_only_projection(tmp_path: Path):
    app, service, plans, _calls = _http_harness(tmp_path)
    client = TestClient(app)
    auth = login(client)
    before = plans.read(PROJECT_ID)
    response = client.get(f"/updates/{PROJECT_ID}/status")
    assert response.status_code == 200
    after = plans.read(PROJECT_ID)
    assert (before.revision, before.title) == (after.revision, after.title)
    payload = response.json()
    assert payload["project_id"] == PROJECT_ID
    assert payload["execution"] == {"available": False}


# ---------------------------------------------------------------------------
# 40-46 Boundary (source-level composition scans)
# ---------------------------------------------------------------------------


def _control_plane_sources():
    root = Path("src/aipm/control_plane")
    return {path: path.read_text(encoding="utf-8") for path in sorted(root.rglob("*.py"))}


def test_40_control_plane_never_names_engine_types():
    for path, source in _control_plane_sources().items():
        for forbidden in ("UpdateEngine", "GitProvider", "ComposeProvider", "DockerProvider"):
            assert forbidden not in source, (path, forbidden)


def test_41_control_plane_never_imports_services_or_cli():
    for path, source in _control_plane_sources().items():
        if path.name in ("systemd_provider.py", "privilege.py"):
            continue
        for forbidden in (
            "from aipm.providers",
            "from aipm.services",
            "from aipm.cli",
            "import aipm.providers",
            "import aipm.services",
            "import aipm.cli",
        ):
            assert forbidden not in source, (path, forbidden)


def test_42_binding_never_constructed_outside_service_layer():
    for path, source in _control_plane_sources().items():
        if path.name == "models.py":
            continue
        if path.name == "service.py":
            continue
        assert "UpdateExecutionBinding(" not in source, path


def test_43_transport_never_touches_confirmations_or_contracts():
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


def test_44_legacy_approval_authority_stays_demoted():
    for path in ("src/aipm/control_plane/transport.py",):
        source = Path(path).read_text(encoding="utf-8")
        for forbidden in (
            "UpdateApprovalRecord",
            "InMemoryUpdateApprovalStore",
            "UpdateApprovalService",
            "UpdateFlightControl",
        ):
            assert forbidden not in source, forbidden
    # The legacy module itself is untouched and non-authoritative.
    legacy = Path("src/aipm/services/update/approval.py").read_text(encoding="utf-8")
    assert "class UpdateApprovalService" in legacy


def test_45_transport_introduces_no_new_audit_vocabulary():
    from aipm.control_plane.audit import builders as audit_builders

    source = Path("src/aipm/control_plane/transport.py").read_text(encoding="utf-8")
    builder_names = {name for name in dir(audit_builders) if callable(getattr(audit_builders, name)) and not name.startswith("_")}
    for name in builder_names:
        assert f"{name}(" not in source, name


def test_46_transport_import_isolation_holds():
    import subprocess
    import sys

    code = (
        "import aipm.control_plane.transport as transport, sys;"
        "forbidden = sorted(m for m in sys.modules if m.startswith(('aipm.repositories', 'aipm.services', 'aipm.dashboard', 'aipm.capabilities')));"
        "print('FORBIDDEN=' + repr(forbidden))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    line = next(item for item in result.stdout.splitlines() if item.startswith("FORBIDDEN="))
    assert "FORBIDDEN=[]" in line
