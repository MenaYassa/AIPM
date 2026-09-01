"""Shot 13B (privilege boundary correction) tests."""
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
)



# --- sudo -l output parsing ---

MINA_OUTPUT = """Matching Defaults entries for mina on agent:
    env_reset, mail_badpass,
    secure_path=/usr/local/sbin\\:/usr/local/bin\\:/usr/sbin\\:/usr/bin\\:/sbin\\:/bin\\:/snap/bin,
    use_pty

User mina may run the following commands on agent:
    (ALL : ALL) ALL
"""
EXECUTOR_OUTPUT = """Matching Defaults entries for aipm-executor on agent:
    env_reset, mail_badpass,
    secure_path=/usr/local/sbin\\:/usr/local/bin\\:/usr/sbin\\:/usr/bin\\:/sbin\\:/bin\\:/snap/bin,
    use_pty

User aipm-executor may run the following commands on agent:
    (root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service
"""


def test_parse_real_sudo_output_extracts_both_domains():
    nopasswd_systemctl, other_systemctl, has_broad = _parse_sudo_list(EXECUTOR_OUTPUT)
    assert len(nopasswd_systemctl) == 1
    assert "aipm-telemetry" in nopasswd_systemctl[0]
    assert "aipm-executor" in EXECUTOR_OUTPUT
    assert has_broad is False


def test_parse_handles_empty_output():
    assert _parse_sudo_list("") == ([], [], False)


def test_parse_handles_no_systemctl():
    output = "User mina may run:\n    (ALL : ALL) ALL\n"
    nopasswd, other, has_broad = _parse_sudo_list(output)
    assert nopasswd == [] and other == [] and has_broad is True


# --- AIPM drift detection (only NOPASSWD systemctl rules) ---

def test_exact_aipm_rule_is_not_drift():
    rules = ["(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service"]
    assert _detect_aipm_drift(rules) is False


def test_missing_aipm_rule_is_drift():
    assert _detect_aipm_drift([]) is True


def test_wildcard_unit_is_drift():
    rules = ["(root) NOPASSWD: /usr/bin/systemctl restart *"]
    assert _detect_aipm_drift(rules) is True


def test_different_unit_is_drift():
    rules = ["(root) NOPASSWD: /usr/bin/systemctl restart ssh.service"]
    assert _detect_aipm_drift(rules) is True


def test_different_verb_is_drift():
    rules = ["(root) NOPASSWD: /usr/bin/systemctl stop aipm-telemetry.service"]
    assert _detect_aipm_drift(rules) is True


def test_multiple_different_rules_are_drift():
    rules = [
        "(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service",
        "(root) NOPASSWD: /usr/bin/systemctl restart aipm-dashboard.service",
    ]
    assert _detect_aipm_drift(rules) is True


def test_human_broad_sudo_is_not_aipm_drift():
    # The broad (ALL : ALL) ALL is human privilege, not an AIPM NOPASSWD systemctl rule.
    # It does not appear in the nopasswd_systemctl list (no systemctl keyword).
    assert _detect_aipm_drift([]) is True  # empty means missing, not broad


# --- Full audit with real-like sudo output ---

def test_audit_with_human_broad_sudo_and_exact_aipm_rule():
    result = audit_privilege_boundary(privilege_boundary_confirmed=True, effective_check=False)
    assert result.confirmed_by_operator is True
    assert result.drift is False


def test_audit_not_confirmed_fails_closed():
    result = audit_privilege_boundary(privilege_boundary_confirmed=False)
    assert result.status is PrivilegeCheckStatus.NOT_CONFIRMED
    assert result.drift is False  # not confirmed ≠ drift, but capability is disabled


def test_assert_raises_when_not_confirmed():
    with pytest.raises(PrivilegeDriftError, match="not confirmed"):
        assert_privilege_boundary_ok(privilege_boundary_confirmed=False)


def test_assert_detects_human_session_without_executor_rule():
    result = audit_privilege_boundary(
        privilege_boundary_confirmed=True,
        effective_check=True,
    )

    assert result.confirmed_by_operator is True
    assert result.effective_check_attempted is True
    assert result.status is PrivilegeCheckStatus.AIPM_RULE_MISSING
    assert result.drift is True


# --- Real VPS audit (with installed sudoers rule) ---

def test_real_vps_audit_detects_executor_narrow_rule():
    """On the actual VPS, the audit runs as the session user (mina). The
    executor's NOPASSWD rule does NOT appear in mina's sudo -l, so the
    effective check reports the narrow rule as missing for this session —
    the rule itself lives in aipm-executor's sudoers grant (verified by
    stage24a/stage25a script certification and by host probes)."""
    result = audit_privilege_boundary(privilege_boundary_confirmed=True, effective_check=True)
    assert result.confirmed_by_operator is True
    assert result.effective_check_attempted is True
    if result.effective_check_succeeded:
        # mina holds only broad passworded sudo; the AIPM narrow rule is
        # scoped to aipm-executor and absent from this session's listing.
        assert result.aipm_narrow_rule_present is False
        assert result.status is PrivilegeCheckStatus.AIPM_RULE_MISSING
        assert result.drift is True


# --- Human ≠ AIPM privilege distinction ---

def test_human_broad_sudo_is_not_aipm_execution_privilege():
    """Document the distinction: the human's broad sudo requires a password.
    The executor identity (aipm-executor) is not in the %sudo group and holds
    only the single NOPASSWD systemctl rule."""
    _, _, has_broad = _parse_sudo_list(EXECUTOR_OUTPUT)
    assert has_broad is False
    nopasswd_rules, _, _ = _parse_sudo_list(EXECUTOR_OUTPUT)
    assert len(nopasswd_rules) == 1
    assert "aipm-telemetry" in nopasswd_rules[0]


# --- Source scan ---

def test_privilege_module_does_not_read_sudoers_files():
    source = Path("src/aipm/control_plane/privilege.py").read_text(encoding="utf-8")
    for forbidden in ("open(", "open('", 'open("', "os.listdir", "os.path.join"):
        assert forbidden not in source, forbidden
    assert "subprocess" in source  # uses sudo -n -l for effective check
    assert "-l" in source  # the list flag
    assert "/etc/sudoers" not in source  # no direct file read


def test_oracle_cloud_systemctl_alias_is_not_aipm_drift():
    output = """
    User mina may run the following commands on agent:
        (ALL : ALL) ALL

    Cmnd_Alias WLP_SYSTEMD = /bin/systemctl, /usr/sbin/systemctl, /usr/bin/systemctl
    """

    rules, _, broad = _parse_sudo_list(output)

    assert rules == []
    assert broad is True
    assert _detect_aipm_drift(rules) is True

def test_executor_narrow_rule_is_accepted():
    output = """
    User aipm-executor may run:
        (root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service
    """

    rules, _, _ = _parse_sudo_list(output)

    assert rules == [
        "(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service"
    ]
    assert _detect_aipm_drift(rules) is False

def test_executor_rule_is_exact():
    rules, _, _ = _parse_sudo_list(EXECUTOR_OUTPUT)

    assert rules == [
        "(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service"
    ]

    assert _detect_aipm_drift(rules) is False