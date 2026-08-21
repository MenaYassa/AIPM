from __future__ import annotations

from aipm.control_plane.models import OperationKind, RiskLevel


_ALLOWED_OPERATIONS = frozenset({OperationKind.UPDATE_PROJECT_PLAN})


def validate_operation(operation: OperationKind) -> None:
    if not isinstance(operation, OperationKind) or operation not in _ALLOWED_OPERATIONS:
        raise ValueError("unsupported operation")


def risk_for(operation: OperationKind) -> RiskLevel:
    validate_operation(operation)
    return RiskLevel.LOW


def allowed_operations() -> frozenset[OperationKind]:
    return _ALLOWED_OPERATIONS
