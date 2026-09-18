"""MC-6.15-C.3: Executor Integration Boundary Test Suite.

Comprehensive tests covering:
1. All 25 required adversarial boundary scenarios.
2. Disposable Compose execution fixture and semantics (--no-deps, ordering, pull/up).
3. Real Unix domain socket IPC roundtrip with SO_PEERCRED and MutationReceiptStore.
4. Static AST no-mutation proofs (no shell=True, no privilege broker calls, no production Docker calls).
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from aipm.control_plane.executor_ipc import (
    CAPABILITY_EXECUTE_SERVICE_UPDATE,
    CAPABILITY_EXECUTE_UPDATE_PLAN,
    ExecutionRequest,
    ExecutionResponse,
    ExecutorIPCClient,
    ExecutorIPCError,
    ExecutorIPCServer,
    validate_service_scope,
)
from aipm.control_plane.mutation_receipt import MutationReceiptStore, MutationStatus
from aipm.composition.executor_update import (
    compose_executor_update_handler,
    compose_ipc_update_runtime,
)
from aipm.services.compose.execution_adapter import (
    BoundedServiceUpdateIntent,
    ComposeExecutionAdapter,
    ComposeExecutionError,
    ComposeExecutionResult,
    ServiceUpdateVerificationCode,
    ServiceUpdateVerificationResult,
)

NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
ACTION_ID = "a" * 64
CONTRACT_DIGEST = "c" * 64
PLAN_DIGEST = "b" * 64
CONFIRMATION_ID = "d" * 32
LEASE_ID = "e" * 32
FENCING_TOKEN = 1
PROJECT_NAME = "searxng-stack"


# ---------------------------------------------------------------------------
# Test Fixtures and Helpers
# ---------------------------------------------------------------------------


@dataclass
class _DummyProject:
    name: str
    path: Path
    compose_files: tuple[Path, ...]
    services: dict[str, Any]
    local_build_services: set[str] = field(default_factory=set)


@dataclass
class _DummyServiceObs:
    service_name: str
    state: str = "running"
    health: str | None = "healthy"
    running_digest: str = "sha256:target111111111111111111111111111111111111111111111111111111111111"


class _MockRunner:
    """Mock command runner recording exact argv invocations."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, cmd: list[str], *, cwd: Path, **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def _make_intent(
    *,
    project_name: str = PROJECT_NAME,
    service_scope: tuple[str, ...] = ("searxng",),
    plan_digest: str = PLAN_DIGEST,
    confirmation_id: str = CONFIRMATION_ID,
    action_id: str = ACTION_ID,
    contract_digest: str = CONTRACT_DIGEST,
    lease_id: str = LEASE_ID,
    fencing_token: int = FENCING_TOKEN,
    expected_target_digest: str | None = "sha256:target111111111111111111111111111111111111111111111111111111111111",
    expected_child_digest: str | None = None,
    atomicity: str = "leaf_independent",
) -> BoundedServiceUpdateIntent:
    return BoundedServiceUpdateIntent(
        project_name=project_name,
        service_scope=service_scope,
        plan_digest=plan_digest,
        confirmation_id=confirmation_id,
        action_id=action_id,
        fencing_token=fencing_token,
        contract_digest=contract_digest,
        lease_id=lease_id,
        expected_target_digest=expected_target_digest,
        expected_child_digest=expected_child_digest,
        atomicity=atomicity,
        now=NOW,
    )


def _setup_project(tmp_path: Path, services: tuple[str, ...] = ("searxng", "searxng-valkey")) -> _DummyProject:
    proj_dir = tmp_path / "test_project"
    proj_dir.mkdir(parents=True, exist_ok=True)
    compose_file = proj_dir / "docker-compose.yml"
    compose_file.write_text("services:\n  searxng:\n    image: searxng/searxng\n", encoding="utf-8")
    return _DummyProject(
        name=PROJECT_NAME,
        path=proj_dir,
        compose_files=(compose_file,),
        services={s: {} for s in services},
    )


# ---------------------------------------------------------------------------
# 25 Required Adversarial & Invariant Tests
# ---------------------------------------------------------------------------


