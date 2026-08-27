from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from aipm.control_plane.audit import Stage2AuditRepository
from aipm.control_plane.models import ActorRole, LifecycleError, LifecycleState, Stage2AuditEvent

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def event(**overrides):
    values = {
        "event_id": "event-001",
        "action_id": "action-001",
        "plan_id": "plan-001",
        "plan_digest": "a" * 64,
        "state": LifecycleState.PLANNED,
        "actor_subject": "human-alice",
        "actor_role": ActorRole.REQUESTER,
        "timestamp": NOW,
        "outcome_code": "plan_created",
        "attributes": (("policy_version", "policy-v1"),),
    }
    values.update(overrides)
    return Stage2AuditEvent(**values)


def test_audit_event_is_bounded_immutable_and_safe():
    value = event()
    assert value.safe_dict()["state"] == "planned"
    assert value.safe_dict()["attributes"] == {"policy_version": "policy-v1"}
    with pytest.raises((FrozenInstanceError, AttributeError)):
        value.state = LifecycleState.RUNNING


def test_audit_repository_is_append_only_and_returns_snapshots():
    repository = Stage2AuditRepository(max_records=2)
    first = repository.append(event())
    second = repository.append(event(event_id="event-002", state=LifecycleState.APPROVAL_REQUESTED, outcome_code="approval_requested"))
    assert repository.records() == (first, second)
    assert repository.safe_records()[1]["state"] == "approval_requested"
    with pytest.raises(LifecycleError, match="bound"):
        repository.append(event(event_id="event-003"))


def test_audit_repository_rejects_duplicate_event_ids():
    repository = Stage2AuditRepository()
    repository.append(event())
    with pytest.raises(LifecycleError, match="Duplicate"):
        repository.append(event())


def test_audit_event_rejects_unbounded_or_duplicate_attributes():
    with pytest.raises(LifecycleError):
        event(attributes=tuple((f"key-{i}", "value") for i in range(13)))
    with pytest.raises(LifecycleError, match="Duplicate"):
        event(attributes=(("same", "one"), ("same", "two")))
    with pytest.raises(LifecycleError):
        event(attributes=(("command", "host operation"),))


def test_stage2_audit_is_memory_only_and_does_not_touch_workspace(tmp_path):
    before = sorted(tmp_path.iterdir())
    repository = Stage2AuditRepository()
    repository.append(event())
    assert sorted(tmp_path.iterdir()) == before
