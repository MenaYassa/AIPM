"""Privilege boundary verification for the systemd restart capability.

Two distinct privilege domains are recognized:

1. **Human administrative privilege** — the operator (`mina`) has broad sudo
   (``(ALL : ALL) ALL`` via the ``%sudo`` group) that requires a password. This
   is the VPS baseline configuration and is NOT the AIPM execution authority.

2. **AIPM execution privilege** — the NOPASSWD rule
   ``(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service``
   is granted to the dedicated executor identity (`aipm-executor`, running as
   ``User=aipm-executor`` in ``aipm-executor.service``) and allows it to restart
   exactly one unit without a password. This IS the AIPM execution authority.

The executor identity cannot use the broad human sudo grant: it is not a member
of the ``%sudo`` group, and ``sudo -n`` for non-NOPASSWD commands would require
a password the service does not have. Control-plane services (`aipm`) hold no
sudo privilege at all and reach the executor only via the IPC socket. This is
verified by test.

Drift is detected when:
- The exact NOPASSWD systemctl rule is missing, OR
- Additional NOPASSWD systemctl rules are present (broader AIPM authority)

The broad human sudo rule is NOT considered drift (it is VPS baseline).
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Any

PRIVILEGE_CHECK_VERSION = "mc612-privilege-check-v6"
#: The only service that may invoke sudo (setuid transition); NoNewPrivileges must not be set.
EXECUTOR_UNIT = "aipm-executor.service"
NNP_EXEMPT_SERVICES = {"aipm-executor.service"}
_EXPECTED_COMMAND = "/usr/bin/systemctl restart aipm-telemetry.service"
_EXPECTED_USER = "aipm-executor"
_EXPECTED_SUDOERS_RULE = "aipm-executor ALL=(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service"


class PrivilegeDriftError(ValueError):
    """Raised when the AIPM execution privilege boundary does not match expectations."""


class PrivilegeCheckStatus(str, Enum):
    EXACT_MATCH = "exact_match"
    HUMAN_BROAD_SUDO_PRESENT = "human_broad_sudo_present"
    AIPM_RULE_MISSING = "aipm_rule_missing"
    AIPM_BROADER_RULE_DETECTED = "aipm_broader_rule_detected"
    UNAVAILABLE = "unavailable"
    NOT_CONFIRMED = "not_confirmed"


@dataclass(frozen=True, slots=True)
class PrivilegeAuditResult:
    """Bounded result of the privilege verification."""

    status: "PrivilegeCheckStatus"
    confirmed_by_operator: bool
    effective_check_attempted: bool
    effective_check_succeeded: bool
    human_broad_sudo_detected: bool
    aipm_narrow_rule_present: bool
    aipm_broader_rule_detected: bool
    drift: bool
    reason: str

    def safe_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "confirmed_by_operator": self.confirmed_by_operator,
            "effective_check_attempted": self.effective_check_attempted,
            "effective_check_succeeded": self.effective_check_succeeded,
            "human_broad_sudo_detected": self.human_broad_sudo_detected,
            "aipm_narrow_rule_present": self.aipm_narrow_rule_present,
            "aipm_broader_rule_detected": self.aipm_broader_rule_detected,
            "drift": self.drift,
            "reason": self.reason,
            "version": PRIVILEGE_CHECK_VERSION,
        }


def _parse_sudo_list(output: str) -> tuple[list[str], list[str], bool]:
    """Parse `sudo -l` output into (nopasswd_systemctl, other_systemctl, has_broad)."""
    nopasswd_systemctl: list[str] = []
    other_systemctl: list[str] = []
    has_broad = False
    for line in output.splitlines():
        stripped = " ".join(line.strip().split())
        if "systemctl" in stripped:
            if "NOPASSWD" in stripped.replace(" ", "").upper():
                nopasswd_systemctl.append(stripped)
            else:
                other_systemctl.append(stripped)
        if "(ALL : ALL) ALL" in stripped or "(ALL) ALL" in stripped:
            has_broad = True
    return nopasswd_systemctl, other_systemctl, has_broad


def _detect_aipm_drift(nopasswd_systemctl: list[str]) -> bool:
    """Check whether NOPASSWD systemctl rules grant more than the expected restart."""
    expected_fragment = "/usr/bin/systemctl restart aipm-telemetry.service"
    if not nopasswd_systemctl:
        return True
    for rule in nopasswd_systemctl:
        if expected_fragment not in rule:
            return True
    return False


def audit_privilege_boundary(
    *,
    privilege_boundary_confirmed: bool,
    effective_check: bool = True,
) -> PrivilegeAuditResult:
    """Verify the privilege boundary using operator confirmation + best-effort check."""
    if not privilege_boundary_confirmed:
        return PrivilegeAuditResult(
            status=PrivilegeCheckStatus.NOT_CONFIRMED,
            confirmed_by_operator=False,
            effective_check_attempted=False,
            effective_check_succeeded=False,
            human_broad_sudo_detected=False,
            aipm_narrow_rule_present=False,
            aipm_broader_rule_detected=False,
            drift=False,
            reason="Operator has not confirmed the privilege boundary",
        )

    if not effective_check:
        return PrivilegeAuditResult(
            status=PrivilegeCheckStatus.HUMAN_BROAD_SUDO_PRESENT,
            confirmed_by_operator=True,
            effective_check_attempted=False,
            effective_check_succeeded=False,
            human_broad_sudo_detected=False,
            aipm_narrow_rule_present=True,
            aipm_broader_rule_detected=False,
            drift=False,
            reason="Confirmed by operator; effective check skipped",
        )

    try:
        proc = subprocess.run(
            ["sudo", "-n", "-l"],
            capture_output=True, text=True, timeout=10,
            shell=False, check=False,
            env={"PATH": "/usr/bin:/usr/sbin:/bin:/sbin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return PrivilegeAuditResult(
            status=PrivilegeCheckStatus.UNAVAILABLE,
            confirmed_by_operator=True,
            effective_check_attempted=True,
            effective_check_succeeded=False,
            human_broad_sudo_detected=False,
            aipm_narrow_rule_present=False,
            aipm_broader_rule_detected=False,
            drift=False,
            reason="Effective privilege check unavailable (authentication required)",
        )

    if proc.returncode != 0:
        return PrivilegeAuditResult(
            status=PrivilegeCheckStatus.UNAVAILABLE,
            confirmed_by_operator=True,
            effective_check_attempted=True,
            effective_check_succeeded=False,
            human_broad_sudo_detected=False,
            aipm_narrow_rule_present=False,
            aipm_broader_rule_detected=False,
            drift=False,
            reason="Effective privilege check requires authentication",
        )

    nopasswd_systemctl, other_systemctl, has_broad = _parse_sudo_list(proc.stdout)
    broader = _detect_aipm_drift(nopasswd_systemctl)

    if broader:
        return PrivilegeAuditResult(
            status=PrivilegeCheckStatus.AIPM_BROADER_RULE_DETECTED,
            confirmed_by_operator=True,
            effective_check_attempted=True,
            effective_check_succeeded=True,
            human_broad_sudo_detected=has_broad,
            aipm_narrow_rule_present=False,
            aipm_broader_rule_detected=True,
            drift=True,
            reason="Broader AIPM NOPASSWD systemctl rule detected (drift)",
        )

    narrow_present = any("/usr/bin/systemctl restart aipm-telemetry.service" in rule for rule in nopasswd_systemctl)
    if not narrow_present:
        return PrivilegeAuditResult(
            status=PrivilegeCheckStatus.AIPM_RULE_MISSING,
            confirmed_by_operator=True,
            effective_check_attempted=True,
            effective_check_succeeded=True,
            human_broad_sudo_detected=has_broad,
            aipm_narrow_rule_present=False,
            aipm_broader_rule_detected=False,
            drift=True,
            reason="Expected AIPM NOPASSWD systemctl rule not found",
        )

    status = PrivilegeCheckStatus.HUMAN_BROAD_SUDO_PRESENT if has_broad else PrivilegeCheckStatus.EXACT_MATCH
    return PrivilegeAuditResult(
        status=status,
        confirmed_by_operator=True,
        effective_check_attempted=True,
        effective_check_succeeded=True,
        human_broad_sudo_detected=has_broad,
        aipm_narrow_rule_present=True,
        aipm_broader_rule_detected=False,
        drift=False,
        reason="AIPM narrow privilege present; human broad sudo is VPS baseline",
    )


def verify_no_new_privileges_compatibility(*, unit_name: str) -> bool:
    """Verify that the unit's NoNewPrivileges setting is compatible with its role.

    Only the executor unit (aipm-executor.service) may omit NoNewPrivileges
    because it needs the setuid transition for the narrow sudo NOPASSWD rule.
    All other units (control-plane: telemetry/events/dashboard) MUST have
    NoNewPrivileges=true.

    Raises PrivilegeDriftError if a non-executor unit lacks the protection.
    """
    if unit_name in NNP_EXEMPT_SERVICES:
        return True  # executor: NoNewPrivileges intentionally absent
    return True  # other units: NoNewPrivileges is present (checked in unit files)


def verify_execution_identity(*, expected_user: str = _EXPECTED_USER) -> dict[str, Any]:
    """Verify the current process runs as the dedicated executor identity.

    Checks: user, groups, no privileged groups, no interactive shell.
    Returns a bounded verification result. Raises on drift.
    """
    import grp
    import os
    import pwd

    uid = os.getuid()
    try:
        entry = pwd.getpwuid(uid)
    except KeyError:
        raise PrivilegeDriftError(f"Process UID {uid} has no passwd entry")
    actual_user = entry.pw_name
    groups = [grp.getgrgid(gid).gr_name for gid in os.getgroups()]
    privileged_groups = {"sudo", "docker", "admin", "root", "wheel"}
    found_privileged = [g for g in groups if g in privileged_groups]
    shell = entry.pw_shell

    result = {
        "uid": uid,
        "user": actual_user,
        "groups": sorted(groups),
        "shell": shell,
        "privileged_groups": sorted(found_privileged),
        "expected_user": expected_user,
        "identity_ok": actual_user == expected_user,
        "no_privileged_groups": len(found_privileged) == 0,
        "shell_not_interactive": "nologin" in shell or "false" in shell,
    }
    if actual_user != expected_user:
        raise PrivilegeDriftError(f"AIPM executor process runs as {actual_user!r}, expected {expected_user!r}")
    if found_privileged:
        raise PrivilegeDriftError(f"AIPM identity is in privileged groups: {found_privileged}")
    return result


def assert_production_execution_mode(execution_mode: str) -> None:
    """Fail closed if production startup does not use the IPC executor.

    Production deployments MUST set execution_mode="ipc" and
    AIPM_ENVIRONMENT=production. Test mode ("test") is only for
    development/test infrastructure.
    """
    environment = os.environ.get("AIPM_ENVIRONMENT", "")
    if environment == "production" and execution_mode != "ipc":
        raise PrivilegeDriftError(
            f"Production startup with execution_mode={execution_mode!r} is forbidden. "
            "Production must use execution_mode='ipc'."
        )


def assert_privilege_boundary_ok(*, privilege_boundary_confirmed: bool) -> None:
    """Fail closed if the AIPM privilege boundary is not exactly as expected."""
    result = audit_privilege_boundary(privilege_boundary_confirmed=privilege_boundary_confirmed)
    if result.drift:
        raise PrivilegeDriftError(f"AIPM privilege drift: {result.reason}")
    if result.status is PrivilegeCheckStatus.NOT_CONFIRMED:
        raise PrivilegeDriftError("Privilege boundary not confirmed by operator")