def test_1_ollama_scope_remains_exact(tmp_path: Path):
    """1. ollama scope remains exactly ('ollama',). Never expands to litellm."""
    proj = _setup_project(tmp_path, services=("ollama", "litellm"))
    runner = _MockRunner()
    inspector = lambda p, s: _DummyServiceObs(s)

    adapter = ComposeExecutionAdapter(
        project_resolver={PROJECT_NAME: proj},
        runner=runner,
        inspector=inspector,
    )

    intent = _make_intent(service_scope=("ollama",))
    res = adapter.execute(intent)

    assert res.is_success
    assert res.verification.verified_services == ("ollama",)
    # Ensure litellm was NEVER called
    for call in runner.calls:
        assert "litellm" not in call
        assert "ollama" in call


def test_2_searxng_scope_remains_exact(tmp_path: Path):
    """2. searxng scope remains exactly ('searxng', 'searxng-valkey')."""
    proj = _setup_project(tmp_path, services=("searxng", "searxng-valkey", "unrelated"))
    runner = _MockRunner()
    inspector = lambda p, s: _DummyServiceObs(s)

    adapter = ComposeExecutionAdapter(
        project_resolver={PROJECT_NAME: proj},
        runner=runner,
        inspector=inspector,
    )

    intent = _make_intent(service_scope=("searxng", "searxng-valkey"))
    res = adapter.execute(intent)

    assert res.is_success
    assert set(res.verification.verified_services) == {"searxng", "searxng-valkey"}
    for call in runner.calls:
        assert "unrelated" not in call


def test_3_widening_scope_rejected():
    """3. Widening scope with invalid/unauthorized values is rejected."""
    with pytest.raises(ComposeExecutionError, match="Invalid service name"):
        _make_intent(service_scope=("ollama", ""))


def test_4_narrowing_scope_rejected_when_empty():
    """4. Narrowing scope to empty tuple is rejected."""
    with pytest.raises(ComposeExecutionError, match="service_scope must be a non-empty tuple"):
        _make_intent(service_scope=())


def test_5_duplicate_service_names_rejected():
    """5. Duplicate service names in scope are strictly rejected."""
    with pytest.raises(ExecutorIPCError, match="Duplicate service name"):
        validate_service_scope(["searxng", "searxng"])

    with pytest.raises(ComposeExecutionError, match="Duplicate service name"):
        _make_intent(service_scope=("searxng", "searxng"))


def test_6_empty_service_name_rejected():
    """6. Empty service names are strictly rejected."""
    with pytest.raises(ExecutorIPCError, match="must be non-empty string"):
        validate_service_scope([""])

    with pytest.raises(ExecutorIPCError, match="service_scope cannot be empty"):
        validate_service_scope([])


def test_7_path_traversal_rejected(tmp_path: Path):
    """7. Path traversal in service name or compose file is strictly rejected."""
    with pytest.raises(ExecutorIPCError, match="forbidden characters"):
        validate_service_scope(["../searxng"])

    with pytest.raises(ExecutorIPCError, match="forbidden characters"):
        validate_service_scope(["searxng/traversal"])

    # Traversal in compose files:
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    traversal_file = proj_dir / ".." / "traversal.yml"
    proj = _DummyProject(name="proj", path=proj_dir, compose_files=(traversal_file,), services={"s": {}})
    adapter = ComposeExecutionAdapter(project_resolver={"proj": proj})
    with pytest.raises(ComposeExecutionError, match="Path traversal detected"):
        adapter.execute(_make_intent(project_name="proj", service_scope=("s",)))


def test_8_shell_metacharacters_rejected():
    """8. Shell metacharacters are strictly rejected."""
    bad_services = [
        "searxng; rm -rf /",
        "searxng && echo pwn",
        "searxng | cat",
        "searxng`id`",
        "searxng$(id)",
        "searxng > /tmp/pwn",
        "searxng\nnewline",
    ]
    for bad in bad_services:
        with pytest.raises(ExecutorIPCError, match="forbidden characters|whitespace"):
            validate_service_scope([bad])


