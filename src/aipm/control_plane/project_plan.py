"""Pure staging ProjectPlan resource contract for MC-6.12."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping

from aipm.control_plane.models import OperationKind

_ALLOWED_FIELDS = frozenset({"title", "objective"})
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_TITLE = 200
_MAX_OBJECTIVE = 2000


class Environment(str, Enum):
    STAGING = "staging"
    PRODUCTION = "production"


class ProjectPlanError(ValueError):
    """Raised when a bounded ProjectPlan contract is invalid."""


class PlanConflict(ProjectPlanError):
    """Raised when an expected revision is stale or a target conflicts."""


@dataclass(frozen=True, slots=True)
class ProjectPlan:
    target_id: str
    environment: Environment
    revision: int
    title: str
    objective: str
    created_at: datetime
    updated_at: datetime
    enabled: bool
    canonical_digest: str

    @classmethod
    def create(
        cls,
        *,
        target_id: str,
        environment: Environment,
        title: str,
        objective: str,
        now: datetime,
        enabled: bool = True,
    ) -> "ProjectPlan":
        if environment is not Environment.STAGING:
            raise ProjectPlanError("production targets are disabled")
        target_id = _target(target_id)
        title = _text(title, "title", _MAX_TITLE)
        objective = _text(objective, "objective", _MAX_OBJECTIVE)
        timestamp = _utc(now)
        plan = cls(target_id, environment, 1, title, objective, timestamp, timestamp, bool(enabled), "")
        return replace(plan, canonical_digest=plan.digest())

    def canonical_payload(self) -> dict[str, object]:
        return {
            "created_at": self.created_at.isoformat(),
            "enabled": self.enabled,
            "environment": self.environment.value,
            "objective": self.objective,
            "revision": self.revision,
            "target_id": self.target_id,
            "title": self.title,
            "updated_at": self.updated_at.isoformat(),
        }

    def digest(self) -> str:
        encoded = json.dumps(self.canonical_payload(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def update(
        self,
        *,
        expected_revision: int,
        fields: Mapping[str, str],
        now: datetime,
    ) -> "ProjectPlan":
        if self.environment is not Environment.STAGING:
            raise ProjectPlanError("production targets are disabled")
        if expected_revision != self.revision:
            raise PlanConflict("stale project plan revision")
        if not isinstance(fields, Mapping) or not fields or any(field not in _ALLOWED_FIELDS for field in fields):
            raise ProjectPlanError("fields outside the allow-list are denied")
        title = _text(fields.get("title", self.title), "title", _MAX_TITLE)
        objective = _text(fields.get("objective", self.objective), "objective", _MAX_OBJECTIVE)
        updated = replace(self, revision=self.revision + 1, title=title, objective=objective, updated_at=_utc(now), canonical_digest="")
        return replace(updated, canonical_digest=updated.digest())

    def safe_dict(self) -> dict[str, object]:
        return {**self.canonical_payload(), "canonical_digest": self.canonical_digest}


class InMemoryProjectPlanStore:
    """Staging-only owner store; no persistence or external side effects."""

    __slots__ = ("_plans", "_production_enabled", "_initialized")

    def __init__(self) -> None:
        object.__setattr__(self, "_plans", {})
        object.__setattr__(self, "_production_enabled", False)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("ProjectPlanStore configuration is immutable")
        object.__setattr__(self, name, value)

    def create(self, plan: ProjectPlan) -> ProjectPlan:
        if not isinstance(plan, ProjectPlan) or plan.environment is not Environment.STAGING:
            raise ProjectPlanError("only staging ProjectPlans are enabled")
        if plan.target_id in self._plans:
            raise PlanConflict("target already exists")
        self._plans[plan.target_id] = plan
        return plan

    def read(self, target_id: str) -> ProjectPlan:
        try:
            return self._plans[target_id]
        except KeyError as exc:
            raise ProjectPlanError("target is not registered") from exc

    def update(self, target_id: str, *, expected_revision: int, fields: Mapping[str, str], now: datetime) -> ProjectPlan:
        plan = self.read(target_id)
        updated = plan.update(expected_revision=expected_revision, fields=fields, now=now)
        self._plans[target_id] = updated
        return updated


def allowed_fields() -> frozenset[str]:
    return _ALLOWED_FIELDS


def _target(value: str) -> str:
    if not isinstance(value, str) or _SAFE_TARGET.fullmatch(value) is None:
        raise ProjectPlanError("invalid target_id")
    return value


def _text(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ProjectPlanError(f"invalid {name}")
    if value != value.strip():
        raise ProjectPlanError(f"invalid {name}")
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ProjectPlanError("invalid timestamp")
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
