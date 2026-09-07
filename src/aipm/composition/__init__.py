"""Composition-root adapters for cross-plane wiring.

Modules here bind canonical authorities together at the composition root
only. They exist OUTSIDE ``aipm.control_plane`` and
``aipm.capabilities.dashboard`` because both packages are boundary-scanned
(the control plane must not name or import engine implementation types;
the dashboard must not import execution machinery). No parallel approval,
confirmation, session, auth, audit, gate, lease, action, or digest
implementation lives here.
"""
from aipm.composition.update_digest import update_plan_digest_port
from aipm.composition.update_runtime import compose_update_runtime
from aipm.composition.executor_update import (
    compose_executor_update_handler,
    compose_ipc_update_runtime,
)

__all__ = [
    "compose_executor_update_handler",
    "compose_ipc_update_runtime",
    "compose_update_runtime",
    "update_plan_digest_port",
]