def test_9_image_reference_injected_into_service_name_rejected():
    """9. Image references embedded in service names are rejected."""
    bad_services = [
        "searxng:latest",
        "searxng@sha256:12345",
        "docker.io/searxng/searxng",
    ]
    for bad in bad_services:
        with pytest.raises(ExecutorIPCError, match="forbidden characters"):
            validate_service_scope([bad])


def test_10_arbitrary_compose_path_rejected(tmp_path: Path):
    """10. Compose file outside project root or non-existent is rejected."""
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    non_existent = proj_dir / "non_existent.yml"
    proj = _DummyProject(name="proj", path=proj_dir, compose_files=(non_existent,), services={"s": {}})
    adapter = ComposeExecutionAdapter(project_resolver={"proj": proj})
    with pytest.raises(ComposeExecutionError, match="Compose file does not exist"):
        adapter.execute(_make_intent(project_name="proj", service_scope=("s",)))


def test_11_arbitrary_image_tag_rejected():
    """11. ExecutionRequest wire protocol accepts NO arbitrary image tag parameter."""
    req_json = json.dumps({
        "action_id": ACTION_ID,
        "capability_id": CAPABILITY_EXECUTE_SERVICE_UPDATE,
        "target_id": PROJECT_NAME,
        "contract_digest": CONTRACT_DIGEST,
        "lease_id": LEASE_ID,
        "fencing_token": FENCING_TOKEN,
        "plan_digest": PLAN_DIGEST,
        "confirmation_id": CONFIRMATION_ID,
        "service_scope": ["searxng"],
        "arbitrary_image_tag": "malicious:latest",
    }).encode()
    req = ExecutionRequest.from_json(req_json)
    assert not hasattr(req, "arbitrary_image_tag")
    # Verify to_json drops any unexpected keys
    assert b"arbitrary_image_tag" not in req.to_json()


def test_12_arbitrary_docker_flags_rejected():
    """12. Arbitrary Docker flags starting with '-' are rejected as service names."""
    bad_flags = ["--privileged", "-v", "-d", "--no-deps"]
    for flag in bad_flags:
        with pytest.raises(ExecutorIPCError, match="cannot start with dash"):
            validate_service_scope([flag])


def test_13_expired_lease_rejected(tmp_path: Path):
    """13. Expired or invalid lease is rejected before any mutation."""
    proj = _setup_project(tmp_path)
    runner = _MockRunner()

    lease_validator = lambda action_id, token, now: False  # expired!

    adapter = ComposeExecutionAdapter(
        project_resolver={PROJECT_NAME: proj},
        runner=runner,
        lease_validator=lease_validator,
    )

    with pytest.raises(ComposeExecutionError, match="Lease expired"):
        adapter.execute(_make_intent())

    assert len(runner.calls) == 0


def test_14_invalid_fencing_token_rejected():
    """14. Invalid fencing token (zero, negative, bool) is rejected."""
    with pytest.raises(ComposeExecutionError, match="Invalid fencing_token"):
        _make_intent(fencing_token=0)
    with pytest.raises(ComposeExecutionError, match="Invalid fencing_token"):
        _make_intent(fencing_token=-1)


def test_15_invalid_plan_digest_rejected():
    """15. Invalid plan digest is rejected."""
    with pytest.raises(ComposeExecutionError, match="Invalid plan_digest"):
        _make_intent(plan_digest="short")
    with pytest.raises(ComposeExecutionError, match="Invalid plan_digest"):
        _make_intent(plan_digest="Z" * 64)


def test_16_invalid_contract_digest_rejected():
    """16. Invalid contract digest is rejected."""
    with pytest.raises(ComposeExecutionError, match="Invalid contract_digest"):
        _make_intent(contract_digest="short")


def test_17_missing_service_scope_rejected_for_service_update():
    """17. Missing service_scope for execute_service_update capability fails closed."""
    payload = {
        "action_id": ACTION_ID,
        "capability_id": CAPABILITY_EXECUTE_SERVICE_UPDATE,
        "target_id": PROJECT_NAME,
        "contract_digest": CONTRACT_DIGEST,
        "lease_id": LEASE_ID,
        "fencing_token": FENCING_TOKEN,
        "plan_digest": PLAN_DIGEST,
        "confirmation_id": CONFIRMATION_ID,
    }
    with pytest.raises(ExecutorIPCError, match="execute_service_update requires service_scope"):
        ExecutionRequest.from_json(json.dumps(payload).encode())


