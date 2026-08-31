"""MC-6.12 stage 25C — canonical executor filesystem contract.

The executor has exactly ONE persistent filesystem root:
`/var/lib/aipm-executor` (dedicated home, owner aipm-executor:aipm-runtime).

The canonical mutation-receipt database is
`/var/lib/aipm-executor/state/receipts.db`.

The competing root `/var/lib/aipm/executor` (a child of the control-plane
root) is FORBIDDEN: it conflates executor state with control-plane state and
grants the executor a foothold inside the control-plane tree. These tests
fail if it — or any other competing executor state root — is reintroduced in
production sources, systemd units, ops scripts, or MC-6.12 documentation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_EXECUTOR_ROOT = "/var/lib/aipm/executor"
CANONICAL_EXECUTOR_ROOT = "/var/lib/aipm-executor"
CANONICAL_RECEIPT_DB = "/var/lib/aipm-executor/state/receipts.db"

# Everything that constitutes "production" for this contract: the deployable
# runtime sources, the ops scripts, the systemd units shipped by the release,
# and the MC-6.12 architecture documents.
PRODUCTION_SCAN_TARGETS = [
    "src/aipm",
    "ops/setup-aipm-identity.sh",
    "ops/migrate-aipm-state.sh",
    "ops/systemd",
    "docs/MC-6.12_PRODUCTION_ARCHITECTURE.md",
    "docs/MC-6.12_PRIVILEGE_BOUNDARY.md",
    "docs/MC-6.12_EXECUTOR_ARCHITECTURE.md",
    "docs/MC-6.12_SYSTEMD_RESTART.md",
    "docs/MC-6.12_OPERATOR_TRANSPORT.md",
    "docs/MC-6.12_RELEASE_AND_CUTOVER.md",
]

# Patterns that would indicate a competing (non-canonical) executor state
# root. The trailing path-component boundary avoids matching the canonical
# root itself (e.g. "/var/lib/aipm-executor" must NOT be flagged).
# A receipts.db is "competing" only if it lives OUTSIDE .../state/ — the
# canonical DB is /var/lib/aipm-executor/state/receipts.db.
COMPETING_ROOT_PATTERNS = [
    re.compile(r"/var/lib/aipm/executor(?![\w-])"),
    re.compile(r"/var/lib/aipm-executor/(?!state/)[\w-]*receipts"),
]


def _iter_production_files() -> list[Path]:
    files: list[Path] = []
    for target in PRODUCTION_SCAN_TARGETS:
        path = REPO_ROOT / target
        if path.is_dir():
            files.extend(
                p for p in sorted(path.rglob("*.py"))
                if "__pycache__" not in p.parts
            )
        elif path.is_file():
            files.append(path)
    # .service units and .sh scripts under ops/
    ops = REPO_ROOT / "ops"
    files.extend(sorted(ops.glob("*.sh")))
    files.extend(sorted(ops.glob("systemd/*.service")))
    # docs
    files.extend(sorted((REPO_ROOT / "docs").glob("MC-6.12_*.md")))
    return sorted(set(files))


def _text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# F1 — forbidden competing root must not appear anywhere in production
# ---------------------------------------------------------------------------

def test_no_production_reference_to_forbidden_executor_root():
    offenders: list[str] = []
    for path in _iter_production_files():
        text = _text_of(path)
        for pattern in COMPETING_ROOT_PATTERNS:
            for m in pattern.finditer(text):
                line_no = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {m.group(0)!r}")
    assert not offenders, (
        "competing executor state root reintroduced in production:\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# F2 — CLI default receipt DB is the canonical path
# ---------------------------------------------------------------------------

def test_cli_receipt_db_default_is_canonical():
    app = REPO_ROOT / "src" / "aipm" / "cli" / "app.py"
    text = _text_of(app)
    assert CANONICAL_RECEIPT_DB in text, (
        "aipm executor run --receipt-db default must be "
        f"{CANONICAL_RECEIPT_DB}"
    )
    # and must appear as the default of the typer option (not just anywhere)
    m = re.search(
        r'receipt_db:\s*str\s*=\s*typer\.Option\(\s*"([^"]+)"', text
    )
    assert m is not None, "receipt_db typer.Option default not found"
    assert m.group(1) == CANONICAL_RECEIPT_DB


# ---------------------------------------------------------------------------
# F3 — systemd unit grants ONLY the canonical executor paths
# ---------------------------------------------------------------------------

def test_executor_unit_read_write_paths_are_canonical():
    unit = REPO_ROOT / "ops" / "systemd" / "aipm-executor.service"
    text = _text_of(unit)
    rwp = [
        line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if line.startswith("ReadWritePaths=")
    ]
    assert rwp, "aipm-executor.service must declare ReadWritePaths"
    paths = [p for entry in rwp for p in entry.split()]
    assert set(paths) == {
        f"{CANONICAL_EXECUTOR_ROOT}/state",
        f"{CANONICAL_EXECUTOR_ROOT}/logs",
    }, f"executor ReadWritePaths must be exactly the canonical pair, got {paths}"


def test_executor_unit_never_grants_control_plane_state():
    unit = REPO_ROOT / "ops" / "systemd" / "aipm-executor.service"
    text = _text_of(unit)
    for line in text.splitlines():
        if line.startswith("ReadWritePaths="):
            entry = line.split("=", 1)[1]
            for p in entry.split():
                assert not p.startswith("/var/lib/aipm/"), (
                    f"executor unit grants write access to control-plane "
                    f"path {p!r}"
                )
        if line.startswith(("ReadWritePaths+", "ReadOnlyPaths=", "BindReadOnlyPaths=")):
            if "/var/lib/aipm" in line:
                pytest.fail(f"executor unit references control-plane state: {line!r}")


def test_executor_unit_identity_and_sandbox_preserved():
    unit = REPO_ROOT / "ops" / "systemd" / "aipm-executor.service"
    text = _text_of(unit)
    required = [
        "User=aipm-executor",
        "Group=aipm-executor",
        "SupplementaryGroups=aipm-runtime",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "RestrictSUIDSGID=true",
        "RestrictNamespaces=true",
        "LockPersonality=true",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectControlGroups=true",
        "RestrictAddressFamilies=AF_UNIX",
        "UMask=0077",
        "RuntimeDirectory=aipm",
    ]
    missing = [d for d in required if d not in text]
    assert not missing, f"executor unit lost required directives: {missing}"
    # the executor must never join the aipm group
    assert re.search(r"^SupplementaryGroups=.*\baipm\b(?![-\w])", text, re.M) is None, (
        "executor unit must not add 'aipm' as a supplementary group"
    )


# ---------------------------------------------------------------------------
# F4 — setup script creates the canonical executor tree with the right owner
# ---------------------------------------------------------------------------

def test_setup_script_creates_canonical_executor_tree():
    script = REPO_ROOT / "ops" / "setup-aipm-identity.sh"
    text = _text_of(script)
    assert 'EXECUTOR_HOME="${AIPM_EXECUTOR_HOME:-/var/lib/aipm-executor}"' in text
    assert 'AIPM_EXECUTOR_STATE_DIR="${EXECUTOR_HOME}/state"' in text
    assert 'AIPM_EXECUTOR_LOG_DIR="${EXECUTOR_HOME}/logs"' in text
    # ownership model: executor owns the tree, runtime group traverses it
    assert 'chown -R "$EXECUTOR_USER:$RUNTIME_GROUP" "$EXECUTOR_HOME"' in text
    # and the permission table declares the same (header block, lines 22-33)
    lines = text.splitlines()
    table = "\n".join(
        lines[i] for i in range(len(lines))
        if 22 <= i + 1 <= 33
    )
    for row in ("/var/lib/aipm-executor", "/var/lib/aipm-executor/state", "/var/lib/aipm-executor/logs"):
        entry = next(
            (l for l in table.splitlines()
             if l.lstrip("# ").split() and l.lstrip("# ").split()[0] == row),
            None,
        )
        assert entry is not None, f"permission table missing canonical row {row!r}"
        parts = entry.lstrip("# ").split()
        assert parts[1] == "aipm-executor" and parts[2] == "aipm-runtime", (
            f"permission table row for {row!r} must be owner aipm-executor, "
            f"group aipm-runtime: {entry!r}"
        )


def test_setup_script_header_table_has_no_competing_rows():
    script = REPO_ROOT / "ops" / "setup-aipm-identity.sh"
    lines = _text_of(script).splitlines()
    table = "\n".join(lines[i] for i in range(len(lines)) if 22 <= i + 1 <= 33)
    assert FORBIDDEN_EXECUTOR_ROOT not in table


# ---------------------------------------------------------------------------
# F5 — documentation states exactly one canonical executor contract
# ---------------------------------------------------------------------------

def test_docs_declare_canonical_contract():
    arch = REPO_ROOT / "docs" / "MC-6.12_EXECUTOR_ARCHITECTURE.md"
    text = _text_of(arch)
    assert CANONICAL_RECEIPT_DB in text
    assert "## Filesystem contract" in text
    # executor must never gain access to control-plane state
    priv = REPO_ROOT / "docs" / "MC-6.12_PRIVILEGE_BOUNDARY.md"
    priv_text = _text_of(priv)
    assert "/var/lib/aipm-executor" in priv_text


def test_no_doc_mentions_forbidden_root():
    for doc in sorted((REPO_ROOT / "docs").glob("MC-6.12_*.md")):
        text = _text_of(doc)
        for pattern in COMPETING_ROOT_PATTERNS:
            m = pattern.search(text)
            assert m is None, (
                f"{doc.name} references forbidden executor root: {m.group(0)!r}"
            )


# ---------------------------------------------------------------------------
# F6 — migration script never touches the executor tree
# ---------------------------------------------------------------------------

def test_migration_script_confined_to_control_plane_state():
    script = REPO_ROOT / "ops" / "migrate-aipm-state.sh"
    text = _text_of(script)
    # migration is a control-plane concern; it must not mention executor paths
    assert CANONICAL_EXECUTOR_ROOT not in text
    assert FORBIDDEN_EXECUTOR_ROOT not in text


# ---------------------------------------------------------------------------
# F8 — filesystem-level isolation of control-plane state from the executor
# ---------------------------------------------------------------------------

def test_control_plane_home_is_private_to_aipm():
    """0700 aipm:aipm on /var/lib/aipm denies executor any traversal.

    The executor (uid aipm-executor, never a member of the aipm group —
    see test_membership_model_never_adds_executor_to_aipm) cannot read,
    write, or even list /var/lib/aipm, /var/lib/aipm/state,
    /var/lib/aipm/state/telemetry, or /var/lib/aipm/logs.
    """
    script = REPO_ROOT / "ops" / "setup-aipm-identity.sh"
    text = _text_of(script)
    assert 'chmod 0700 "$AIPM_HOME"' in text
    assert 'chown "$AIPM_USER:$AIPM_GROUP" "$AIPM_HOME"' in text
    # state/logs subtree stays group aipm (not aipm-runtime): executor is in
    # aipm-runtime, so group aipm grants it nothing.
    assert 'chown -R "$AIPM_USER:$AIPM_GROUP" "$AIPM_STATE_DIR" "$AIPM_LOG_DIR"' in text


def test_membership_model_never_adds_executor_to_aipm():
    script = REPO_ROOT / "ops" / "setup-aipm-identity.sh"
    text = _text_of(script)
    m = re.search(r'for membership in "([^"]+)" "([^"]+)" "([^"]+)"; do', text)
    assert m is not None, "membership loop not found"
    memberships = m.groups()
    assert sorted(memberships) == [
        "$AIPM_USER:$EXECUTOR_GROUP",
        "$AIPM_USER:$RUNTIME_GROUP",
        "$EXECUTOR_USER:$RUNTIME_GROUP",
    ], f"membership model must never add the executor to the aipm group: {memberships}"


def test_executor_sources_have_no_competing_receipt_path():
    for module in (
        REPO_ROOT / "src" / "aipm" / "control_plane" / "standalone_executor.py",
        REPO_ROOT / "src" / "aipm" / "control_plane" / "executor_ipc.py",
        REPO_ROOT / "src" / "aipm" / "control_plane" / "mutation_receipt.py",
    ):
        text = _text_of(module)
        for pattern in COMPETING_ROOT_PATTERNS:
            m = pattern.search(text)
            assert m is None, (
                f"{module.name} references competing executor path: {m.group(0)!r}"
            )
