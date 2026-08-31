"""Shot 11 boundary scans: control/execution-plane separation + docs."""
from __future__ import annotations

from pathlib import Path


def test_execution_plane_never_imports_control_plane_authority():
    execution_plane = ("executor.py", "recovery.py", "capabilities_registry.py")
    forbidden = (
        "OwnerAuthenticator",
        "OwnerSessionStore",
        "from aipm.control_plane.transport",
        "AuthorizationPolicy",
        "aipm.dashboard",
        "aipm.capabilities.dashboard",
    )
    for name in execution_plane:
        source = (Path("src/aipm/control_plane") / name).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, (name, token)


def test_transport_never_imports_execution_internals_directly():
    source = Path("src/aipm/control_plane/transport.py").read_text(encoding="utf-8")
    # The transport may import the ExecutionRefused exception type for error
    # mapping, but must not construct contracts, run executors, or touch recovery.
    for forbidden in ("Executor(", "ExecutionContract(", "from aipm.control_plane.recovery import", "executor.execute"):
        assert forbidden not in source, forbidden


def test_production_architecture_doc_exists_and_covers_the_planes():
    doc = Path("docs/MC-6.12_PRODUCTION_ARCHITECTURE.md").read_text(encoding="utf-8")
    for section in ("CONTROL PLANE", "EXECUTION PLANE", "Capability registry", "Recovery", "kill switch", "Target binding", "External execution boundary"):
        assert section.lower() in doc.lower(), section


def test_capability_registry_source_has_no_dangerous_imports():
    source = Path("src/aipm/control_plane/capabilities_registry.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "socket", "sqlite3.connect", "urllib"):
        assert forbidden not in source, forbidden
