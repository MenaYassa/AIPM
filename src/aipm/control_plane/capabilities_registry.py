"""Typed execution capability registry and executor registry.

This module is the permanent control-plane/execution-plane boundary for
capabilities. A capability is a typed, versioned definition that must declare
its complete safety posture before any executor may run it. Anything
undefined → the capability cannot execute (fail closed).

The registry is authoritative; provider self-description is descriptive
only. Fresh-install posture: only the control-plane plan mutation and its
dry-run variant are enabled; every external capability is disabled; the
arbitrary-script capability is permanently forbidden.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from aipm.control_plane.audit.sanitize import AuditEventError, bounded_reference

CAPABILITY_REGISTRY_VERSION = "mc612-capability-registry-v1"


class RiskClass(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PrivilegeClass(str, Enum):
    CONTROL_PLANE_INTERNAL = "control_plane_internal"
    SERVICE_ACCOUNT = "service_account"
    HOST_ROOT = "host_root"


class CapabilityId(str, Enum):
    """Closed capability vocabulary; no free-form capabilities exist."""

    APPLY_PROJECT_PLAN = "apply_project_plan"
    DRY_RUN_APPLY_PROJECT_PLAN = "dry_run_apply_project_plan"
    RESTART_ALLOWLISTED_SYSTEMD_UNIT = "restart_allowlisted_systemd_unit"
    UPDATE_GIT_REF = "update_git_ref"
    REBUILD_COMPOSE_STACK = "rebuild_compose_stack"
    DOCKER_MUTATION = "docker_mutation"
    ARBITRARY_SCRIPT = "arbitrary_script"


class CapabilityPolicyError(ValueError):
    """Raised when a capability definition or resolution fails closed."""


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    """Typed, immutable capability definition with its full safety posture.

    Every property required for safe execution must be explicitly declared.
    ``None`` anywhere it is not allowed → the capability cannot execute.
    """

    capability_id: CapabilityId
    version: str
    allowed_environments: frozenset[str]
    target_type: str
    reversible: bool
    snapshot_contract: str | None
    verification_contract: str | None
    reconciliation_contract: str | None
    risk_class: RiskClass
    privilege_class: PrivilegeClass
    enabled: bool
    automatic_rollback_allowed: bool = False
    permanently_forbidden: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", bounded_reference(self.version, field="capability version", maximum=64))
        normalized = frozenset(environment.strip() for environment in self.allowed_environments if isinstance(environment, str) and environment.strip())
        if self.enabled and not normalized:
            raise CapabilityPolicyError("Enabled capability must declare allowed environments")
        object.__setattr__(self, "allowed_environments", normalized)
        object.__setattr__(self, "target_type", bounded_reference(self.target_type, field="target type", maximum=64))
        if self.permanently_forbidden and self.enabled:
            raise CapabilityPolicyError("A permanently forbidden capability cannot be enabled")
        if self.enabled:
            for name in ("snapshot_contract", "verification_contract", "reconciliation_contract"):
                if getattr(self, name) is None:
                    raise CapabilityPolicyError(f"Enabled capability is missing its {name}")
        if self.automatic_rollback_allowed and not self.reversible:
            raise CapabilityPolicyError("Automatic rollback requires a reversible capability")

    def can_execute_in(self, environment: str) -> bool:
        if self.permanently_forbidden or not self.enabled:
            return False
        if environment != "staging":
            return False
        return environment in self.allowed_environments

    def safe_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id.value,
            "version": self.version,
            "allowed_environments": sorted(self.allowed_environments),
            "target_type": self.target_type,
            "reversible": self.reversible,
            "snapshot_contract": self.snapshot_contract,
            "verification_contract": self.verification_contract,
            "reconciliation_contract": self.reconciliation_contract,
            "risk_class": self.risk_class.value,
            "privilege_class": self.privilege_class.value,
            "enabled": self.enabled,
            "automatic_rollback_allowed": self.automatic_rollback_allowed,
            "permanently_forbidden": self.permanently_forbidden,
            "description": self.description,
        }


from typing import Any  # noqa: E402  (used in safe_dict annotation above)


def _definition(
    capability_id: CapabilityId,
    *,
    version: str,
    environments: tuple[str, ...],
    target_type: str,
    reversible: bool,
    snapshot: str | None,
    verification: str | None,
    reconciliation: str | None,
    risk: RiskClass,
    privilege: PrivilegeClass,
    enabled: bool,
    automatic_rollback: bool = False,
    permanently_forbidden: bool = False,
    description: str = "",
) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id=capability_id,
        version=version,
        allowed_environments=frozenset(environments),
        target_type=target_type,
        reversible=reversible,
        snapshot_contract=snapshot,
        verification_contract=verification,
        reconciliation_contract=reconciliation,
        risk_class=risk,
        privilege_class=privilege,
        enabled=enabled,
        automatic_rollback_allowed=automatic_rollback,
        permanently_forbidden=permanently_forbidden,
        description=description,
    )


#: Default fresh-install posture. External mutation is disabled; production is
#: structurally absent from every environment list.
_DEFAULT_CAPABILITIES: Mapping[CapabilityId, CapabilityDefinition] = {
    CapabilityId.APPLY_PROJECT_PLAN: _definition(
        CapabilityId.APPLY_PROJECT_PLAN,
        version="1",
        environments=("staging",),
        target_type="control_plane_project_plan",
        reversible=True,
        snapshot="mc612-snapshot-v1",
        verification="mc612-verification-v1",
        reconciliation="unknown-outcome-observation",
        risk=RiskClass.LOW,
        privilege=PrivilegeClass.CONTROL_PLANE_INTERNAL,
        enabled=True,
        automatic_rollback=True,
        description="CAS mutation of a control-plane ProjectPlan; the Shot-5/6 vertical slice.",
    ),
    CapabilityId.DRY_RUN_APPLY_PROJECT_PLAN: _definition(
        CapabilityId.DRY_RUN_APPLY_PROJECT_PLAN,
        version="1",
        environments=("staging",),
        target_type="control_plane_project_plan",
        reversible=True,
        snapshot="mc612-snapshot-v1",
        verification="record-echo",
        reconciliation="not-required",
        risk=RiskClass.LOW,
        privilege=PrivilegeClass.CONTROL_PLANE_INTERNAL,
        enabled=True,
        description="Records what a real executor would do; performs nothing.",
    ),
    CapabilityId.RESTART_ALLOWLISTED_SYSTEMD_UNIT: _definition(
        CapabilityId.RESTART_ALLOWLISTED_SYSTEMD_UNIT,
        version="0",
        environments=(),
        target_type="systemd_unit",
        reversible=False,
        snapshot=None,
        verification=None,
        reconciliation=None,
        risk=RiskClass.HIGH,
        privilege=PrivilegeClass.SERVICE_ACCOUNT,
        enabled=False,
        description="Typed preparation only: no unit allow-list, no verification contract; disabled.",
    ),
    CapabilityId.UPDATE_GIT_REF: _definition(
        CapabilityId.UPDATE_GIT_REF,
        version="0",
        environments=(),
        target_type="git_repository",
        reversible=False,
        snapshot=None,
        verification=None,
        reconciliation=None,
        risk=RiskClass.HIGH,
        privilege=PrivilegeClass.SERVICE_ACCOUNT,
        enabled=False,
        description="Blocked: repo/ref allow-lists, worktree snapshot, reconciliation undefined.",
    ),
    CapabilityId.REBUILD_COMPOSE_STACK: _definition(
        CapabilityId.REBUILD_COMPOSE_STACK,
        version="0",
        environments=(),
        target_type="compose_stack",
        reversible=False,
        snapshot=None,
        verification=None,
        reconciliation=None,
        risk=RiskClass.HIGH,
        privilege=PrivilegeClass.SERVICE_ACCOUNT,
        enabled=False,
        description="Blocked: project allow-list, image snapshot, down is non-reversible.",
    ),
    CapabilityId.DOCKER_MUTATION: _definition(
        CapabilityId.DOCKER_MUTATION,
        version="0",
        environments=(),
        target_type="docker_container",
        reversible=False,
        snapshot=None,
        verification=None,
        reconciliation=None,
        risk=RiskClass.CRITICAL,
        privilege=PrivilegeClass.SERVICE_ACCOUNT,
        enabled=False,
        description="Blocked: no bounded per-container contract exists.",
    ),
    CapabilityId.ARBITRARY_SCRIPT: _definition(
        CapabilityId.ARBITRARY_SCRIPT,
        version="0",
        environments=(),
        target_type="none",
        reversible=False,
        snapshot=None,
        verification=None,
        reconciliation=None,
        risk=RiskClass.CRITICAL,
        privilege=PrivilegeClass.HOST_ROOT,
        enabled=False,
        permanently_forbidden=True,
        description="Permanently forbidden: arbitrary command/script execution cannot be bounded.",
    ),
}


class CapabilityRegistry:
    """Immutable typed registry; resolution fails closed."""

    __slots__ = ("_capabilities", "_registry_version", "_initialized")

    def __init__(self, *, capabilities: Mapping[CapabilityId, CapabilityDefinition] | None = None) -> None:
        from types import MappingProxyType

        capabilities = _DEFAULT_CAPABILITIES if capabilities is None else capabilities
        if not isinstance(capabilities, Mapping) or not capabilities:
            raise CapabilityPolicyError("Capability registry cannot be empty")
        for key, value in capabilities.items():
            if not isinstance(key, CapabilityId) or not isinstance(value, CapabilityDefinition) or value.capability_id is not key:
                raise CapabilityPolicyError("Capability registry entries must be typed and self-consistent")
        object.__setattr__(self, "_capabilities", MappingProxyType(dict(capabilities)))
        object.__setattr__(self, "_registry_version", CAPABILITY_REGISTRY_VERSION)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("CapabilityRegistry configuration is immutable")
        object.__setattr__(self, name, value)

    @property
    def registry_version(self) -> str:
        return self._registry_version

    def resolve(self, capability_id: CapabilityId | str, *, version: str | None = None) -> CapabilityDefinition:
        """Resolve a capability; unknown id, unknown version, or disabled → denied."""

        try:
            key = capability_id if isinstance(capability_id, CapabilityId) else CapabilityId(capability_id)
        except ValueError as exc:
            raise CapabilityPolicyError("Unknown capability") from exc
        definition = self._capabilities.get(key)
        if definition is None:
            raise CapabilityPolicyError("Unknown capability")
        if version is not None and definition.version != version:
            raise CapabilityPolicyError("Capability version mismatch")
        return definition

    def require_executable(self, capability_id: CapabilityId | str, *, environment: str, version: str | None = None) -> CapabilityDefinition:
        definition = self.resolve(capability_id, version=version)
        if definition.permanently_forbidden:
            raise CapabilityPolicyError("Capability is permanently forbidden")
        if not definition.enabled:
            raise CapabilityPolicyError("Capability is disabled")
        if not definition.can_execute_in(environment):
            raise CapabilityPolicyError("Capability is not allowed in this environment")
        return definition

    def posture(self) -> dict[str, Any]:
        return {key.value: value.safe_dict() for key, value in self._capabilities.items()}


class ExecutorRegistry:
    """Explicit capability_id → executor implementation mapping.

    Executors are registered at application construction only; no import
    side effects, no dynamic selection from user input. Unknown capability,
    missing executor, or version mismatch → denied.
    """

    __slots__ = ("_executors", "_registry", "_initialized")

    def __init__(self, *, capability_registry: CapabilityRegistry) -> None:
        from types import MappingProxyType

        if not isinstance(capability_registry, CapabilityRegistry):
            raise TypeError("ExecutorRegistry requires the CapabilityRegistry")
        object.__setattr__(self, "_registry", capability_registry)
        object.__setattr__(self, "_executors", MappingProxyType({}))
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("ExecutorRegistry configuration is immutable")
        object.__setattr__(self, name, value)

    def register(self, *, capability_id: CapabilityId, executor) -> None:
        from types import MappingProxyType

        definition = self._registry.resolve(capability_id)
        if definition.permanently_forbidden:
            raise CapabilityPolicyError("A permanently forbidden capability cannot have an executor")
        key = (capability_id, definition.version)
        if key in self._executors:
            raise CapabilityPolicyError("Duplicate executor registration")
        executors = dict(self._executors)
        executors[key] = executor
        object.__setattr__(self, "_executors", MappingProxyType(executors))

    def resolve(self, capability_id: CapabilityId | str, *, version: str | None = None):
        definition = self._registry.resolve(capability_id, version=version)
        executor = self._executors.get((definition.capability_id, definition.version))
        if executor is None:
            raise CapabilityPolicyError("No executor registered for this capability/version")
        return definition, executor


#: Default registry instance for application construction.
DEFAULT_CAPABILITY_REGISTRY = CapabilityRegistry()


@dataclass(frozen=True, slots=True)
class TargetRecord:
    """Typed, resolved target binding; providers receive this, never strings.

    Resolved from the durable plan store by the control plane — a provider
    may not reinterpret target identifiers or select its own targets.
    """

    target_id: str
    plan_id: str
    environment: str
    allowed_capabilities: frozenset[CapabilityId]

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", bounded_reference(self.target_id, field="target id"))
        object.__setattr__(self, "plan_id", bounded_reference(self.plan_id, field="plan id"))
        object.__setattr__(self, "environment", bounded_reference(self.environment, field="environment", maximum=32))
        if not isinstance(self.allowed_capabilities, frozenset) or not all(isinstance(capability, CapabilityId) for capability in self.allowed_capabilities):
            raise CapabilityPolicyError("Target must declare typed allowed capabilities")


def resolve_target(*, target_id: str, plan_store, capability_registry: CapabilityRegistry, capability_id: CapabilityId) -> TargetRecord:
    """Resolve a target binding from the durable plan store; fail closed."""

    from aipm.control_plane.project_plan import ProjectPlan

    try:
        plan = plan_store.read(target_id)
    except Exception as exc:
        raise CapabilityPolicyError("Target is not registered") from exc
    if not isinstance(plan, ProjectPlan):
        raise CapabilityPolicyError("Target is not a registered ProjectPlan")
    definition = capability_registry.require_executable(capability_id, environment=plan.environment.value)
    allowed = frozenset({capability_id for candidate in CapabilityId if _supports_target(capability_registry, candidate, plan.environment.value, target_type=definition.target_type)})
    return TargetRecord(
        target_id=plan.target_id,
        plan_id=plan.digest()[:32],
        environment=plan.environment.value,
        allowed_capabilities=allowed,
    )


def _supports_target(registry: CapabilityRegistry, capability_id: CapabilityId, environment: str, *, target_type: str) -> bool:
    try:
        definition = registry.require_executable(capability_id, environment=environment)
    except CapabilityPolicyError:
        return False
    return definition.target_type == target_type


def validate_startup_configuration(
    *,
    owner_verifier_present: bool,
    capability_registry: CapabilityRegistry,
    database_path_permissions_ok: bool,
    staging_targets_registered: int,
    unsafe_bind_detected: bool = False,
) -> None:
    """Fail-closed startup validation for security-critical configuration.

    Rejects startup when: the owner verifier is missing, the capability
    registry is malformed, the database path is unsafe, no staging target is
    registered, or an unsafe bind address was configured. Security-critical
    configuration never receives silent defaults.
    """

    problems: list[str] = []
    if not owner_verifier_present:
        problems.append("owner authentication configuration is missing")
    if not isinstance(capability_registry, CapabilityRegistry):
        problems.append("capability registry is malformed or missing")
    if not database_path_permissions_ok:
        problems.append("control-plane database path is unsafe")
    if staging_targets_registered < 1:
        problems.append("no staging target is registered")
    if unsafe_bind_detected:
        problems.append("unsafe bind address configured")
    if problems:
        raise CapabilityPolicyError("Startup configuration rejected: " + "; ".join(problems))
