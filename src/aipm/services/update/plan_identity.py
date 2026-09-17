"""Canonical identity and digest for security-relevant update plans.

This module defines the deterministic, canonical representation of the
security-relevant content of an :class:`~aipm.models.update.UpdatePlan`.
The digest binds any future approval or execution authorization to the
exact plan the operator saw; any security-relevant mutation of the plan
changes the digest.

Format version: ``mc612-update-plan-identity-v1``.

Canonical serialization rules (mirroring the control-plane audit
canonicalization conventions):
* the payload is a flat JSON object with lexicographically sorted keys;
* separators are ``,`` and ``:`` with no whitespace; encoding is UTF-8;
* optional (``None``) fields are ABSENT keys, never explicit nulls;
* enums serialize as their string values (for example ``"low"``);
* ordered collections serialize as JSON arrays in their given order
  (reasons/actions are planner-ordered and semantically meaningful);
* genuinely unordered collections (Git file lists) are sorted and
  de-duplicated before serialization;
* no ``repr``, no memory addresses, no wall-clock, no random input,
  no filesystem access, no subprocess access.

The digest is ``SHA-256(canonical_bytes)`` rendered as lowercase hex,
matching the 64-hex plan-digest convention used by the control plane.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from aipm.models.update import UpdatePlan, UpdateRisk

PLAN_IDENTITY_VERSION = "mc612-update-plan-identity-v1"

MAX_IDENTITY_TEXT = 512
MAX_IDENTITY_LIST = 64
MAX_GIT_SHA_LENGTH = 64

_RISK_VALUES = frozenset(item.value for item in UpdateRisk)


@dataclass(frozen=True, slots=True)
class UpdatePlanIdentity:
    """Canonical, security-relevant identity of one update plan.

    Only fields that can affect whether an update is safe to execute are
    represented. Observation content is carried as bounded scalar/summary
    fields; free-text or path-bearing model content (finding messages,
    stash labels, remote URLs, timestamps) is deliberately excluded.
    """

    project: str
    dry_run: bool
    proceed: bool
    approval_required: bool
    risk: str
    reasons: tuple[str, ...]
    actions: tuple[str, ...]
    snapshot_required: bool
    estimated_restart: bool
    stash_required: bool
    pull_required: bool
    git_exists: bool | None = None
    git_branch: str | None = None
    git_dirty: bool | None = None
    git_detached: bool | None = None
    git_ahead: int | None = None
    git_behind: int | None = None
    git_current_sha: str | None = None
    git_remote_sha: str | None = None
    git_modified_files: tuple[str, ...] | None = None
    git_untracked_files: tuple[str, ...] | None = None
    git_conflicted_files: tuple[str, ...] | None = None
    git_stash_count: int | None = None
    health_state: str | None = None
    health_score: int | None = None
    health_critical: int | None = None
    health_high: int | None = None
    health_warning: int | None = None
    health_info: int | None = None
    runtime_mode: str | None = None
    systemd_units: tuple[str, ...] | None = None
    systemd_action: str | None = None
    health_probe_contract: str | None = None
    service_name: str | None = None
    target_digest: str | None = None
    current_digest: str | None = None
    candidate_lookup_key: str | None = None
    dependency_scope: tuple[str, ...] | None = None
    atomicity: str | None = None
    observation_freshness: str | None = None
    version: str = PLAN_IDENTITY_VERSION

    def __post_init__(self) -> None:
        _validate_project(self.project)
        if self.version != PLAN_IDENTITY_VERSION:
            raise ValueError("Invalid plan identity version")
        for name in ("dry_run", "proceed", "approval_required", "snapshot_required", "estimated_restart", "stash_required", "pull_required"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"Invalid {name}")
        if not isinstance(self.risk, str) or self.risk not in _RISK_VALUES:
            raise ValueError("Invalid risk")
        _validate_text_tuple(self.reasons, "reasons")
        _validate_text_tuple(self.actions, "actions")
        if self.runtime_mode is not None:
            _validate_text(self.runtime_mode, "runtime_mode")
        if self.systemd_action is not None:
            _validate_text(self.systemd_action, "systemd_action")
        if self.health_probe_contract is not None:
            _validate_text(self.health_probe_contract, "health_probe_contract")
        if self.systemd_units is not None:
            _validate_text_tuple(self.systemd_units, "systemd_units")
        for name in ("git_modified_files", "git_untracked_files", "git_conflicted_files"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, tuple) or len(value) > MAX_IDENTITY_LIST:
                    raise ValueError(f"Invalid {name}")
                for entry in value:
                    _validate_text(entry, name)
        for name in ("git_exists", "git_dirty", "git_detached"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"Invalid {name}")
        if self.git_branch is not None:
            _validate_text(self.git_branch, "git_branch")
        for name in ("git_ahead", "git_behind", "git_stash_count", "health_score", "health_critical", "health_high", "health_warning", "health_info"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError(f"Invalid {name}")
        for name in ("git_current_sha", "git_remote_sha"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value or len(value) > MAX_GIT_SHA_LENGTH):
                raise ValueError(f"Invalid {name}")
        if self.health_state is not None:
            _validate_text(self.health_state, "health_state")
        if self.service_name is not None:
            _validate_text(self.service_name, "service_name")
        if self.target_digest is not None:
            _validate_text(self.target_digest, "target_digest")
        if self.current_digest is not None:
            _validate_text(self.current_digest, "current_digest")
        if self.candidate_lookup_key is not None:
            _validate_text(self.candidate_lookup_key, "candidate_lookup_key")
        if self.dependency_scope is not None:
            _validate_text_tuple(self.dependency_scope, "dependency_scope")
        if self.atomicity is not None:
            _validate_text(self.atomicity, "atomicity")
        if self.observation_freshness is not None:
            _validate_text(self.observation_freshness, "observation_freshness")

    def canonical_payload(self) -> dict[str, Any]:
        """Return the canonical payload; absent (None) fields are omitted."""

        payload: dict[str, Any] = {
            "actions": list(self.actions),
            "approval_required": self.approval_required,
            "dry_run": self.dry_run,
            "estimated_restart": self.estimated_restart,
            "project": self.project,
            "proceed": self.proceed,
            "pull_required": self.pull_required,
            "reasons": list(self.reasons),
            "risk": self.risk,
            "snapshot_required": self.snapshot_required,
            "stash_required": self.stash_required,
            "version": self.version,
        }
        optional = {
            "git_ahead": self.git_ahead,
            "git_behind": self.git_behind,
            "git_branch": self.git_branch,
            "git_conflicted_files": list(self.git_conflicted_files) if self.git_conflicted_files is not None else None,
            "git_current_sha": self.git_current_sha,
            "git_detached": self.git_detached,
            "git_dirty": self.git_dirty,
            "git_exists": self.git_exists,
            "git_modified_files": list(self.git_modified_files) if self.git_modified_files is not None else None,
            "git_remote_sha": self.git_remote_sha,
            "git_stash_count": self.git_stash_count,
            "git_untracked_files": list(self.git_untracked_files) if self.git_untracked_files is not None else None,
            "health_critical": self.health_critical,
            "health_high": self.health_high,
            "health_info": self.health_info,
            "health_score": self.health_score,
            "health_state": self.health_state,
            "health_warning": self.health_warning,
            "health_probe_contract": self.health_probe_contract,
            "runtime_mode": self.runtime_mode,
            "systemd_action": self.systemd_action,
            "systemd_units": list(self.systemd_units) if self.systemd_units is not None else None,
            "service_name": self.service_name,
            "target_digest": self.target_digest,
            "current_digest": self.current_digest,
            "candidate_lookup_key": self.candidate_lookup_key,
            "dependency_scope": list(self.dependency_scope) if self.dependency_scope is not None else None,
            "atomicity": self.atomicity,
            "observation_freshness": self.observation_freshness,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        return payload

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def canonical_bytes(self) -> bytes:
        return self.canonical_json().encode("utf-8")

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_plan(cls, plan: UpdatePlan) -> "UpdatePlanIdentity":
        """Extract the canonical identity from a planner-produced UpdatePlan."""

        if not isinstance(plan, UpdatePlan):
            raise ValueError("Invalid plan")
        git = plan.git
        health = plan.health_before
        raw_units = getattr(plan, "systemd_units", None)
        systemd_units = tuple(sorted(raw_units)) if raw_units else None
        return cls(
            project=plan.project,
            dry_run=plan.dry_run,
            proceed=plan.proceed,
            approval_required=plan.approval_required,
            risk=plan.risk.value if isinstance(plan.risk, UpdateRisk) else UpdateRisk(plan.risk).value,
            reasons=tuple(plan.reasons or ()),
            actions=tuple(plan.actions or ()),
            snapshot_required=plan.snapshot_required,
            estimated_restart=plan.estimated_restart,
            stash_required=plan.stash_required,
            pull_required=plan.pull_required,
            git_exists=None if git is None else bool(git.exists),
            git_branch=None if git is None else git.branch,
            git_dirty=None if git is None else bool(git.dirty),
            git_detached=None if git is None else bool(git.detached),
            git_ahead=None if git is None else int(git.ahead or 0),
            git_behind=None if git is None else int(git.behind or 0),
            git_current_sha=None if git is None else git.current_sha,
            git_remote_sha=None if git is None else git.remote_sha,
            git_modified_files=None if git is None else _normalize_file_list(git.modified_files),
            git_untracked_files=None if git is None else _normalize_file_list(git.untracked_files),
            git_conflicted_files=None if git is None else _normalize_file_list(git.conflicted_files),
            git_stash_count=None if git is None else len(git.stashes or ()),
            health_state=None if health is None else health.state.value,
            health_score=None if health is None else int(health.score),
            health_critical=None if health is None else int(health.critical),
            health_high=None if health is None else int(health.high),
            health_warning=None if health is None else int(health.warning),
            health_info=None if health is None else int(health.info),
            runtime_mode=getattr(plan, "runtime_mode", None),
            systemd_units=systemd_units,
            systemd_action=getattr(plan, "systemd_action", None),
            health_probe_contract=getattr(plan, "health_probe_contract", None),
        )

    @classmethod
    def from_service_plan(cls, plan: Any) -> "UpdatePlanIdentity":
        """Extract the canonical identity from a ServiceUpdatePlan."""
        actions = getattr(plan, "actions", None) or (
            (
                f"Update service {plan.service_name} to {plan.target_candidate_digest}",
                f"Verify health contract for {plan.service_name}",
            )
            if getattr(plan, "eligible", False)
            else ()
        )
        blocking_str = ""
        if hasattr(plan, "blocking_reason") and plan.blocking_reason:
            blocking_str = plan.blocking_reason.value if hasattr(plan.blocking_reason, "value") else str(plan.blocking_reason)
        reasons = getattr(plan, "reasons", None) or (
            (f"Candidate digest differs from running image for {plan.service_name}",)
            if getattr(plan, "eligible", False)
            else ((f"Service update blocked: {blocking_str}",) if blocking_str else ("Service update blocked",))
        )
        atomicity_val = plan.atomicity.value if hasattr(getattr(plan, "atomicity", None), "value") else str(getattr(plan, "atomicity", "leaf_independent"))
        rollback_design = getattr(plan, "rollback_design", None)
        snapshot_req = getattr(rollback_design, "snapshot_required", True) if rollback_design else True
        candidate_key = getattr(plan, "candidate_key", None)
        candidate_key_str = candidate_key.canonical_str() if hasattr(candidate_key, "canonical_str") else (str(candidate_key) if candidate_key else None)

        raw_deps = getattr(plan, "dependency_scope", ()) or ()
        dep_names = tuple(sorted(d.service_name if hasattr(d, "service_name") else str(d) for d in raw_deps))

        health_contract = getattr(plan, "health_contract", None)
        health_summary = health_contract.canonical_summary() if hasattr(health_contract, "canonical_summary") else None

        eligible = bool(getattr(plan, "eligible", False))
        risk = "low" if eligible and atomicity_val == "leaf_independent" else ("medium" if eligible else "blocked")

        return cls(
            project=getattr(plan, "project_name", "unknown"),
            dry_run=True,
            proceed=eligible,
            approval_required=True,
            risk=risk,
            reasons=tuple(reasons),
            actions=tuple(actions),
            snapshot_required=snapshot_req,
            estimated_restart=True,
            stash_required=False,
            pull_required=False,
            service_name=getattr(plan, "service_name", None),
            target_digest=getattr(plan, "target_candidate_digest", None),
            current_digest=getattr(plan, "current_runtime_digest", None),
            candidate_lookup_key=candidate_key_str,
            dependency_scope=dep_names if dep_names else None,
            atomicity=atomicity_val,
            observation_freshness=getattr(plan, "candidate_freshness", None),
            health_probe_contract=health_summary,
        )


def _normalize_file_list(values: Any) -> tuple[str, ...]:
    """Git file lists are unordered observations: sort and de-duplicate them."""

    if values is None:
        return ()
    return tuple(sorted({str(value) for value in values}))


def _validate_project(value: Any) -> None:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("Invalid project identity")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid project identity")
    if "/" in value or "\\" in value:
        raise ValueError("Invalid project identity")


def _validate_text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTITY_TEXT:
        raise ValueError(f"Invalid {name}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"Invalid {name}")


def _validate_text_tuple(values: Any, name: str) -> None:
    if not isinstance(values, tuple) or len(values) > MAX_IDENTITY_LIST:
        raise ValueError(f"Invalid {name}")
    for entry in values:
        _validate_text(entry, name)


def update_plan_digest(plan: UpdatePlan) -> str:
    """Convenience: digest of the canonical identity of a planner plan."""

    return UpdatePlanIdentity.from_plan(plan).digest()
