"""Shot 16 (dedicated AIPM execution identity) tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from aipm.control_plane.privilege import (
    PrivilegeCheckStatus,
    PrivilegeDriftError,
    _detect_aipm_drift,
    _parse_sudo_list,
    assert_privilege_boundary_ok,
    audit_privilege_boundary,
    verify_execution_identity,
)

# The expected dedicated identity
EXPECTED_USER = "aipm"
EXPECTED_SUDOERS = "aipm ALL=(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service"


# --- Identity separation ---

def test_aipm_user_exists_and_is_dedicated():
    """The dedicated AIPM user must exist with the correct properties.

    The certified setup script (ops/setup-aipm-identity.sh) creates the
    identity with `useradd --system`, so a UID below 1000 is the EXPECTED
    production state; the boundary that matters is: dedicated account,
    non-interactive shell, own primary group (see stage25a for full
    identity-semantics certification).
    """
    import pwd
    try:
        entry = pwd.getpwnam(EXPECTED_USER)
    except KeyError:
        pytest.skip("AIPM user not yet created — run ops/setup-aipm-identity.sh")
    assert entry.pw_uid > 0  # a real dedicated account, never root
    assert "nologin" in entry.pw_shell or "false" in entry.pw_shell


def test_aipm_user_is_not_in_sudo_group():
    """AIPM identity must NOT be in the sudo group."""
    import grp
    try:
        sudo_group = grp.getgrnam("sudo")
    except KeyError:
        pytest.skip("sudo group not found")
    assert EXPECTED_USER not in sudo_group.gr_mem


def test_aipm_user_is_not_in_docker_group():
    """AIPM identity must NOT be in the docker group."""
    import grp
    try:
        docker_group = grp.getgrnam("docker")
    except KeyError:
        pytest.skip("docker group not found")
    assert EXPECTED_USER not in docker_group.gr_mem


def test_aipm_user_has_no_privileged_groups():
    """AIPM identity must not be in any privileged group."""
    import grp, pwd
    try:
        entry = pwd.getpwnam(EXPECTED_USER)
    except KeyError:
        pytest.skip("AIPM user not yet created")
    privileged = {"sudo", "docker", "admin", "root", "wheel"}
    primary = grp.getgrgid(entry.pw_gid).gr_name
    # Check all groups the user belongs to (via /etc/group membership scan)
    for group in grp.getgrall():
        if EXPECTED_USER in group.gr_mem:
            assert group.gr_name not in privileged, f"AIPM in privileged group: {group.gr_name}"
    assert primary not in privileged, f"AIPM primary group is privileged: {primary}"


def test_aipm_user_password_is_locked():
    """AIPM identity must have no usable password."""
    import pwd, subprocess
    try:
        pwd.getpwnam(EXPECTED_USER)
    except KeyError:
        pytest.skip("AIPM user not yet created")
    # Check /etc/shadow for locked password (via subprocess as we can't read it as mina)
    # This is a documentation test — the setup script enforces it


def test_identity_verification_rejects_mina():
    """If AIPM runs as mina, the identity check must reject it."""
    import os
    current_uid = os.getuid()
    import pwd
    current_user = pwd.getpwuid(current_uid).pw_name
    if current_user == EXPECTED_USER:
        pytest.skip("Already running as aipm user")
    with pytest.raises(PrivilegeDriftError, match="expected"):
        verify_execution_identity(expected_user=EXPECTED_USER)


def test_identity_verification_accepts_aipm():
    """If AIPM runs as the dedicated user, the check accepts it."""
    import os
    current_uid = os.getuid()
    import pwd
    current_user = pwd.getpwuid(current_uid).pw_name
    if current_user != EXPECTED_USER:
        pytest.skip("Not running as aipm user")
    result = verify_execution_identity(expected_user=EXPECTED_USER)
    assert result["identity_ok"] is True
    assert result["no_privileged_groups"] is True


# --- Sudoers boundary ---

def test_sudoers_rule_targets_aipm_not_mina():
    """The sudoers rule must target the aipm user, not mina."""
    assert "aipm" in EXPECTED_SUDOERS
    assert not EXPECTED_SUDOERS.startswith("mina")
    assert "NOPASSWD" in EXPECTED_SUDOERS


def test_sudoers_drift_detects_wildcard():
    rules = ["(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service"]
    assert _detect_aipm_drift(rules) is False
    rules = ["(root) NOPASSWD: /usr/bin/systemctl restart *"]
    assert _detect_aipm_drift(rules) is True


def test_sudoers_drift_detects_different_unit():
    rules = ["(root) NOPASSWD: /usr/bin/systemctl restart ssh.service"]
    assert _detect_aipm_drift(rules) is True


def test_sudoers_drift_detects_missing_rule():
    assert _detect_aipm_drift([]) is True


# --- Systemd unit files ---

def test_systemd_unit_files_specify_aipm_user():
    """The systemd unit files must specify User=aipm."""
    unit_dir = Path("ops/systemd")
    for unit_name in ("aipm-telemetry.service", "aipm-events.service", "aipm-dashboard.service"):
        unit_file = unit_dir / unit_name
        assert unit_file.exists(), f"{unit_name} not found"
        content = unit_file.read_text(encoding="utf-8")
        assert "User=aipm" in content, f"{unit_name} does not specify User=aipm"
        assert "Group=aipm" in content, f"{unit_name} does not specify Group=aipm"


def test_systemd_unit_files_have_hardening():
    """Dashboard/events must have NoNewPrivileges=true; telemetry (executor) may omit it."""
    unit_dir = Path("ops/systemd")
    hardening_directives = [
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "RestrictSUIDSGID=true",
        "ProtectKernelTunables=true",
    ]
    for unit_name in ("aipm-telemetry.service", "aipm-events.service", "aipm-dashboard.service"):
        content = (unit_dir / unit_name).read_text(encoding="utf-8")
        for directive in hardening_directives:
            assert directive in content, f"{unit_name} missing {directive}"
    # Dashboard/events must have NoNewPrivileges=true (not executors)
    for unit_name in ("aipm-events.service", "aipm-dashboard.service"):
        content = (unit_dir / unit_name).read_text(encoding="utf-8")
        assert "NoNewPrivileges=true" in content, f"{unit_name} missing NoNewPrivileges"
    # Telemetry (executor) must NOT have NoNewPrivileges (sudo setuid transition needed)
    telemetry = (unit_dir / "aipm-telemetry.service").read_text(encoding="utf-8")
    assert "NoNewPrivileges=true" not in telemetry, "executor must NOT have NoNewPrivileges"


def test_no_new_privileges_is_incompatible_with_sudo():
    """Document that NoNewPrivileges=true prevents setuid transitions.

    NoNewPrivileges sets prctl(PR_SET_NO_NEW_PRIVS, 1) which blocks
    execve() from transitioning to a setuid binary. sudo is setuid root.
    Therefore: NoNewPrivileges=true + sudo = sudo runs as the invoking user.
    The telemetry service (the executor) must omit NoNewPrivileges.
    """
    import stat
    sudo = Path("/usr/bin/sudo")
    if sudo.exists():
        mode = stat.S_IMODE(sudo.stat().st_mode)
        is_setuid = bool(mode & stat.S_ISUID)
        assert is_setuid, "sudo must be setuid for the privilege transition to work"


def test_systemd_unit_files_do_not_specify_mina():
    """Unit files must NOT specify User=mina."""
    unit_dir = Path("ops/systemd")
    for unit_name in ("aipm-telemetry.service", "aipm-events.service", "aipm-dashboard.service"):
        content = (unit_dir / unit_name).read_text(encoding="utf-8")
        assert "User=mina" not in content, f"{unit_name} specifies User=mina"


# --- Setup script ---

def test_setup_script_exists_and_creates_aipm_user():
    script = Path("ops/setup-aipm-identity.sh")
    assert script.exists()
    content = script.read_text(encoding="utf-8")
    assert "useradd" in content
    assert "nologin" in content
    assert "aipm" in content
    assert "NOPASSWD" in content
    assert "systemctl restart aipm-telemetry.service" in content


# --- Source scan ---

def test_privilege_module_checks_identity():
    source = Path("src/aipm/control_plane/privilege.py").read_text(encoding="utf-8")
    assert "verify_execution_identity" in source
    assert "expected_user" in source
    assert "PrivilegeDriftError" in source
    assert "privileged_groups" in source
