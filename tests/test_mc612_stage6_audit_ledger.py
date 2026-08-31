"""Shot 6 (canonical durable audit ledger) tests.

Covers: append/read/restart, genesis and chain verification, tamper detection
(bit flip, hash modification, previous-hash modification, deletion, reorder,
duplicate, forged insert), concurrency, centralized secret hygiene,
failure injection (state transitions are refused when evidence cannot be
committed), and integration with the Shot-3 service flows and kill switch.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.control_plane.audit import (
    GENESIS_PREVIOUS_HASH,
    AuditActorRole,
    AuditEventDraft,
    AuditEventError,
    AuditEventType,
    SQLiteAuditLedger,
)
from aipm.control_plane.audit import builders as audit_builders
from aipm.control_plane.audit.chain import compute_event_hash
from aipm.control_plane.kill_switch import KillSwitchRegistry
from aipm.control_plane.models import ActionRequest, ControlPlaneError, OperationKind, PlanningErrorCode
from aipm.control_plane.owner_auth import Argon2idVerifier, OwnerAuthenticator
from aipm.control_plane.planner import PlanOnlyPlanner
from aipm.control_plane.policy import AuthorizationPolicy
from aipm.control_plane.project_plan import Environment, ProjectPlan
from aipm.control_plane.service import OwnerControlPlaneService
from aipm.control_plane.session import OwnerSessionStore
from aipm.control_plane.storage import (
    ControlPlaneDatabase,
    SQLiteActionRepository,
    SQLiteKillSwitchStore,
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


def make_plan(**overrides):
    values = {"target_id": "project-demo", "title": "Old title", "objective": "Objective", "now": NOW}
    values.update(overrides)
    return ProjectPlan.create(target_id=values["target_id"], environment=Environment.STAGING, title=values["title"], objective=values["objective"], now=values["now"])


def request(**overrides):
    values = {
        "operation": OperationKind.UPDATE_PROJECT_PLAN,
        "target_id": "project-demo",
        "idempotency_key": "idem-001",
        "metadata": (("title", "New title"),),
        "environment": "staging",
    }
    values.update(overrides)
    return ActionRequest(**values)


def build_full_service(tmp_path: Path, *, clock=None, policy_overrides=None):
    clock = clock or _Clock(NOW)
    db = ControlPlaneDatabase(db_path(tmp_path), clock=clock)
    targets = {"project-demo"}
    ledger = SQLiteAuditLedger(db)
    authenticator = OwnerAuthenticator(Argon2idVerifier(VERIFIER), clock=clock)
    sessions = OwnerSessionStore(clock=clock)
    policy = AuthorizationPolicy(
        policy_version="policy-v1",
        allowed_scopes=frozenset({("project-demo", "staging")}),
        **(policy_overrides or {}),
    )
    from aipm.control_plane.approval import OwnerConfirmationService

    confirmations = OwnerConfirmationService(clock=clock)
    plans = SQLiteProjectPlanStore(db)
    try:
        plans.create(make_plan())
    except Exception:
        pass
    planner = PlanOnlyPlanner(clock=clock, target_allow_list=targets)
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
        execution_mode='test',
        clock=clock,
    )
    return service, db, ledger, clock


# ---------------------------------------------------------------------------
# Basic audit: append, read, restart, identity, linkage
# ---------------------------------------------------------------------------


def test_ledger_append_read_and_event_identity(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path))
    ledger = SQLiteAuditLedger(db)
    first = ledger.append(AuditEventDraft(event_type=AuditEventType.AUTHENTICATION_SUCCESS, actor_subject="local-owner", occurred_at=NOW))
    second = ledger.append(AuditEventDraft(event_type=AuditEventType.ACTION_CREATED, actor_subject="local-owner", occurred_at=NOW, action_id="a" * 64))
    assert first.sequence == 1 and second.sequence == 2
    assert first.previous_hash == GENESIS_PREVIOUS_HASH
    assert second.previous_hash == first.event_hash
    assert first.event_id != second.event_id
    events = ledger.events()
    assert [event.sequence for event in events] == [1, 2]
    assert events[1].draft.action_id == "a" * 64
    assert ledger.count() == 2
    db.close()


def test_ledger_survives_restart_and_verifies(tmp_path: Path):
    db1 = ControlPlaneDatabase(db_path(tmp_path))
    ledger1 = SQLiteAuditLedger(db1)
    for index in range(5):
        ledger1.append(AuditEventDraft(event_type=AuditEventType.LIFECYCLE_TRANSITION, actor_subject="local-owner", occurred_at=NOW + timedelta(seconds=index), result_code=f"step-{index}"))
    db1.close()

    db2 = ControlPlaneDatabase(db_path(tmp_path))
    ledger2 = SQLiteAuditLedger(db2)
    verification = ledger2.verify_chain()
    assert verification.ok is True
    assert verification.events_checked == 5
    assert ledger2.count() == 5
    assert ledger2.events()[0].previous_hash == GENESIS_PREVIOUS_HASH
    db2.close()


def test_genesis_hash_is_deterministic():
    assert GENESIS_PREVIOUS_HASH == compute_event_hash.__globals__["GENESIS_PREVIOUS_HASH"]
    from aipm.control_plane.audit.chain import GENESIS_PREVIOUS_HASH as again

    assert again == GENESIS_PREVIOUS_HASH


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


def _ledger_with_events(tmp_path: Path, *, count: int = 4):
    db = ControlPlaneDatabase(db_path(tmp_path))
    ledger = SQLiteAuditLedger(db)
    for index in range(count):
        ledger.append(
            AuditEventDraft(
                event_type=AuditEventType.ACTION_CREATED,
                actor_subject="local-owner",
                occurred_at=NOW + timedelta(seconds=index),
                action_id="a" * 64,
                result_code=f"step-{index}",
            )
        )
    return db, ledger


def test_verification_passes_before_tampering(tmp_path: Path):
    db, ledger = _ledger_with_events(tmp_path)
    assert ledger.verify_chain().ok is True
    db.close()


def test_payload_bit_flip_is_detected(tmp_path: Path):
    db, ledger = _ledger_with_events(tmp_path)
    with db.connection:
        db.connection.execute("UPDATE control_plane_audit_ledger SET result_code = 'tampered' WHERE sequence = 3")
    result = ledger.verify_chain()
    assert result.ok is False and result.error_sequence == 3
    db.close()


def test_event_hash_modification_is_detected(tmp_path: Path):
    db, ledger = _ledger_with_events(tmp_path)
    with db.connection:
        db.connection.execute("UPDATE control_plane_audit_ledger SET event_hash = ?", ("f" * 64,))
    result = ledger.verify_chain()
    assert result.ok is False
    db.close()


def test_previous_hash_modification_is_detected(tmp_path: Path):
    db, ledger = _ledger_with_events(tmp_path)
    with db.connection:
        db.connection.execute("UPDATE control_plane_audit_ledger SET previous_hash = ?", ("0" * 64,))
    result = ledger.verify_chain()
    assert result.ok is False
    db.close()


def test_deleted_middle_event_is_detected(tmp_path: Path):
    db, ledger = _ledger_with_events(tmp_path)
    with db.connection:
        db.connection.execute("DELETE FROM control_plane_audit_ledger WHERE sequence = 2")
    result = ledger.verify_chain()
    assert result.ok is False and result.error_sequence == 2
    assert result.error == "sequence discontinuity"
    db.close()


def test_reordered_events_are_detected(tmp_path: Path):
    db, ledger = _ledger_with_events(tmp_path)
    with db.connection:
        db.connection.execute("UPDATE control_plane_audit_ledger SET sequence = 0 WHERE sequence = 1")
        db.connection.execute("UPDATE control_plane_audit_ledger SET sequence = 1 WHERE sequence = 4")
        db.connection.execute("UPDATE control_plane_audit_ledger SET sequence = 4 WHERE sequence = 0")
    result = ledger.verify_chain()
    assert result.ok is False
    db.close()


def test_forged_previous_hash_breaks_linkage(tmp_path: Path):
    db, ledger = _ledger_with_events(tmp_path)
    with db.connection:
        db.connection.execute(
            "UPDATE control_plane_audit_ledger SET previous_hash = ? WHERE sequence = 3",
            ("e" * 64,),
        )
    result = ledger.verify_chain()
    assert result.ok is False and result.error_sequence == 3
    assert result.error == "previous hash linkage broken"
    db.close()


def test_forged_inserted_event_is_detected(tmp_path: Path):
    db, ledger = _ledger_with_events(tmp_path)
    last = db.connection.execute(
        "SELECT * FROM control_plane_audit_ledger ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    with db.connection:
        db.connection.execute(
            "INSERT INTO control_plane_audit_ledger (sequence, event_id, event_type, occurred_at, actor_subject, actor_role,"
            " previous_hash, event_hash, chain_version) VALUES (?, ?, 'action_created', ?, 'attacker', 'system', ?, ?, ?)",
            (
                last["sequence"] + 1,
                "f" * 32,
                datetime.now(timezone.utc).isoformat(),
                last["event_hash"],
                "f" * 64,
                "mc612-audit-v1",
            ),
        )
    result = ledger.verify_chain()
    assert result.ok is False
    db.close()


def test_duplicate_sequence_is_rejected_by_the_database(tmp_path: Path):
    db, ledger = _ledger_with_events(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        with db.connection:
            db.connection.execute(
                "INSERT INTO control_plane_audit_ledger (sequence, event_id, event_type, occurred_at, actor_subject, actor_role,"
                " previous_hash, event_hash, chain_version) SELECT sequence, '0' || substr(event_id, 2), event_type, occurred_at,"
                " actor_subject, actor_role, previous_hash, event_hash, chain_version FROM control_plane_audit_ledger WHERE sequence = 1"
            )
    db.close()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_appends_produce_unique_sequence_and_valid_chain(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path))
    ledger = SQLiteAuditLedger(db)
    errors = []
    barrier = threading.Barrier(4)

    def worker(worker_index: int):
        barrier.wait()
        try:
            for index in range(5):
                ledger.append(
                    AuditEventDraft(
                        event_type=AuditEventType.LIFECYCLE_TRANSITION,
                        actor_subject="local-owner",
                        occurred_at=NOW + timedelta(seconds=worker_index * 100 + index),
                        result_code=f"w{worker_index}-{index}",
                    )
                )
        except Exception as error:  # pragma: no cover - surfaced via assertion
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert ledger.count() == 20
    sequences = [event.sequence for event in ledger.events(limit=4096)]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == 20
    assert ledger.verify_chain().ok is True
    db.close()


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


def test_secret_material_never_reaches_persisted_records(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path))
    ledger = SQLiteAuditLedger(db)
    for kwargs in (
        {"reason": "password=hunter2"},
        {"reason": "session cookie presented"},
        {"reason": "csrf token mismatch"},
        {"actor_subject": "bearer abcdef"},
        {"result_code": "leaked_api_key"},
        {"reason": "verifier $argon2id$v=19$m=65536"},
    ):
        values = {"event_type": AuditEventType.AUTHENTICATION_FAILURE, "occurred_at": NOW, "actor_subject": "unauthenticated"}
        values.update(kwargs)
        with pytest.raises(AuditEventError):
            ledger.append(AuditEventDraft(**values))
    assert ledger.count() == 0
    db.close()


def test_full_service_flow_persists_no_owner_secret(tmp_path: Path):
    service, db, ledger, _clock = build_full_service(tmp_path)
    session = service.login(SECRET)
    service.authorize(session.session_id, request())
    for event in ledger.events():
        rendered = str(event.safe_dict())
        assert SECRET not in rendered
        assert "argon2" not in rendered.casefold()
    db.close()


# ---------------------------------------------------------------------------
# Failure injection: no state without evidence
# ---------------------------------------------------------------------------



class _FailingEvidenceSink:
    """Failure-injection sink: evidence durability is unavailable."""

    def append_in_transaction(self, draft):
        raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "injected evidence failure")


def test_action_registration_is_refused_when_evidence_cannot_be_committed(tmp_path: Path):
    service, db, ledger, _clock = build_full_service(tmp_path)
    session = service.login(SECRET)
    actions_repo = service._actions
    object.__setattr__(actions_repo, "_audit", _FailingEvidenceSink())
    with pytest.raises(ControlPlaneError, match="injected"):
        service.authorize(session.session_id, request())
    object.__setattr__(actions_repo, "_audit", ledger)
    # The state change must have rolled back with the evidence failure.
    assert db.connection.execute("SELECT COUNT(*) AS c FROM actions").fetchone()["c"] == 0
    assert db.connection.execute("SELECT COUNT(*) AS c FROM authorization_decisions").fetchone()["c"] == 0
    # The plane recovers once evidence durability is restored.
    decision = service.authorize(session.session_id, request())
    assert decision.allowed is True
    assert db.connection.execute("SELECT COUNT(*) AS c FROM actions").fetchone()["c"] == 1
    assert ledger.verify_chain().ok is True
    db.close()


def test_confirmation_is_refused_when_evidence_cannot_be_committed(tmp_path: Path):
    service, db, ledger, clock = build_full_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    actions_repo = service._actions
    object.__setattr__(actions_repo, "_audit", _FailingEvidenceSink())
    with pytest.raises(ControlPlaneError, match="injected"):
        service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    object.__setattr__(actions_repo, "_audit", ledger)
    action = db.connection.execute(
        "SELECT lifecycle_state FROM actions WHERE decision_id = ?",
        (decision.decision_id,),
    ).fetchone()
    assert action["lifecycle_state"] == "confirmation_required"
    # Recovery: confirmation succeeds with evidence once durability is restored.
    binding = service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=2))
    assert binding.state.value == "confirmed"
    assert ledger.verify_chain().ok is True
    db.close()


def test_kill_switch_change_is_refused_when_evidence_cannot_be_committed(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path), clock=_Clock(NOW))
    ledger = SQLiteAuditLedger(db)
    store = SQLiteKillSwitchStore(db, audit=ledger)
    registry = KillSwitchRegistry(clock=_Clock(NOW), store=store, audit=ledger)
    object.__setattr__(store, "_audit", _FailingEvidenceSink())
    with pytest.raises(ControlPlaneError, match="injected"):
        registry.disengage(Environment.STAGING, reason="maintenance", now=NOW)
    object.__setattr__(store, "_audit", ledger)
    row = db.connection.execute("SELECT state FROM kill_switch_state WHERE environment = 'staging'").fetchone()
    assert row["state"] == "engaged"
    registry.disengage(Environment.STAGING, reason="maintenance", now=NOW)
    row = db.connection.execute("SELECT state FROM kill_switch_state WHERE environment = 'staging'").fetchone()
    assert row["state"] == "disengaged"
    assert ledger.verify_chain().ok is True
    db.close()


# ---------------------------------------------------------------------------
# Integration with Shot-3 flows
# ---------------------------------------------------------------------------


def test_service_flow_emits_canonical_event_vocabulary(tmp_path: Path):
    service, db, ledger, clock = build_full_service(tmp_path)
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    binding = service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    service.logout(session.session_id)
    types = [event.event_type for event in ledger.events()]
    assert types == [
        AuditEventType.AUTHENTICATION_SUCCESS,
        AuditEventType.SESSION_CREATED,
        AuditEventType.AUTHORIZATION_ALLOWED,
        AuditEventType.ACTION_CREATED,
        AuditEventType.LIFECYCLE_TRANSITION,
        AuditEventType.LIFECYCLE_TRANSITION,
        AuditEventType.OWNER_CONFIRMATION_REQUESTED,
        AuditEventType.OWNER_CONFIRMED,
        AuditEventType.LIFECYCLE_TRANSITION,
        AuditEventType.SESSION_REVOKED,
    ]
    assert ledger.verify_chain().ok is True
    db.close()


def test_denials_replays_and_conflicts_are_audited(tmp_path: Path):
    service, db, ledger, _clock = build_full_service(tmp_path)
    session = service.login(SECRET)
    denied = service.authorize(session.session_id, request(metadata=(("objective", "x"), ("nickname", "y"))))
    assert denied.allowed is False
    allowed = service.authorize(session.session_id, request())
    assert allowed.allowed is True
    service.authorize(session.session_id, request())  # replay
    with pytest.raises(ControlPlaneError):
        service.authorize(session.session_id, request(metadata=(("title", "Different"),)))  # conflict
    types = [event.event_type for event in ledger.events()]
    assert AuditEventType.AUTHORIZATION_DENIED in types
    assert AuditEventType.ACTION_IDEMPOTENCY_REPLAY in types
    assert AuditEventType.ACTION_IDEMPOTENCY_CONFLICT in types
    assert ledger.verify_chain().ok is True
    db.close()


def test_authentication_failures_are_audited_without_secrets(tmp_path: Path):
    service, db, ledger, _clock = build_full_service(tmp_path)
    with pytest.raises(ControlPlaneError):
        service.login("wrong-secret")
    failures = [event for event in ledger.events() if event.event_type is AuditEventType.AUTHENTICATION_FAILURE]
    assert len(failures) == 1
    assert failures[0].draft.result_code == "rejected"
    assert failures[0].draft.actor_subject == "unauthenticated"
    assert SECRET not in str(failures[0].safe_dict())
    db.close()


def test_credential_rotation_is_audited(tmp_path: Path):
    service, db, ledger, _clock = build_full_service(tmp_path)
    service.rotate_credentials()
    rotations = [event for event in ledger.events() if event.event_type is AuditEventType.CREDENTIAL_EPOCH_ROTATED]
    assert len(rotations) == 1
    assert rotations[0].draft.actor_subject == "control-plane-system"
    assert "epoch advanced to 2" in rotations[0].draft.reason
    db.close()


def test_kill_switch_changes_are_actor_attributed_and_chained(tmp_path: Path):
    db = ControlPlaneDatabase(db_path(tmp_path), clock=_Clock(NOW))
    ledger = SQLiteAuditLedger(db)
    store = SQLiteKillSwitchStore(db, audit=ledger)
    registry = KillSwitchRegistry(clock=_Clock(NOW), store=store, audit=ledger)
    registry.disengage(Environment.STAGING, reason="maintenance window", now=NOW, actor_subject="local-owner")
    registry.engage(Environment.STAGING, reason="window closed", now=NOW + timedelta(minutes=5), actor_subject="local-owner")
    events = [event for event in ledger.events() if event.event_type in {AuditEventType.KILL_SWITCH_DISENGAGED, AuditEventType.KILL_SWITCH_ENGAGED}]
    assert len(events) == 2
    disengaged = events[0]
    assert disengaged.draft.actor_subject == "local-owner"
    assert disengaged.draft.actor_role is AuditActorRole.KILL_SWITCH_OPERATOR
    assert disengaged.draft.environment == "staging"
    assert disengaged.draft.lifecycle_from == "engaged" and disengaged.draft.lifecycle_to == "disengaged"
    assert disengaged.draft.reason == "maintenance window"
    assert "epoch_2" in disengaged.draft.result_code
    with pytest.raises(Exception):
        registry.disengage(Environment.PRODUCTION, reason="impossible", now=NOW)
    production_events = [event for event in ledger.events() if event.draft.environment == "production"]
    assert production_events == []
    assert ledger.verify_chain().ok is True
    db.close()


def test_confirmation_rejection_is_audited(tmp_path: Path):
    from aipm.control_plane.models import ConfirmationKind

    service, db, ledger, _clock = build_full_service(
        tmp_path, policy_overrides={"confirmation_kind": ConfirmationKind.DISTINCT_APPROVAL}
    )
    session = service.login(SECRET)
    decision = service.authorize(session.session_id, request())
    with pytest.raises(ControlPlaneError, match="different subject"):
        service.confirm(session.session_id, decision.decision_id, now=NOW + timedelta(minutes=1))
    rejected = [event for event in ledger.events() if event.event_type is AuditEventType.OWNER_CONFIRMATION_REJECTED]
    assert len(rejected) == 1
    assert rejected[0].draft.actor_subject == "local-owner"
    assert rejected[0].draft.action_id == decision.action_identity.action_id
    assert ledger.verify_chain().ok is True
    db.close()


# ---------------------------------------------------------------------------
# Sanitizer coverage on builder surfaces
# ---------------------------------------------------------------------------


def test_builders_reject_secret_bearing_reasons():
    with pytest.raises(AuditEventError):
        audit_builders.kill_switch_changed(
            actor_subject="local-owner",
            occurred_at=NOW,
            environment="staging",
            from_state="engaged",
            to_state="disengaged",
            epoch=2,
            reason="token=leaked",
        )
