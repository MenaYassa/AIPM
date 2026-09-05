"""Read-only update planning façade for Mission Control."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

from aipm.capabilities.dashboard.api import DashboardApi
from aipm.capabilities.dashboard.project_api import DashboardProjectApi
from aipm.capabilities.dashboard.safety import assert_safe_payload
from aipm.core.app import Application
from aipm.core.exceptions import ProviderError
from aipm.models.mission_control import Observation, ObservationError
from aipm.services.project.intelligence import ProjectIntelligenceService
from aipm.services.project.service import ProjectService
from aipm.services.update.planner import UpdatePlanner

MAX_REASONS = 32
MAX_ACTIONS = 16
MAX_TEXT_LENGTH = 240

_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_PATH_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.~-]+)+")


class DashboardUpdateApi:
    """Expose only the certified read-only update planner over HTTP.

    The planner produces a dry-run plan without touching project or host
    state. Repository, health, and filesystem models embedded in the plan
    are never serialized; only whitelisted scalar fields and sanitized text
    reach the payload.
    """

    def __init__(
        self,
        intelligence: ProjectIntelligenceService,
        planner: UpdatePlanner,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.intelligence = intelligence
        self.planner = planner
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def from_application(cls, application: Any, *, dashboard_api: DashboardApi | None = None) -> "DashboardUpdateApi":
        intelligence = DashboardProjectApi.from_application(application, dashboard_api=dashboard_api).intelligence
        planner = UpdatePlanner(ProjectService(app=application))
        return cls(intelligence, planner)

    def update_plan(self, project_id: str) -> dict[str, Any]:
        identifier = self._identifier(project_id)
        if identifier is None:
            return self._error("PROJECT_ID_INVALID", "Project identifier is invalid")
        application = self.intelligence.detail(identifier)
        if application is None or not application.local_project_name:
            return self._error("PROJECT_NOT_FOUND", "Project is unavailable")
        try:
            plan = self.planner.plan(application.local_project_name, dry_run=True)
        except (LookupError, ProviderError):
            return self._error("PROJECT_NOT_FOUND", "Project is unavailable")
        except Exception:
            return self._error("UPDATE_PLAN_UNAVAILABLE", "Update plan is unavailable")
        payload = {
            "update_plan": {
                "project": plan.project,
                "dry_run": plan.dry_run,
                "proceed": plan.proceed,
                "approval_required": plan.approval_required,
                "risk": plan.risk.value,
                "reasons": self._bounded([self._sanitize(reason) for reason in plan.reasons], MAX_REASONS),
                "actions": self._bounded([self._sanitize(action) for action in plan.actions], MAX_ACTIONS),
                "snapshot_required": plan.snapshot_required,
                "estimated_restart": plan.estimated_restart,
                "stash_required": plan.stash_required,
                "pull_required": plan.pull_required,
            },
        }
        response = self._success(payload)
        assert_safe_payload(response)
        return response

    @staticmethod
    def _sanitize(text: str) -> str:
        cleaned = _URL_PATTERN.sub("[redacted]", str(text))
        cleaned = _PATH_TOKEN_PATTERN.sub(lambda match: match.group(0).rsplit("/", 1)[-1], cleaned)
        return cleaned[:MAX_TEXT_LENGTH]

    @staticmethod
    def _bounded(values: list[str], limit: int) -> list[str]:
        return values[:limit]

    @staticmethod
    def _identifier(value: str | None) -> str | None:
        value = str(value or "").strip()
        return value if len(value) == 24 and all(char in "0123456789abcdef" for char in value) else None

    def _success(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        observation = Observation.from_sample(payload, observed_at=now, now=now, max_age_seconds=60, available=True, transport_ok=True)
        return {"available": True, "status": "ok", "error": None, "observation": self._observation(observation), **payload}

    def _error(self, code: str, message: str) -> dict[str, Any]:
        now = self.clock()
        observation = Observation.from_sample(None, observed_at=None, now=now, max_age_seconds=60, available=False, transport_ok=True, error=ObservationError(code, message))
        return {"available": False, "status": "error", "error": message, "observation": self._observation(observation), "update_plan": None}

    @staticmethod
    def _observation(observation: Observation[Any]) -> dict[str, Any]:
        return {
            "transport_ok": observation.transport_ok,
            "available": observation.available,
            "state": observation.state.value,
            "observed_at": observation.observed_at.isoformat() if observation.observed_at else None,
            "age_seconds": observation.age_seconds,
            "max_age_seconds": observation.max_age_seconds,
            "error": observation.error.message if observation.error else None,
        }