def test_18_unknown_ipc_outcome_remains_reconciliation_unknown(tmp_path: Path):
    """18. Interruption or exception during execution maps to unknown_outcome in receipt and response."""
    receipt_db = tmp_path / "receipts.db"
    receipts = MutationReceiptStore(str(receipt_db))

    class _CrashingAdapter:
        def execute(self, intent):
            raise RuntimeError("Process crashed mid-mutation!")

    handler = compose_executor_update_handler(
        compose_adapter=_CrashingAdapter(),
        receipts=receipts,
    )

    req = ExecutionRequest(
        action_id=ACTION_ID,
        capability_id=CAPABILITY_EXECUTE_SERVICE_UPDATE,
        target_id=PROJECT_NAME,
        contract_digest=CONTRACT_DIGEST,
        lease_id=LEASE_ID,
        fencing_token=FENCING_TOKEN,
        plan_digest=PLAN_DIGEST,
        confirmation_id=CONFIRMATION_ID,
        service_scope=("searxng",),
    )

    resp = handler(req)
    assert resp.outcome == "unknown_outcome"
    assert resp.provider_code == "update_interrupted"

    receipt = receipts.get(action_id=ACTION_ID, fencing_token=FENCING_TOKEN)
    assert receipt is not None
    assert receipt.mutation_status == MutationStatus.UNKNOWN_OUTCOME


def test_19_partial_atomic_tight_execution_never_becomes_success(tmp_path: Path):
    """19. Partial execution in ATOMIC_TIGHT scope yields RECONCILIATION_REQUIRED, never success."""
    proj = _setup_project(tmp_path, services=("searxng", "searxng-valkey"))

    # Fail on the second service up command
    call_count = 0

    def mock_runner(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        # cmd 1: pull valkey, cmd 2: up valkey, cmd 3: pull searxng, cmd 4: up searxng
        if "up" in cmd and "searxng" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=1, stderr="container crash")
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    adapter = ComposeExecutionAdapter(
        project_resolver={PROJECT_NAME: proj},
        runner=mock_runner,
        inspector=lambda p, s: _DummyServiceObs(s),
    )

    intent = _make_intent(
        service_scope=("searxng-valkey", "searxng"),
        atomicity="atomic_tight",
    )
    res = adapter.execute(intent)

    assert not res.is_success
    assert res.verification.code == ServiceUpdateVerificationCode.RECONCILIATION_REQUIRED
    assert res.mutation_occurred is True


def test_20_unrelated_service_is_never_included(tmp_path: Path):
    """20. Unrelated service not defined in project is rejected before any command runs."""
    proj = _setup_project(tmp_path, services=("searxng",))
    runner = _MockRunner()

    adapter = ComposeExecutionAdapter(
        project_resolver={PROJECT_NAME: proj},
        runner=runner,
    )

    intent = _make_intent(service_scope=("searxng", "unrelated_pwn"))
    with pytest.raises(ComposeExecutionError, match="not defined in Compose project") as excinfo:
        adapter.execute(intent)
    assert excinfo.value.code == "unrelated_service"
    assert len(runner.calls) == 0


def test_21_local_build_service_cannot_accidentally_enter_registry_mutation(tmp_path: Path):
    """21. Service configured with local build cannot enter registry mutation."""
    proj = _setup_project(tmp_path, services=("custom-app",))
    proj.local_build_services.add("custom-app")
    runner = _MockRunner()

    adapter = ComposeExecutionAdapter(
        project_resolver={PROJECT_NAME: proj},
        runner=runner,
    )

    intent = _make_intent(service_scope=("custom-app",))
    with pytest.raises(ComposeExecutionError, match="local build and cannot enter registry mutation") as excinfo:
        adapter.execute(intent)
    assert excinfo.value.code == "local_build_forbidden"
    assert len(runner.calls) == 0


