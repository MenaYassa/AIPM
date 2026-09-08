"""MC-6.13 C6.6-C: repository-side executor deployment contract.

Static validation only — no host interaction. Proves the checked-in base
unit keeps the executor capability-OFF and fail-closed, that the C6.6-B
update deployment contract exists and stays clearly non-active, and that
no least-privilege regression (wildcards, broad grants, secrets) enters
the repository artifacts.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[1]
BASE_UNIT = REPO / "ops" / "systemd" / "aipm-executor.service"
DEPLOYMENT_DOC = REPO / "docs" / "MC-6.13_EXECUTOR_UPDATE_DEPLOYMENT.md"
VALIDATOR = REPO / "ops" / "validate-release.py"


def _unit_text() -> str:
    return BASE_UNIT.read_text(encoding="utf-8")


def _doc_text() -> str:
    assert DEPLOYMENT_DOC.is_file(), "deployment contract doc is missing"
    return DEPLOYMENT_DOC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Base unit (capability-OFF, fail-closed)
# ---------------------------------------------------------------------------


def test_01_base_unit_carries_required_caller_allow_list():
    exec_line = next(line for line in _unit_text().splitlines() if line.startswith("ExecStart="))
    assert "--allowed-caller-uids 997" in exec_line
    assert "aipm executor run" in exec_line


def test_02_base_unit_never_enables_the_update_capability():
    assert "--enable-update-plan" not in _unit_text()


def test_03_base_unit_has_no_docker_supplementary_group():
    for line in _unit_text().splitlines():
        if line.startswith("SupplementaryGroups="):
            groups = line.split("=", 1)[1].split()
            assert "docker" not in groups
            assert groups == ["aipm-runtime"]


def test_04_base_unit_readwritepaths_are_confined_to_executor_state():
    for line in _unit_text().splitlines():
        if line.startswith("ReadWritePaths="):
            paths = line.split("=", 1)[1].split()
            assert paths == ["/var/lib/aipm-executor/state", "/var/lib/aipm-executor/logs"]
            assert not any(path.startswith("/home/ubuntu") for path in paths)


def test_05_base_unit_remains_af_unix_only():
    family_lines = [line for line in _unit_text().splitlines() if line.startswith("RestrictAddressFamilies=")]
    assert len(family_lines) == 1
    assert family_lines[0] == "RestrictAddressFamilies=AF_UNIX"


def test_06_base_unit_preserves_existing_hardening():
    text = _unit_text()
    for required in (
        "User=aipm-executor",
        "Group=aipm-executor",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "RestrictSUIDSGID=true",
        "RestrictNamespaces=true",
        "LockPersonality=true",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectControlGroups=true",
        "RuntimeDirectory=aipm",
        "RuntimeDirectoryMode=0750",
        "UMask=0077",
    ):
        assert required in text, required


def test_07_base_unit_runs_the_executor_identity_not_mina():
    assert "User=mina" not in _unit_text()


def test_08_validator_still_enforces_the_executor_unit_and_new_contract_doc():
    validator = VALIDATOR.read_text(encoding="utf-8")
    assert '"ops/systemd/aipm-executor.service"' in validator
    assert '"docs/MC-6.13_EXECUTOR_UPDATE_DEPLOYMENT.md"' in validator


# ---------------------------------------------------------------------------
# Deployment contract doc (clearly non-active template)
# ---------------------------------------------------------------------------


def test_09_deployment_doc_exists_and_declares_itself_non_active():
    text = _doc_text()
    assert "NOTHING IN THIS FILE IS INSTALLED OR ACTIVE" in text
    assert "TEMPLATE" in text
    assert "do not install without explicit authorization" in text.lower().replace("do not install without explicitly authorization", "do not install without explicit authorization")


def test_10_drop_in_template_declares_capability_and_caller_uid():
    text = _doc_text()
    assert "--enable-update-plan" in text
    assert "--allowed-caller-uids 997" in text
    assert "capability ON only when" in text or "Capability ON only when" in text


def test_11_template_documents_docker_group_and_network_families():
    text = _doc_text()
    assert "SupplementaryGroups=aipm-runtime docker" in text
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in text
    assert "RestrictAddressFamilies=AF_UNIX AF_INET" not in _unit_text()


def test_12_template_project_grants_are_enumerated_never_wildcarded():
    text = _doc_text()
    # A literal /home/ubuntu/* grant is forbidden anywhere except inside an
    # explicit rejection phrase, which must itself be present.
    rejection_present = "/home/ubuntu/*" in text and "never /home/ubuntu or /home/ubuntu/*" in text
    without_rejections = text.replace("never /home/ubuntu or /home/ubuntu/*", "")
    if not rejection_present:
        assert "/home/ubuntu/*" not in text
    readwrite_lines = [line.strip() for line in text.splitlines() if line.strip().startswith("ReadWritePaths=")]
    assert readwrite_lines, "template must contain ReadWritePaths lines"
    for line in readwrite_lines:
        for path in line.split("=", 1)[1].split():
            assert path != "/home/ubuntu" and not path.endswith("/*"), path
            assert not path.startswith("/home/ubuntu") or len(path) > len("/home/ubuntu/") + 1, path
    assert "/home/ubuntu or /home/ubuntu/*" in without_rejections or "never /home/ubuntu" in text


def test_13_no_wildcard_safe_directory_recommendation():
    text = _doc_text()
    assert re.search(r"^\s*directory\s*=\s*\*\s*$", text, re.MULTILINE) is None
    assert "REJECTED: `safe.directory = *`" in text or "REJECTED: `safe.directory=*`" in text or "safe.directory = *` (global wildcard)" in text


def test_14_path_contract_is_versioned():
    text = _doc_text()
    for required in (
        "/var/lib/aipm-executor/state/receipts.db",
        "/var/lib/aipm-executor/state/audit",
        "/var/lib/aipm-executor/state/backups",
        "/var/lib/aipm-executor/logs/executor.log",
        "AIPM_BACKUP_DIR",
        "AIPM_LOG_FILE",
        "AIPM_CONFIG",
        "--update-audit-dir",
        "--receipt-db",
    ):
        assert required in text, required


def test_15_config_template_uses_root_owned_read_only_contract():
    text = _doc_text()
    assert "/etc/aipm/executor/config.yaml" in text
    assert "0440" in text
    assert "root:aipm-executor" in text
    assert "refuses to start" in text or "refuses to start" in text.lower()


def test_16_registration_runbook_contains_validation_and_rollback():
    text = _doc_text()
    for required in (
        "core.sharedRepository",
        "chmod g+s",
        "setgid",
        "runuser -u aipm-executor --",
        "dubious-ownership",
        "Rollback:",
        "safe.directory",
    ):
        assert required in text, required


def test_17_no_secrets_in_deployment_artifacts():
    secret_patterns = (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"ghp_[A-Za-z0-9]{20,}",
        r"github_pat_[A-Za-z0-9_]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"sk-[A-Za-z0-9]{20,}",
        r"-----BEGIN OPENSSH PRIVATE KEY-----",
    )
    for path in (BASE_UNIT, DEPLOYMENT_DOC):
        text = path.read_text(encoding="utf-8")
        for pattern in secret_patterns:
            assert re.search(pattern, text) is None, (path.name, pattern)


def test_18_doc_does_not_instruct_copying_operator_config():
    text = _doc_text()
    assert "Never copy" in text or "never copy" in text.lower()
    assert "~/.config/aipm/config.yaml" in text
