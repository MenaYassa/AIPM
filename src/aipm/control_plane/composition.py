"""C6.1 production operator-transport composition root.

Single canonical composition path for the durable operator transport:

* one dedicated ``ControlPlaneDatabase`` SQLite store holds every durable
  component (audit ledger, action repository, project plans, durable
  operator sessions, kill-switch persistence);
* the C6.0 startup sweep (``reconcile_non_terminal_actions``) runs on the
  durable stores BEFORE the listener accepts traffic (fail closed: an
  enumeration failure refuses startup — no retry loop, no partial start);
* the existing C2 operator transport (``create_operator_app`` /
  ``run_operator_transport``) serves the composed service on loopback only;
* the update-plane ports are bound narrowly: when an update engine is
  provided, ``current_plan_digest`` reads the authoritative execution-plan
  identity — ``UpdatePlanIdentity.from_plan(engine.plan_update(target,
  dry_run=False)).digest()`` — through the composition-root port
  (``aipm.composition.update_digest``; the control plane itself never
  names engine types), and ``update_runtime`` is the C6.2 adapter
  (``compose_update_runtime(engine)``) driving the same engine instance;
  both ports therefore share one planning semantics. Without an engine
  (the default), the digest port reads the durable
  ``ProjectPlan.canonical_digest`` and ``update_runtime`` is deliberately
  NOT composed (fail-closed ``UNAVAILABLE_EVIDENCE`` at the execution
  boundary is the required behavior until the executor IPC exists —
  runtime binding is fail-closed, never a silent fallback).

This module composes only canonical authorities; it defines no parallel
approval, confirmation, session, auth, audit, gate, lease, or action
implementations, and it never binds a wildcard address.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from aipm.control_plane.approval import OwnerConfirmationService
from aipm.control_plane.audit import SQLiteAuditLedger
from aipm.control_plane.kill_switch import KillSwitchRegistry
from aipm.control_plane.models import ControlPlaneError, PlanningErrorCode
from aipm.control_plane.owner_auth import Argon2idVerifier, OwnerAuthenticator
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import AuthorizationPolicy
from aipm.control_plane.project_plan import Environment, ProjectPlanError
from aipm.control_plane.recovery_sweep import (
    RecoverySweepResult,
    reconcile_non_terminal_actions,
)
from aipm.control_plane.service import OwnerControlPlaneService
from aipm.control_plane.session import SessionStore
from aipm.control_plane.storage import (
    ControlPlaneDatabase,
    DurableSessionStore,
    SQLiteActionRepository,
    SQLiteKillSwitchStore,
    SQLiteProjectPlanStore,
    default_database_path,
)

DEFAULT_OPERATOR_TRANSPORT_PORT = 8789
OPERATOR_TRANSPORT_PORT_ENV = "AIPM_OPERATOR_TRANSPORT_PORT"
OPERATOR_TRANSPORT_VERIFIER_ENV = "AIPM_OWNER_ARGON2ID_VERIFIER"
MAX_PORT = 65535

__all__ = [
    "DEFAULT_OPERATOR_TRANSPORT_PORT",
    "OPERATOR_TRANSPORT_PORT_ENV",
    "OPERATOR_TRANSPORT_VERIFIER_ENV",
    "OperatorTransportConfigError",
    "compose_operator_service",
    "create_operator_service_app",
    "default_database_path",
    "project_plan_digest_port",
    "run_startup_recovery_sweep",
    "serve_operator_transport",
]
class OperatorTransportConfigError(Exception):
    """Fail-closed configuration error; startup refuses instead of guessing."""


def project_plan_digest_port(plans: SQLiteProjectPlanStore) -> Callable[[str], str]:
    """Bind the ``current_plan_digest`` port to the durable plan store.

    Read-only adapter: reads the authoritative ``ProjectPlan`` for the
    target and returns its ``canonical_digest``. A missing or unreadable
    plan raises a typed error; the control plane maps it to canonical
    fail-closed codes (no fabricated digest, no default).

    NOTE (C6.3): ``ProjectPlan.canonical_digest`` is the identity of the
    durable plan-of-record (a distinct, legitimate digest space). It is
    NOT the update execution-plan identity. When an update engine is
    composed, use :func:`update_plan_digest_port` (via
    ``aipm.composition.update_digest``) so approval verification speaks
    the canonical ``UpdatePlanIdentity`` digest space of the
    ``dry_run=False`` execution plan.
    """

    def _read_digest(target_id: str) -> str:
        try:
            plan = plans.read(target_id)
        except ProjectPlanError as exc:
            raise ControlPlaneError(
                PlanningErrorCode.UNAVAILABLE_EVIDENCE, "Authoritative plan is unreadable"
            ) from exc
        digest = plan.canonical_digest
        if not isinstance(digest, str) or len(digest) != 64:
            raise ControlPlaneError(
                PlanningErrorCode.UNAVAILABLE_EVIDENCE, "Authoritative plan digest is malformed"
            )
        return digest

    return _read_digest


def run_startup_recovery_sweep(
    *,
    actions: SQLiteActionRepository,
    plans: SQLiteProjectPlanStore,
    clock: Callable[[], object] | None = None,
    limit: int = 1000,
) -> RecoverySweepResult:
    """Run the C6.0 sweep once on the durable stores; failures propagate.

    The caller (composition root) treats any exception as a refuse-to-start
    condition: no listener is bound until the sweep completes.
    """

    return reconcile_non_terminal_actions(actions=actions, plans=plans, clock=clock, limit=limit)


def _bounded_port(value: str | int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise OperatorTransportConfigError("Operator transport port must be an integer") from exc
    if port < 1 or port > MAX_PORT:
        raise OperatorTransportConfigError("Operator transport port out of range")
    return port


def _verifier_from_environment() -> str:
    verifier = os.environ.get(OPERATOR_TRANSPORT_VERIFIER_ENV, "").strip()
    if not verifier:
        raise OperatorTransportConfigError(
            f"{OPERATOR_TRANSPORT_VERIFIER_ENV} is required to compose the owner authenticator"
        )
    return verifier


def _environment_for_policy() -> str:
    # The canonical policy scope set is staging; AIPM_ENVIRONMENT selects the
    # runtime posture (privilege assertions) and never widens the allow-list.
    return "staging"


def compose_operator_service(
    *,
    database_path: str | Path | None = None,
    verifier: str | None = None,
    clock: Callable[[], object] | None = None,
    allowed_targets: frozenset[str] | set[str] | None = None,
    with_kill_switch: bool = True,
    run_sweep: bool = True,
    update_engine: object | None = None,
) -> dict:
    """Compose the canonical OwnerControlPlaneService on durable stores.

    Startup order (fail closed at each step):

    1. resolve the dedicated database path (``AIPM_CONTROL_PLANE_DB``
       override via ``default_database_path()``);
    2. open the SQLite store (failure refuses startup);
    3. build every durable store on that one database (audit ledger, action
       repository, plans, durable sessions, kill-switch persistence);
    4. run the C6.0 startup sweep when ``run_sweep`` is true — enumeration
       failure propagates and refuses startup;
    5. compose the canonical service with ``execution_mode="ipc"`` and the
       update-plane ports: with an injected ``update_engine``, the digest
       port is the canonical execution-plan identity
       (``UpdatePlanIdentity`` over the engine's ``dry_run=False`` plan)
       and the runtime is the C6.2 adapter over the SAME engine instance;
       without one, the digest port reads the durable
       ``ProjectPlan.canonical_digest`` and the runtime stays fail-closed
       (no engine injected → no execution capability — never a fallback).

    Returns a bounded composition record dict; the caller owns lifecycle.
    """

    path = Path(database_path) if database_path is not None else default_database_path()
    db = ControlPlaneDatabase(path, clock=clock)
    ledger = SQLiteAuditLedger(db)
    authenticator = OwnerAuthenticator(
        Argon2idVerifier(verifier if verifier is not None else _verifier_from_environment()),
        clock=clock,
    )
    sessions = DurableSessionStore(db, clock=clock)
    plans = SQLiteProjectPlanStore(db)
    actions = SQLiteActionRepository(db, audit=ledger)
    kill_switches = None
    if with_kill_switch:
        kill_switches = KillSwitchRegistry(
            clock=clock, store=SQLiteKillSwitchStore(db, audit=ledger), audit=ledger
        )
    targets = allowed_targets if allowed_targets is not None else _registered_targets(plans)
    if not targets:
        raise OperatorTransportConfigError(
            "No registered project targets; refusing to compose an empty allow-list"
        )
    planner = PlanOnlyPlanner(clock=clock, target_allow_list=targets)
    policy = AuthorizationPolicy(
        policy_version="policy-v1",
        allowed_scopes=frozenset({(target, _environment_for_policy()) for target in targets}),
    )
    confirmations = OwnerConfirmationService(clock=clock)

    sweep_result: RecoverySweepResult | None = None
    if run_sweep:
        sweep_result = run_startup_recovery_sweep(actions=actions, plans=plans, clock=clock)

    if update_engine is not None:
        # C6.3 digest alignment + C6.2 runtime wiring (composition root
        # only; the control plane never names engine types). Both ports
        # bind the SAME engine instance so approval verification and
        # execution share one planning semantics — one canonical
        # UpdatePlanIdentity digest space end to end.
        from aipm.composition import compose_update_runtime, update_plan_digest_port

        current_plan_digest = update_plan_digest_port(update_engine)
        update_runtime = compose_update_runtime(update_engine)
    else:
        current_plan_digest = project_plan_digest_port(plans)
        update_runtime = None

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
        execution_mode="ipc",
        current_plan_digest=current_plan_digest,
        update_runtime=update_runtime,
    )
    return {
        "database": db,
        "database_path": path,
        "ledger": ledger,
        "actions": actions,
        "plans": plans,
        "sessions": sessions,
        "kill_switches": kill_switches,
        "service": service,
        "sweep_result": sweep_result,
    }


def _registered_targets(plans: SQLiteProjectPlanStore) -> frozenset[str]:
    """Enumerate registered targets from the durable plan store, fail closed.

    The plan store deliberately exposes only ``create``/``read``/``update``;
    enumeration is bounded by schema knowledge and any failure refuses
    composition (never an empty silent allow-list).
    """

    try:
        rows = plans._db.connection.execute(
            "SELECT target_id FROM project_plans ORDER BY target_id LIMIT 1000"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - fail closed on any storage error
        raise OperatorTransportConfigError("Registered targets cannot be enumerated") from exc
    return frozenset(row[0] for row in rows if row and row[0])


def create_operator_service_app(composition: dict, *, bind: str = "127.0.0.1"):
    """Wrap the composed service in the canonical C2 operator transport app.

    Delegates entirely to ``create_operator_app`` — no parallel routes, no
    dashboard import, no wildcard bind (``validate_bind_address`` refuses).
    """

    from aipm.control_plane.transport import create_operator_app

    service = composition["service"] if isinstance(composition, dict) else composition
    return create_operator_app(service, bind=bind)


def serve_operator_transport(
    *,
    database_path: str | Path | None = None,
    verifier: str | None = None,
    port: int | None = None,
    host: str = "127.0.0.1",
    clock: Callable[[], object] | None = None,
    allowed_targets: frozenset[str] | set[str] | None = None,
    with_kill_switch: bool = True,
) -> dict:
    """Compose, sweep, then serve the operator transport on loopback only.

    The sweep runs BEFORE the listener binds; any failure in composition or
    sweep propagates and the process exits without ever accepting traffic.
    """

    from aipm.control_plane.transport import run_operator_transport

    composition = compose_operator_service(
        database_path=database_path,
        verifier=verifier,
        clock=clock,
        allowed_targets=allowed_targets,
        with_kill_switch=with_kill_switch,
        run_sweep=True,
    )
    raw_port = port if port is not None else os.environ.get(OPERATOR_TRANSPORT_PORT_ENV, DEFAULT_OPERATOR_TRANSPORT_PORT)
    resolved_port = _bounded_port(raw_port)
    app = create_operator_service_app(composition, bind=host)
    run_operator_transport(composition["service"], host=host, port=resolved_port)
    return composition