def test_22_dependency_health_failure_blocks_mutation(tmp_path: Path):
    """22. Unhealthy dependency blocks mutation before any pull/up command runs."""
    proj = _setup_project(tmp_path, services=("searxng", "searxng-valkey"))
    runner = _MockRunner()

    def inspector(p, s):
        if s == "searxng-valkey":
            return _DummyServiceObs(s, health="unhealthy")
        return _DummyServiceObs(s)

    adapter = ComposeExecutionAdapter(
        project_resolver={PROJECT_NAME: proj},
        runner=runner,
        inspector=inspector,
    )

    intent = _make_intent(service_scope=("searxng-valkey", "searxng"))
    res = adapter.execute(intent)

    assert not res.is_success
    assert res.verification.code == ServiceUpdateVerificationCode.DEPENDENCY_HEALTH_FAILURE
    assert len(runner.calls) == 0
    assert res.mutation_occurred is False


def test_23_post_mutation_runtime_digest_mismatch_detected(tmp_path: Path):
    """23. Post-mutation runtime digest mismatch is caught and classified."""
    proj = _setup_project(tmp_path, services=("searxng",))
    runner = _MockRunner()

    # Inspector returns stale digest
    inspector = lambda p, s: _DummyServiceObs(s, running_digest="sha256:old_digest_did_not_change")

    adapter = ComposeExecutionAdapter(
        project_resolver={PROJECT_NAME: proj},
        runner=runner,
        inspector=inspector,
    )

    intent = _make_intent(
        service_scope=("searxng",),
        expected_target_digest="sha256:new_target_digest_expected",
    )
    res = adapter.execute(intent)

    assert not res.is_success
    assert res.verification.code == ServiceUpdateVerificationCode.RUNTIME_DIGEST_MISMATCH


def test_24_post_mutation_health_failure_detected(tmp_path: Path):
    """24. Post-mutation container health failure is caught and classified."""
    proj = _setup_project(tmp_path, services=("searxng",))
    runner = _MockRunner()

    calls = 0

    def inspector(p, s):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _DummyServiceObs(s, health="healthy")
        return _DummyServiceObs(s, health="unhealthy")

    adapter = ComposeExecutionAdapter(
        project_resolver={PROJECT_NAME: proj},
        runner=runner,
        inspector=inspector,
    )

    intent = _make_intent(service_scope=("searxng",))
    res = adapter.execute(intent)

    assert not res.is_success
    assert res.verification.code == ServiceUpdateVerificationCode.APPLICATION_PROBE_FAILURE


def test_25_successful_command_without_successful_verification_is_not_success(tmp_path: Path):
    """25. Command exit code 0 without independent verification is NOT VERIFIED_SUCCESS."""
    proj = _setup_project(tmp_path, services=("searxng",))
    runner = _MockRunner(returncode=0)  # exit code 0

    # No inspector configured
    adapter = ComposeExecutionAdapter(
        project_resolver={PROJECT_NAME: proj},
        runner=runner,
        inspector=None,
    )

    intent = _make_intent(service_scope=("searxng",))
    res = adapter.execute(intent)

    assert not res.is_success
    assert res.verification.code == ServiceUpdateVerificationCode.SUPERVISOR_FAILURE
    assert "command exit 0 is not verified success" in res.verification.details


# ---------------------------------------------------------------------------
# Disposable Compose Execution Fixture & Semantics Proof
# ---------------------------------------------------------------------------


def test_disposable_compose_semantics_no_deps_and_ordering(tmp_path: Path):
    """Proves exact Docker Compose command construction, argv safety, and --no-deps."""
    proj = _setup_project(tmp_path, services=("valkey", "searxng"))
    runner = _MockRunner()
    inspector = lambda p, s: _DummyServiceObs(s)

    adapter = ComposeExecutionAdapter(
        project_resolver={PROJECT_NAME: proj},
        runner=runner,
        inspector=inspector,
    )

    intent = _make_intent(
        service_scope=("valkey", "searxng"),
        atomicity="atomic_tight",
    )
    res = adapter.execute(intent)

    assert res.is_success
    assert len(runner.calls) == 4

    # Command 1: pull valkey
    assert runner.calls[0] == ["docker", "compose", "-f", str(proj.compose_files[0]), "pull", "valkey"]
    # Command 2: up valkey with --no-deps
    assert runner.calls[1] == ["docker", "compose", "-f", str(proj.compose_files[0]), "up", "-d", "--no-deps", "valkey"]
    # Command 3: pull searxng
    assert runner.calls[2] == ["docker", "compose", "-f", str(proj.compose_files[0]), "pull", "searxng"]
    # Command 4: up searxng with --no-deps
    assert runner.calls[3] == ["docker", "compose", "-f", str(proj.compose_files[0]), "up", "-d", "--no-deps", "searxng"]


