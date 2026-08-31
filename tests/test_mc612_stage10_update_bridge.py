"""Shot 10 (update-plane bridge + legacy mutation boundary) tests.

Covers: the full update-plane architecture map as enforced invariants —
UpdatePlanner cannot authorize/execute; the intent adapter allow-lists
everything; the dry-run E2E leaves the plan untouched; real providers are
unreachable from the control plane; command injection cannot pass; the
dormant dashboard acknowledge chain is gone.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.control_plane.audit import SQLiteAuditLedger
from aipm.control_plane.audit.models import AuditEventType
from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.bridge import (
    BRIDGE_VERSION,
    BridgeError,
    DryRunMutationSink,
    LegacyUpdateIntent,
    UpdateActionRequestAdapter,
)
from aipm.control_plane.models import ActionRequest, ControlPlaneError, LifecycleState, OperationKind
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


def build_service(tmp_path: Path, *, clock=None, sink=None):
    clock = clock or _Clock(NOW)
    db = ControlPlaneDatabase(db_path(tmp_path), clock=clock)
    ledger = SQLiteAuditLedger(db)
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=clock)
    sessions = OwnerSessionStore(clock=clock)
    policy = AuthorizationPolicy(policy_version="policy-v1", allowed_scopes=frozenset({("project-demo", "staging")}))
    confirmations = OwnerConfirmationService(clock=clock)
    plans = SQLiteProjectPlanStore(db)
    plans.create(ProjectPlan.create(target_id="project-demo", environment=Environment.STAGING, title="Old title", objective="Objective", now=NOW))
    planner = PlanOnlyPlanner(clock=clock, target_allow_list={"project-demo"})
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
        dry_run_sink=sink,
        execution_mode='test',
        clock=clock,
    )
    return service, db, ledger, plans, clock


# ---------------------------------------------------------------------------
# UpdatePlanner boundary (legacy planner stays read-only)
# ---------------------------------------------------------------------------


def test_legacy_update_planner_cannot_authorize_or_execute():
    from aipm.services.update.planner import UpdatePlanner

    source = inspect.getsource(UpdatePlanner)
    for forbidden in ("execute", "authorize", "ActionRequest", "subprocess", "pull(", "stash(", "commit("):
        assert forbidden not in source, forbidden
    public = {name for name in dir(UpdatePlanner) if not name.startswith("_")}
    assert public == {"plan"} or public == {"plan", "max_risk"} or "execute" not in public and "authorize" not in public


def test_legacy_update_engine_owns_all_mutation_and_is_cli_only():
    from aipm.services.update import engine as engine_module

    source = inspect.getsource(engine_module)
    assert "subprocess.run" in source or "runner" in source  # mutation lives here, bounded to CLI
    # The control plane does NOT import the engine.
    cp_root = Path("src/aipm/control_plane")
    for path in cp_root.rglob("*.py"):
        assert "UpdateEngine" not in path.read_text(encoding="utf-8"), path


def test_cli_update_path_is_the_only_legacy_entry():
    from pathlib import Path as _Path

    cli = _Path("src/aipm/cli/app.py").read_text(encoding="utf-8")
    assert "UpdateEngine().execute_update" in cli
    for path in _Path("src/aipm/capabilities/dashboard").glob("*.py"):
        assert "UpdateEngine" not in path.read_text(encoding="utf-8"), path
    for path in _Path("src/aipm/control_plane").rglob("*.py"):
        assert "execute_update" not in path.read_text(encoding="utf-8"), path


# ---------------------------------------------------------------------------
# Legacy intent adapter
# ---------------------------------------------------------------------------


def test_adapter_produces_canonical_request_from_valid_intent(tmp_path: Path):
    plans = SQLiteProjectPlanStore(ControlPlaneDatabase(db_path := tmp_path / "cp.db"))
    plans.create(ProjectPlan.create(target_id="project-demo", environment=Environment.STAGING, title="Old title", objective="Objective", now=NOW))
    adapter = UpdateActionRequestAdapter(plan_store=plans, allowed_projects={"project-demo"})
    request = adapter.adapt(LegacyUpdateIntent(project="project-demo", idempotency_key="legacy-001"))
    assert request.operation is OperationKind.UPDATE_PROJECT_PLAN
    assert request.target_id == "project-demo"
    assert request.environment == "staging"
    assert request.metadata == (("objective", "Objective"), ("title", "Old title"))
    assert request.idempotency_key == "legacy-001"
    assert BRIDGE_VERSION == "mc612-update-bridge-v1"


def test_adapter_rejects_unallow_listed_projects_and_environments(tmp_path: Path):
    plans = SQLiteProjectPlanStore(ControlPlaneDatabase(tmp_path / "cp.db"))
    plans.create(ProjectPlan.create(target_id="project-demo", environment=Environment.STAGING, title="T", objective="O", now=NOW))
    adapter = UpdateActionRequestAdapter(plan_store=plans, allowed_projects={"project-demo"})
    with pytest.raises(BridgeError, match="allow-list"):
        adapter.adapt(LegacyUpdateIntent(project="other-project", idempotency_key="k"))
    with pytest.raises(BridgeError, match="staging"):
        adapter.adapt(LegacyUpdateIntent(project="project-demo", idempotency_key="k", environment="production"))


def test_adapter_rejects_unregistered_or_disabled_plans(tmp_path: Path):
    plans = SQLiteProjectPlanStore(ControlPlaneDatabase(tmp_path / "cp.db"))
    plans.create(ProjectPlan.create(target_id="project-demo", environment=Environment.STAGING, title="T", objective="O", now=NOW))
    wide_adapter = UpdateActionRequestAdapter(plan_store=plans, allowed_projects={"project-demo", "absent"})
    with pytest.raises(BridgeError, match="registered"):
        wide_adapter.adapt(LegacyUpdateIntent(project="absent", idempotency_key="k"))
    # A disabled plan is rejected even when allow-listed (direct SQL with
    # a recomputed canonical digest keeps the row internally consistent).
    plan_row = plans.read("project-demo")
    payload = plan_row.canonical_payload()
    payload["enabled"] = False
    disabled = ProjectPlan(
        target_id=payload["target_id"],
        environment=Environment(payload["environment"]),
        revision=payload["revision"],
        title=payload["title"],
        objective=payload["objective"],
        created_at=datetime.fromisoformat(payload["created_at"]),
        updated_at=datetime.fromisoformat(payload["updated_at"]),
        enabled=False,
        canonical_digest="",
    )
    from dataclasses import replace as _replace

    disabled = _replace(disabled, canonical_digest=disabled.digest())
    disabled_store = SQLiteProjectPlanStore(ControlPlaneDatabase(tmp_path / "cp.db"))
    disabled_store._db.connection.execute(
        "UPDATE project_plans SET enabled = 0, canonical_digest = ? WHERE target_id = 'project-demo'",
        (disabled.canonical_digest,),
    )
    disabled_store._db.connection.commit()
    with pytest.raises(BridgeError, match="Disabled"):
        wide_adapter.adapt(LegacyUpdateIntent(project="project-demo", idempotency_key="k"))


def test_adapter_never_invents_identity_and_never_authorizes():
    adapter_source = inspect.getsource(UpdateActionRequestAdapter)
    for forbidden in ("derive_action_identity", "plan_digest", "sha256", "AuthorizationPolicy", "execute("):
        assert forbidden not in adapter_source, forbidden


def test_hostile_intent_values_cannot_pass(tmp_path: Path):
    plans = SQLiteProjectPlanStore(ControlPlaneDatabase(tmp_path / "cp.db"))
    plans.create(ProjectPlan.create(target_id="project-demo", environment=Environment.STAGING, title="T", objective="O", now=NOW))
    adapter = UpdateActionRequestAdapter(plan_store=plans, allowed_projects={"project-demo"})
    for hostile in ("project; rm -rf /", "project$(id)", "project`id`", "project\nid", "project/demo"):
        with pytest.raises((BridgeError, Exception)):
            adapter.adapt(LegacyUpdateIntent(project=hostile, idempotency_key="k"))
    for hostile_key in ("k; drop table", "k$(id)", "k token=x"):
        with pytest.raises((BridgeError, Exception)):
            adapter.adapt(LegacyUpdateIntent(project="project-demo", idempotency_key=hostile_key))


# ---------------------------------------------------------------------------
# Dry-run E2E: legacy intent → canonical flow → dry-run record, plan untouched
# ---------------------------------------------------------------------------


def test_dry_run_e2e_records_without_mutating(tmp_path: Path):
    sink = DryRunMutationSink()
    service, db, ledger, plans, clock = build_service(tmp_path, sink=sink)
    session = service.login(SECRET)
    intent = LegacyUpdateIntent(project="project-demo", idempotency_key="legacy-001")
    payload = service.dry_run_update_intent(session.session_id, intent, now=NOW + timedelta(minutes=1))
    assert payload["allowed"] is True and payload["dry_run"] is True
    record = payload["record"]
    assert record["pre_mutation_revision"] == 1
    assert record["pre_mutation_digest"] == plans.read("project-demo").digest()
    assert record["mutation_fields"] == [["objective", "Objective"], ["title", "Old title"]]
    assert record["dry_run"] is True
    assert record["operation"] == "update_project_plan"

    # The plan was NEVER mutated.
    assert plans.read("project-demo").revision == 1
    assert plans.read("project-demo").title == "Old title"

    # The canonical flow actually ran: confirmation consumed, snapshot taken,
    # lease granted, action parked at LEASED for a separately authorized execution.
    action = service.lifecycle(payload["action_id"])
    assert action.state is LifecycleState.LEASED
    row = db.connection.execute("SELECT outcome FROM actions WHERE action_id = ?", (payload["action_id"],)).fetchone()
    assert row["outcome"] == "mutation_not_started"
    types = [event.event_type.value for event in ledger.events()]
    assert "authorization_allowed" in types
    assert "owner_confirmed" in types
    assert "lease_acquired" in types
    # No execution events: nothing was executed.
    assert "execution_started" not in types
    assert "execution_succeeded" not in types
    assert ledger.verify_chain().ok is True
    db.close()


def test_dry_run_requires_a_sink(tmp_path: Path):
    service, db, ledger, plans, clock = build_service(tmp_path, sink=None)
    session = service.login(SECRET)
    with pytest.raises(ControlPlaneError, match="sink"):
        service.dry_run_update_intent(session.session_id, LegacyUpdateIntent(project="project-demo", idempotency_key="k"))
    db.close()


def test_dry_run_denied_intent_is_reported_not_executed(tmp_path: Path):
    sink = DryRunMutationSink()
    service, db, ledger, plans, clock = build_service(tmp_path, sink=sink)
    # Deny path: the adapter allow-lists the project, but the policy decision
    # is denied for a different reason — the plan is disabled. Authority
    # stays with the domain checks, not the adapter.
    plan_row = plans.read("project-demo")
    payload = plan_row.canonical_payload()
    payload["enabled"] = False
    disabled = ProjectPlan(
        target_id=payload["target_id"],
        environment=Environment(payload["environment"]),
        revision=payload["revision"],
        title=payload["title"],
        objective=payload["objective"],
        created_at=datetime.fromisoformat(payload["created_at"]),
        updated_at=datetime.fromisoformat(payload["updated_at"]),
        enabled=False,
        canonical_digest="",
    )
    from dataclasses import replace as _replace

    disabled = _replace(disabled, canonical_digest=disabled.digest())
    with db.connection:
        db.connection.execute(
            "UPDATE project_plans SET enabled = 0, canonical_digest = ? WHERE target_id = 'project-demo'",
            (disabled.canonical_digest,),
        )
    session = service.login(SECRET)
    payload = service.dry_run_update_intent(
        session.session_id,
        LegacyUpdateIntent(project="project-demo", idempotency_key="k"),
        now=NOW + timedelta(minutes=1),
    )
    assert payload["allowed"] is False
    assert sink.records == []
    assert payload["dry_run"] is True
    assert sink.records == []
    assert plans.read("project-demo").revision == 1
    db.close()


# ---------------------------------------------------------------------------
# Real providers are unreachable from the control plane
# ---------------------------------------------------------------------------


def test_control_plane_never_imports_providers_services_or_cli():
    cp_root = Path("src/aipm/control_plane")
    forbidden_imports = (
        "from aipm.providers",
        "from aipm.services",
        "from aipm.cli",
        "import aipm.providers",
        "import aipm.services",
        "import subprocess",
        "os.system",
    )
    for path in cp_root.rglob("*.py"):
        if path.name in ("systemd_provider.py", "privilege.py"):
            continue  # the ONE sanctioned subprocess boundary
        source = path.read_text(encoding="utf-8")
        for forbidden in forbidden_imports:
            assert forbidden not in source, (path, forbidden)
    # Named legacy machinery must not appear anywhere in the package.
    for path in cp_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("UpdateEngine", "GitProvider", "ComposeProvider", "DockerProvider"):
            assert forbidden not in source, (path, forbidden)


def test_real_update_provers_are_not_bound_to_the_bridge():
    from aipm.control_plane import bridge

    # The bridge package exposes only the typed adapter + dry-run sink —
    # no live provider objects, no engine handles.
    exported = set()
    for name in dir(bridge):
        if name.startswith("_") or not name.isidentifier() or not name[0].isupper():
            continue
        value = getattr(bridge, name)
        module = getattr(value, "__module__", None)
        if module == "aipm.control_plane.bridge" or (isinstance(value, str) and name.isupper()):
            exported.add(name)
    assert exported == {"BRIDGE_VERSION", "BridgeError", "DryRunMutationRecord", "DryRunMutationSink", "LegacyUpdateIntent", "PlanMutationStore", "UpdateActionRequestAdapter"}
    for name in exported:
        assert "engine" not in name.lower()
        assert "git" not in name.lower()
        assert "docker" not in name.lower()
        assert "compose" not in name.lower()


def test_legacy_update_engine_is_not_subordinate_to_the_control_plane_yet():
    # Direction check: control plane MUST NOT call UpdateEngine. The engine
    # keeps its own CLI entry; the bridge is intent-only (Stage A/B/C).
    engine = Path("src/aipm/services/update/engine.py").read_text(encoding="utf-8")
    assert "control_plane" not in engine
    bridge = Path("src/aipm/control_plane/bridge/__init__.py").read_text(encoding="utf-8")
    assert "UpdateEngine" not in bridge and "start_services" not in bridge


# ---------------------------------------------------------------------------
# Legacy audit analysis (documented by test)
# ---------------------------------------------------------------------------


def test_legacy_update_audit_is_documented_as_non_authoritative():
    from aipm.services.update import audit as legacy_audit

    source = inspect.getsource(legacy_audit)
    # It is a plain JSON file writer: no actor, no chain, no append-only
    # guarantee. It stays as legacy execution detail; the canonical ledger
    # remains the only authoritative audit.
    assert "write_text" in source
    assert "actor" not in source.lower()
    assert "previous_hash" not in source


def test_dormant_dashboard_acknowledge_chain_is_removed():
    from aipm.capabilities.dashboard.incidents_api import DashboardIncidentsApi
    from aipm.services.incidents.query import IncidentQueryService

    assert not hasattr(DashboardIncidentsApi, "acknowledge")
    assert not hasattr(IncidentQueryService, "acknowledge")
    source = Path("src/aipm/dashboard/server.py").read_text(encoding="utf-8")
    assert "acknowledge" not in source.lower()
