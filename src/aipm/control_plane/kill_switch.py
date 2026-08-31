"""Fail-closed kill switch for the control plane.

The switch is denied by default: only an explicitly disengaged STAGING switch
permits an operation, and production can never be disengaged. An unknown or
unregistered environment is treated as engaged (fail-closed), and a missing
persisted record is engaged.

State persistence goes through a ``KillSwitchStore``; the default store is
in-memory (test double), and the durable control-plane store implements the
same contract so an engaged or disengaged state survives process restart. A
stored state value that is not a valid ``KillSwitchState`` fails closed on
read. This module performs no execution and touches no external system.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from aipm.control_plane.project_plan import Environment


class KillSwitchState(str, Enum):
    ENGAGED = "engaged"
    DISENGAGED = "disengaged"
    PERMANENT = "permanent"


class KillSwitchError(ValueError):
    """Raised when a kill-switch transition is not permitted."""


@dataclass(frozen=True, slots=True)
class KillSwitch:
    environment: Environment
    state: KillSwitchState
    reason: str = ""
    created_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
    updated_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
    epoch: int = 1
    actor_subject: str | None = None

    def __post_init__(self) -> None:
        state = self.state if isinstance(self.state, KillSwitchState) else KillSwitchState(self.state)
        object.__setattr__(self, "state", state)
        if self.environment is not Environment.STAGING and self.state is not KillSwitchState.PERMANENT:
            raise KillSwitchError("production is permanently engaged")
        object.__setattr__(self, "reason", _reason(self.reason))
        if not isinstance(self.epoch, int) or self.epoch < 1:
            raise KillSwitchError("invalid kill-switch epoch")
        object.__setattr__(self, "created_at", _utc(self.created_at))
        updated = _utc(self.updated_at)
        if updated < self.created_at:
            raise KillSwitchError("updated_at must follow created_at")
        object.__setattr__(self, "updated_at", updated)
        if self.actor_subject is not None and not isinstance(self.actor_subject, str):
            raise KillSwitchError("invalid actor subject")

    def permits_operations(self) -> bool:
        return self.environment is Environment.STAGING and self.state is KillSwitchState.DISENGAGED

    def safe_dict(self) -> dict[str, object]:
        return {
            "environment": self.environment.value,
            "state": self.state.value,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "epoch": self.epoch,
            "permits_operations": self.permits_operations(),
        }


class InMemoryKillSwitchStore:
    """Bounded in-memory kill-switch store (test double)."""

    def __init__(self, *, audit=None) -> None:
        self._records: dict = {}
        self._audit = audit

    def record_for(self, environment):
        return self._records.get(environment)

    def save(self, switch, *, epoch: int, actor_subject: str | None, audit_drafts=()) -> None:
        from dataclasses import replace

        self._records[switch.environment] = replace(switch, epoch=epoch, actor_subject=actor_subject)
        if audit_drafts and self._audit is not None:
            for draft in audit_drafts:
                self._audit.append_in_transaction(draft)

    def records(self) -> tuple:
        return tuple(self._records.values())


class KillSwitchRegistry:
    """Fail-closed kill switches with pluggable persistence and audit.

    When an audit ledger is attached, every engage/disengage change is
    recorded with actor, environment, old/new state, epoch, and reason. With
    the durable store the state write and its evidence share one transaction;
    an evidence failure refuses the state change.
    """

    __slots__ = ("_store", "_clock", "_allocator", "_audit", "_initialized")

    def __init__(self, *, clock=None, allocator=None, store=None, audit=None) -> None:
        from aipm.control_plane.contracts import KillSwitchStore

        if store is not None and not isinstance(store, KillSwitchStore):
            raise TypeError("store must implement the KillSwitchStore contract")
        if audit is not None and not hasattr(audit, "append_in_transaction"):
            raise TypeError("audit must provide append_in_transaction")
        object.__setattr__(self, "_clock", clock or (lambda: datetime.now(timezone.utc)))
        object.__setattr__(self, "_allocator", allocator or _default_switch)
        object.__setattr__(self, "_store", store if store is not None else InMemoryKillSwitchStore())
        object.__setattr__(self, "_audit", audit)
        object.__setattr__(self, "_initialized", True)
        if not self._store.records():
            timestamp = _utc(self._clock())
            self._store.save(
                KillSwitch(environment=Environment.STAGING, state=KillSwitchState.ENGAGED, created_at=timestamp, updated_at=timestamp),
                epoch=1,
                actor_subject=None,
            )
            self._store.save(
                KillSwitch(environment=Environment.PRODUCTION, state=KillSwitchState.PERMANENT, created_at=timestamp, updated_at=timestamp),
                epoch=1,
                actor_subject=None,
            )

    def _audit_drafts(self, *, updated, old_state, actor_subject):
        if self._audit is None:
            return ()
        from aipm.control_plane.audit import builders as audit_builders

        return (
            audit_builders.kill_switch_changed(
                actor_subject=actor_subject,
                occurred_at=_utc(self._clock()),
                environment=updated.environment.value,
                from_state=old_state.value,
                to_state=updated.state.value,
                epoch=updated.epoch,
                reason=updated.reason,
            ),
        )

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("KillSwitchRegistry configuration is immutable")
        object.__setattr__(self, name, value)

    def switch(self, environment: Environment) -> KillSwitch:
        normalized = _environment(environment)
        record = self._store.record_for(normalized)
        if record is None:
            return self._allocator(normalized)
        return record

    def permits(self, environment: Environment) -> bool:
        return self.switch(environment).permits_operations()

    def engage(self, environment: Environment, *, reason: str = "", now: datetime | None = None, actor_subject: str | None = None) -> KillSwitch:
        normalized = _environment(environment)
        switch = self.switch(normalized)
        if switch.state is KillSwitchState.PERMANENT:
            raise KillSwitchError("production is permanently engaged")
        timestamp = _utc(now or self._clock())
        updated = KillSwitch(
            environment=normalized,
            state=KillSwitchState.ENGAGED,
            reason=_reason(reason) or switch.reason,
            created_at=switch.created_at,
            updated_at=timestamp,
            epoch=switch.epoch + 1,
            actor_subject=actor_subject,
        )
        drafts = self._audit_drafts(updated=updated, old_state=switch.state, actor_subject=actor_subject)
        self._store.save(updated, epoch=updated.epoch, actor_subject=actor_subject, audit_drafts=drafts)
        return updated

    def disengage(self, environment: Environment, *, reason: str = "", now: datetime | None = None, actor_subject: str | None = None) -> KillSwitch:
        normalized = _environment(environment)
        if normalized is Environment.PRODUCTION:
            raise KillSwitchError("production is permanently engaged")
        switch = self.switch(normalized)
        if switch.state is KillSwitchState.PERMANENT:
            raise KillSwitchError("production is permanently engaged")
        timestamp = _utc(now or self._clock())
        updated = KillSwitch(
            environment=normalized,
            state=KillSwitchState.DISENGAGED,
            reason=_reason(reason) or switch.reason,
            created_at=switch.created_at,
            updated_at=timestamp,
            epoch=switch.epoch + 1,
            actor_subject=actor_subject,
        )
        drafts = self._audit_drafts(updated=updated, old_state=switch.state, actor_subject=actor_subject)
        self._store.save(updated, epoch=updated.epoch, actor_subject=actor_subject, audit_drafts=drafts)
        return updated

    def engage_all(self, *, reason: str = "", now: datetime | None = None, actor_subject: str | None = None) -> None:
        self.engage(Environment.STAGING, reason=reason, now=now, actor_subject=actor_subject)


def _default_switch(environment: Environment) -> KillSwitch:
    timestamp = datetime.now(timezone.utc)
    if environment is Environment.PRODUCTION:
        return KillSwitch(environment=environment, state=KillSwitchState.PERMANENT, created_at=timestamp, updated_at=timestamp)
    return KillSwitch(environment=environment, state=KillSwitchState.ENGAGED, created_at=timestamp, updated_at=timestamp)


def _reason(value: str) -> str:
    if not isinstance(value, str) or len(value) > 256:
        raise KillSwitchError("invalid reason")
    return value


def _environment(value: Environment) -> Environment:
    if isinstance(value, Environment):
        return value
    if isinstance(value, str):
        try:
            return Environment(value)
        except ValueError as exc:
            raise KillSwitchError("invalid environment") from exc
    raise KillSwitchError("invalid environment")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise KillSwitchError("invalid timestamp")
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
