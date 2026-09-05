"""Engine-side execution-contract binding for composed update execution.

This is the update-domain view of the authorization the control plane
already issued. It carries ONLY binding material the engine must verify
against the plan it is about to execute:

* ``project_name`` — the registered project identity the authorization bound;
* ``plan_digest`` — the canonical ``UpdatePlanIdentity`` digest of the exact
  plan the operator saw and approved;
* ``confirmation_id`` — reference to the consumed canonical confirmation
  (carried for traceability; the engine never re-validates it, the control
  plane consumed it under its own authority).

The engine performs an integrity/binding check only: it recomputes
``UpdatePlanIdentity.from_plan(plan).digest()`` and refuses (fail closed,
before any runtime mutation) when the contract is missing or mismatched.
Authorization semantics stay entirely in the control plane.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_PROJECT_PATTERN = re.compile(r"^[^\x00-\x1f\x7f/\\]{1,128}$")
_HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_HEX32_PATTERN = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class ExecutionContract:
    """Binding material linking one engine execution to one authorization.

    Immutable by construction; malformed values are rejected at creation so
    a malformed contract can never reach the engine's mutation boundary.
    """

    project_name: str
    plan_digest: str
    confirmation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.project_name, str) or _PROJECT_PATTERN.fullmatch(self.project_name) is None:
            raise ValueError("Invalid contract project identity")
        if not isinstance(self.plan_digest, str) or _HEX64_PATTERN.fullmatch(self.plan_digest) is None:
            raise ValueError("Invalid contract plan digest")
        if not isinstance(self.confirmation_id, str) or _HEX32_PATTERN.fullmatch(self.confirmation_id) is None:
            raise ValueError("Invalid contract confirmation reference")