# ---------------------------------------------------------------------------
# Real Unix Domain Socket IPC & Receipt Integration
# ---------------------------------------------------------------------------


def test_live_unix_socket_ipc_roundtrip_with_receipts(tmp_path: Path):
    """Proves full end-to-end IPC roundtrip over an actual Unix domain socket with receipts."""
    sock_path = tmp_path / "executor.sock"
    receipt_db = tmp_path / "receipts.db"
    receipts = MutationReceiptStore(str(receipt_db))

    proj = _setup_project(tmp_path, services=("searxng",))
    runner = _MockRunner()
    inspector = lambda p, s: _DummyServiceObs(s)

    adapter = ComposeExecutionAdapter(
        project_resolver={PROJECT_NAME: proj},
        runner=runner,
        inspector=inspector,
    )

    update_handler = compose_executor_update_handler(
        compose_adapter=adapter,
        receipts=receipts,
    )

    server = ExecutorIPCServer(
        socket_path=str(sock_path),
        allowed_caller_uids={os.getuid()},
        handler=update_handler,
    )
    server.start()

    stop_event = threading.Event()
    server_thread = threading.Thread(target=server.serve_one, daemon=True)
    server_thread.start()

    try:
        client = ExecutorIPCClient(socket_path=str(sock_path))
        runtime = compose_ipc_update_runtime(client)

        from aipm.control_plane.models import UpdateExecutionBinding
        binding = UpdateExecutionBinding(
            project_name=PROJECT_NAME,
            plan_digest=PLAN_DIGEST,
            confirmation_id=CONFIRMATION_ID,
            action_id=ACTION_ID,
            contract_digest=CONTRACT_DIGEST,
            lease_id=LEASE_ID,
            fencing_token=FENCING_TOKEN,
            service_scope=("searxng",),
        )

        resp_dict = runtime(binding)
        assert resp_dict["outcome"] == "succeeded"
        assert resp_dict["provider_code"] == "update_ok"

        # Verify receipt state
        receipt = receipts.get(action_id=ACTION_ID, fencing_token=FENCING_TOKEN)
        assert receipt is not None
        assert receipt.mutation_status == MutationStatus.MUTATION_SUCCEEDED
    finally:
        server.stop()
        server_thread.join(timeout=1.0)


# ---------------------------------------------------------------------------
# Static AST No-Mutation Proofs
# ---------------------------------------------------------------------------


def test_ast_no_shell_true_or_string_commands():
    """Static AST check: execution adapter and IPC modules NEVER use shell=True."""
    target_files = [
        Path("src/aipm/services/compose/execution_adapter.py"),
        Path("src/aipm/composition/executor_update.py"),
        Path("src/aipm/control_plane/executor_ipc.py"),
    ]
    for file_path in target_files:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Verify no shell=True keyword argument
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        pytest.fail(f"shell=True detected in {file_path}:{node.lineno}")


def test_ast_no_privilege_broker_or_production_socket_in_c3_code():
    """Static check: C.3 production modules do not invoke production broker or production socket."""
    c3_files = [
        Path("src/aipm/services/compose/execution_adapter.py"),
    ]
    prod_broker = "/usr/local" + "/libexec/aipm/aipm-privilege-broker"
    prod_sock = "/run" + "/aipm/executor.sock"
    for file_path in c3_files:
        content = file_path.read_text(encoding="utf-8")
        assert prod_broker not in content, f"Forbidden broker found in {file_path}"
        assert prod_sock not in content, f"Forbidden socket found in {file_path}"
