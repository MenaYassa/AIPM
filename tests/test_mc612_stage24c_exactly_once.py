"""Shot 24C: exactly-once certification for the mutation receipt claim model.

These tests prove the executor's concurrency guarantee at the executor
level (envelope in, provider calls out) with real threads and a real
SQLite database:

- Cases A-D: concurrent duplicate identities vs. distinct identities.
- Case E: duplicate request after completion is refused with the stored
  receipt status visible in the error message.
- Case F: a request arriving while another claim is in flight (claim
  issued, completion pending) must not invoke the provider.
- Stress: 100 consecutive 20-worker trials must show zero duplicate
  provider invocations and zero duplicate receipts.

Terminology (binding for docs and reports):
- "exactly-once durable claim": at most one receipt per (action_id,
  fencing_token) — enforced by UNIQUE + BEGIN IMMEDIATE.
- "single provider invocation under concurrent duplicate requests":
  the claim gates the provider.
- NOT proven (and not claimed): exactly-once EXTERNAL SIDE EFFECT. If
  the process dies between claim and provider completion, the side
  effect may or may not have happened; the receipt stays RECEIPT_CREATED
  (attempted/unknown) and is never auto-retried.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.control_plane.mutation_receipt import (
    MutationReceiptError, MutationReceiptStore, MutationStatus,
)
from aipm.control_plane.standalone_executor import (
    ExecutionEnvelope, StandaloneSystemdExecutor,
)
from aipm.control_plane.systemd_provider import (
    SystemdRestartError, SystemdRestartPolicy, SystemdRestartProvider,
    SubprocessResult,
)

NOW = datetime.now(timezone.utc)  # fresh: envelopes must not be expired
POLICY = SystemdRestartPolicy(
    environment="staging", target_id="aipm-telemetry", unit_id="aipm-telemetry",
    canonical_unit_name="aipm-telemetry.service", policy_version="policy-v1",
)


class CountingRunner:
    """Thread-safe fake systemctl: records argv, returns canned results.

    Optionally gates the restart invocation: when `restart_gate` is set,
    a worker calling restart blocks on it. This creates a durable
    RECEIPT_CREATED window while the provider is "in flight".
    """

    def __init__(self, *, returncode: int = 0, fail_show: bool = False,
                 restart_gate: threading.Event | None = None) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.fail_show = fail_show
        self.restart_gate = restart_gate
        self._lock = threading.Lock()

    def __call__(self, argv: list[str], *, timeout: int = 30) -> SubprocessResult:
        with self._lock:
            self.calls.append(list(argv))
        if argv[1] == "show":
            if self.fail_show:
                return SubprocessResult(1, "", "show failed", False)
            return SubprocessResult(
                0,
                "LoadState=loaded\nActiveState=active\nSubState=running\n"
                "UnitFileState=enabled\nMainPID=1234\nFragmentPath=/x\n",
                "",
                False,
            )
        if argv[1] == "restart" and self.restart_gate is not None:
            self.restart_gate.wait(timeout=10)
        return SubprocessResult(self.returncode, "", "", False)

    @property
    def restart_calls(self) -> int:
        with self._lock:
            return sum(1 for argv in self.calls if argv[1] == "restart")


def make_envelope(**overrides) -> ExecutionEnvelope:
    values = {
        "protocol_version": "mc612-execution-envelope-v1",
        "action_id": "a" * 64,
        "action_version": 1,
        "capability_id": "apply_project_plan",
        "capability_version": "1",
        "target_id": "aipm-telemetry",
        "environment": "staging",
        "unit_name": "aipm-telemetry.service",
        "contract_digest": "d" * 64,
        "fencing_token": 1,
        "lease_id": "l" * 32,
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }
    values.update(overrides)
    return ExecutionEnvelope(**values)


def make_executor(tmp_path: Path, *, returncode: int = 0, fail_show: bool = False,
                  restart_gate: threading.Event | None = None):
    runner = CountingRunner(returncode=returncode, fail_show=fail_show, restart_gate=restart_gate)
    provider = SystemdRestartProvider(policies=[POLICY], runner=runner)
    receipts = MutationReceiptStore(str(Path(tmp_path) / "receipts.db"))
    executor = StandaloneSystemdExecutor(provider=provider, policy=POLICY, receipts=receipts)
    return executor, runner, receipts


def _run_concurrent_workers(n_workers: int, fn) -> tuple[list, list[BaseException]]:
    """Run fn(i) on n_workers threads released by one barrier."""
    results: list = []
    errors: list[BaseException] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(n_workers)

    def worker(i: int) -> None:
        try:
            barrier.wait()
            outcome = fn(i)
            with results_lock:
                results.append(outcome)
        except BaseException as exc:  # noqa: BLE001 - test boundary
            with results_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), "worker thread timed out"
    return results, errors


# --- Case A: 20 concurrent duplicate requests -> exactly one provider call ---


def test_case_a_20_workers_single_identity(tmp_path: Path):
    executor, runner, receipts = make_executor(tmp_path)
    envelope = make_envelope()

    results, errors = _run_concurrent_workers(
        20, lambda _i: executor.execute_restart(envelope)
    )

    assert runner.restart_calls == 1
    assert receipts.count() == 1
    successes = [r for r in results if getattr(r, "outcome", None) == "succeeded"]
    assert len(successes) == 1
    loser_errors = [e for e in errors if isinstance(e, MutationReceiptError)]
    assert len(loser_errors) == len(errors)
    assert len(results) + len(errors) == 20
    # Every loser must see "already claimed" (stage18 contract wording)
    for e in loser_errors:
        assert "already claimed" in str(e)
    # Exactly one receipt in terminal state
    receipt = receipts.get(action_id=envelope.action_id, fencing_token=envelope.fencing_token)
    assert receipt is not None
    assert receipt.mutation_status == MutationStatus.MUTATION_SUCCEEDED


# --- Case B: 50 workers, same assertion shape ---


def test_case_b_50_workers_single_identity(tmp_path: Path):
    executor, runner, receipts = make_executor(tmp_path)
    envelope = make_envelope()

    results, errors = _run_concurrent_workers(
        50, lambda _i: executor.execute_restart(envelope)
    )

    assert runner.restart_calls == 1
    assert receipts.count() == 1
    assert len(results) + len(errors) == 50
    assert all(isinstance(e, MutationReceiptError) for e in errors)
    assert any(getattr(r, "outcome", None) == "succeeded" for r in results)


# --- Case C: 100 workers ---


def test_case_c_100_workers_single_identity(tmp_path: Path):
    executor, runner, receipts = make_executor(tmp_path)
    envelope = make_envelope()

    results, errors = _run_concurrent_workers(
        100, lambda _i: executor.execute_restart(envelope)
    )

    assert runner.restart_calls == 1
    assert receipts.count() == 1
    assert len(results) + len(errors) == 100
    assert all(isinstance(e, MutationReceiptError) for e in errors)


# --- Case D: distinct identities -> every worker gets its own claim ---


def test_case_d_distinct_identities_all_succeed(tmp_path: Path):
    executor, runner, receipts = make_executor(tmp_path)

    def run(i: int):
        envelope = make_envelope(action_id=f"{i:064d}", fencing_token=i + 1)
        return executor.execute_restart(envelope)

    results, errors = _run_concurrent_workers(20, run)

    assert errors == []
    assert len(results) == 20
    assert runner.restart_calls == 20
    assert receipts.count() == 20
    assert all(r.outcome == "succeeded" for r in results)


# --- Case E: duplicate after completion is refused, provider not re-invoked ---


def test_case_e_duplicate_after_completion(tmp_path: Path):
    executor, runner, receipts = make_executor(tmp_path)
    envelope = make_envelope()

    first = executor.execute_restart(envelope)
    assert first.outcome == "succeeded"
    assert runner.restart_calls == 1
    completed = receipts.get(action_id=envelope.action_id, fencing_token=envelope.fencing_token)
    assert completed.mutation_status == MutationStatus.MUTATION_SUCCEEDED

    with pytest.raises(MutationReceiptError, match="already claimed") as exc_info:
        executor.execute_restart(envelope)

    # Stored status must be visible in the error
    assert "mutation_succeeded" in str(exc_info.value)
    assert runner.restart_calls == 1, "provider must not be re-invoked"


# --- Case F: duplicate arriving while a claim is in flight (completion pending) ---


def test_case_f_duplicate_during_inflight_claim(tmp_path: Path):
    """A duplicate must be refused by the durable fence while the first
    request holds RECEIPT_CREATED mid-flight (provider gated, not yet
    completed). This is the claim-gates-provider proof.
    """
    restart_gate = threading.Event()
    restart_entered = threading.Event()

    class GatedProvider(SystemdRestartProvider):
        def restart(self, policy, *, now=None):
            restart_entered.set()
            restart_gate.wait(timeout=10)
            return super().restart(policy, now=now)

    runner = CountingRunner()
    provider = GatedProvider(policies=[POLICY], runner=runner)
    receipts = MutationReceiptStore(str(Path(tmp_path) / "receipts.db"))
    executor = StandaloneSystemdExecutor(provider=provider, policy=POLICY, receipts=receipts)
    envelope = make_envelope()

    first_result: list = []

    def first_worker():
        first_result.append(executor.execute_restart(envelope))

    t1 = threading.Thread(target=first_worker)
    t1.start()
    assert restart_entered.wait(timeout=10), "first worker never reached the provider"

    # First worker holds a RECEIPT_CREATED receipt and is inside restart().
    receipt = receipts.get(action_id=envelope.action_id, fencing_token=envelope.fencing_token)
    assert receipt is not None
    assert receipt.mutation_status == MutationStatus.RECEIPT_CREATED

    # Duplicate arriving NOW must be refused by the fence, not queued.
    duplicate_error: list = []

    def duplicate_worker():
        try:
            executor.execute_restart(envelope)
        except MutationReceiptError as exc:
            duplicate_error.append(exc)

    t2 = threading.Thread(target=duplicate_worker)
    t2.start()
    t2.join(timeout=10)
    assert not t2.is_alive()
    assert len(duplicate_error) == 1, "duplicate must raise MutationReceiptError"
    assert "already claimed" in str(duplicate_error[0])
    assert "receipt_created" in str(duplicate_error[0])

    # Release the first worker; it completes normally.
    restart_gate.set()
    t1.join(timeout=10)
    assert not t1.is_alive()
    assert len(first_result) == 1 and first_result[0].outcome == "succeeded"

    # Provider invoked exactly once for the identity.
    assert runner.restart_calls == 1
    assert receipts.count() == 1
    final = receipts.get(action_id=envelope.action_id, fencing_token=envelope.fencing_token)
    assert final.mutation_status == MutationStatus.MUTATION_SUCCEEDED


# --- Pre-provider failure: receipt must NOT stay RECEIPT_CREATED forever ---


def test_pre_provider_failure_records_mutation_failed(tmp_path: Path):
    """If observe fails AFTER claim (pre-provider), the receipt is completed
    as MUTATION_FAILED with provider_code=executor_error:pre_provider."""
    executor, runner, receipts = make_executor(tmp_path, fail_show=True)
    envelope = make_envelope()

    with pytest.raises(SystemdRestartError):
        executor.execute_restart(envelope)

    assert runner.restart_calls == 0, "provider must never be reached"
    receipt = receipts.get(action_id=envelope.action_id, fencing_token=envelope.fencing_token)
    assert receipt is not None, "claim must have been created before the failure"
    assert receipt.mutation_status == MutationStatus.MUTATION_FAILED
    assert receipt.provider_code == "executor_error:pre_provider"


# --- Committed stress: 100 consecutive 20-worker trials ---


def test_stress_100_trials_20_workers(tmp_path: Path):
    for trial in range(100):
        trial_dir = Path(tmp_path) / f"trial_{trial:03d}"
        trial_dir.mkdir()
        executor, runner, receipts = make_executor(trial_dir)
        envelope = make_envelope()

        results, errors = _run_concurrent_workers(
            20, lambda _i: executor.execute_restart(envelope)
        )

        assert runner.restart_calls == 1, f"trial {trial}: duplicate provider invocation"
        assert receipts.count() == 1, f"trial {trial}: duplicate receipts"
        assert len(results) + len(errors) == 20
        assert all(isinstance(e, MutationReceiptError) for e in errors)
