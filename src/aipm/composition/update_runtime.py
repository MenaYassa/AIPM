"""Composition-root adapter: canonical binding → engine execution contract.

Thin re-export of the canonical C6.2 update-runtime adapter, exposed under
the composition package so the control-plane composition root can bind the
runtime without importing from ``aipm.services`` (boundary-scanned: no
engine implementation imports are permitted inside
``aipm.control_plane``). The adapter itself remains the single canonical
seam — no second executor, no second runtime path is created here.
"""
from aipm.services.update.runtime_adapter import compose_update_runtime

__all__ = ["compose_update_runtime"]
