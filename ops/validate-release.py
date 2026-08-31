#!/usr/bin/env python3
"""Release validation for the MC-6.12 control plane.

Verifies that the repository is in a valid release state. Run from the
repository root. Exits nonzero on any validation failure. Read-only.

Usage:
    python ops/validate-release.py [--development]

Without --development, requires a clean Git tree and build metadata.
With --development, skips the clean-tree and build-metadata checks.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

REQUIRED_RUNTIME = [
    "src/aipm/control_plane/__init__.py",
    "src/aipm/control_plane/action_state.py",
    "src/aipm/control_plane/approval.py",
    "src/aipm/control_plane/bridge/__init__.py",
    "src/aipm/control_plane/capabilities_registry.py",
    "src/aipm/control_plane/contracts.py",
    "src/aipm/control_plane/executor.py",
    "src/aipm/control_plane/executor_ipc.py",
    "src/aipm/control_plane/gate.py",
    "src/aipm/control_plane/identity.py",
    "src/aipm/control_plane/kill_switch.py",
    "src/aipm/control_plane/lifecycle.py",
    "src/aipm/control_plane/models.py",
    "src/aipm/control_plane/mutation_receipt.py",
    "src/aipm/control_plane/owner_auth.py",
    "src/aipm/control_plane/planner.py",
    "src/aipm/control_plane/policy.py",
    "src/aipm/control_plane/privilege.py",
    "src/aipm/control_plane/project_plan.py",
    "src/aipm/control_plane/recovery.py",
    "src/aipm/control_plane/rollback.py",
    "src/aipm/control_plane/service.py",
    "src/aipm/control_plane/session.py",
    "src/aipm/control_plane/standalone_executor.py",
    "src/aipm/control_plane/storage/__init__.py",
    "src/aipm/control_plane/storage/schema.py",
    "src/aipm/control_plane/storage/sqlite_store.py",
    "src/aipm/control_plane/systemd_executor.py",
    "src/aipm/control_plane/systemd_provider.py",
    "src/aipm/control_plane/transport.py",
    "src/aipm/control_plane/verification.py",
    "src/aipm/control_plane/audit/__init__.py",
    "src/aipm/control_plane/audit/builders.py",
    "src/aipm/control_plane/audit/canonical.py",
    "src/aipm/control_plane/audit/chain.py",
    "src/aipm/control_plane/audit/models.py",
    "src/aipm/control_plane/audit/repository.py",
    "src/aipm/control_plane/audit/sanitize.py",
]

REQUIRED_SYSTEMD = [
    "ops/systemd/aipm-dashboard.service",
    "ops/systemd/aipm-events.service",
    "ops/systemd/aipm-executor.service",
    "ops/systemd/aipm-telemetry.service",
]

REQUIRED_OPS = [
    "ops/setup-aipm-identity.sh",
    "ops/migrate-aipm-state.sh",
]

REQUIRED_DOCS = [
    "docs/MC-6.12_PRODUCTION_ARCHITECTURE.md",
    "docs/MC-6.12_PRIVILEGE_BOUNDARY.md",
    "docs/MC-6.12_EXECUTOR_ARCHITECTURE.md",
    "docs/MC-6.12_SYSTEMD_RESTART.md",
    "docs/MC-6.12_OPERATOR_TRANSPORT.md",
]

FORBIDDEN_ARTIFACTS = [
    "realistic_vision_v6_b1.safetensors",
    "AGENTS.md",
    "commands.txt",
]

FORBIDDEN_DIRS = [
    "state/",
    "reports/",
    "logs/",
]


def _git(*args: str) -> str:
    result = subprocess.run(["git"] + list(args), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    development = "--development" in sys.argv
    errors: list[str] = []
    root = Path.cwd()

    # 1. Git tree status
    status = _git("status", "--porcelain")
    if not development and status:
        dirty_files = status.splitlines()
        errors.append(f"Git tree is dirty ({len(dirty_files)} files): {dirty_files[:5]}...")

    commit = _git("rev-parse", "HEAD")
    if not commit:
        if not development:
            errors.append("Cannot determine Git commit SHA")
        commit = "development"
    branch = _git("branch", "--show-current")

    # 2. Required runtime files
    for f in REQUIRED_RUNTIME:
        if not (root / f).is_file():
            errors.append(f"Missing required runtime file: {f}")

    # 3. Required systemd files
    for f in REQUIRED_SYSTEMD:
        if not (root / f).is_file():
            errors.append(f"Missing required systemd file: {f}")

    # 4. Required ops scripts
    for f in REQUIRED_OPS:
        if not (root / f).is_file():
            errors.append(f"Missing required ops file: {f}")

    # 5. Required documentation
    for f in REQUIRED_DOCS:
        if not (root / f).is_file():
            errors.append(f"Missing required documentation: {f}")

    # 6. Forbidden artifacts
    for f in FORBIDDEN_ARTIFACTS:
        if (root / f).exists():
            if not development:
                errors.append(f"Forbidden artifact in release tree: {f}")

    # 7. Build metadata (production only)
    if not development:
        meta_file = root / "build_meta.json"
        if not meta_file.is_file():
            errors.append("Missing build_meta.json (generate with ops/build-release.py)")
        else:
            try:
                meta = json.loads(meta_file.read_text())
                if meta.get("commit_sha") != commit:
                    errors.append(f"Build metadata commit mismatch: metadata={meta.get('commit_sha')}, HEAD={commit}")
            except json.JSONDecodeError:
                errors.append("Malformed build_meta.json")

    # 8. Systemd unit sanity: no User=mina in new units
    for f in REQUIRED_SYSTEMD:
        unit_text = (root / f).read_text(encoding="utf-8")
        if "User=mina" in unit_text:
            errors.append(f"{f} still specifies User=mina (must be User=aipm or User=aipm-executor)")

    # Report
    if errors:
        print("RELEASE VALIDATION FAILED:")
        for error in errors:
            print(f"  ✗ {error}")
        return 1

    print("RELEASE VALIDATION PASSED:")
    print(f"  commit: {commit}")
    print(f"  branch: {branch}")
    print(f"  mode: {'development' if development else 'production'}")
    print(f"  runtime files: {len(REQUIRED_RUNTIME)} OK")
    print(f"  systemd files: {len(REQUIRED_SYSTEMD)} OK")
    print(f"  ops files: {len(REQUIRED_OPS)} OK")
    print(f"  docs: {len(REQUIRED_DOCS)} OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
