"""Tests for the durable mutation receipt (Shot 16 executor fence)."""
from __future__ import annotations

import sqlite3
import tempfile
import threading
from pathlib import Path

import pytest

from aipm.control_plane.mutation_receipt import (
    MutationReceipt,
    MutationReceiptError,
    MutationReceiptStore,
    MutationStatus,
)


@pytest.fixture
def store(tmp_path: Path) -> MutationReceiptStore:
    return MutationReceiptStore(str(tmp_path / "receipts.db"))


ACTION = "a" * 64
FENCE = 7
CAP = "apply_project_plan"
TARGET = "project-demo"
DIGEST = "d" * 64


def test_claim_creates_receipt_with_created_status(store: MutationReceiptStore):
    receipt = store.claim(action_id=ACTION, fencing_token=FENCE, capability_id=CAP, target_id=TARGET, contract_digest=DIGEST)
    assert receipt.mutation_status is MutationStatus.RECEIPT_CREATED
    assert receipt.action_id == ACTION
    assert receipt.fencing_token == FENCE
    loaded = store.get(action_id=ACTION, fencing_token=FENCE)
    assert loaded is not None
    assert loaded.mutation_status is MutationStatus.RECEIPT_CREATED


def test_claim_duplicate_action_fence_is_rejected(store: MutationReceiptStore):
    store.claim(action_id=ACTION, fencing_token=FENCE, capability_id=CAP, target_id=TARGET, contract_digest=DIGEST)
    with pytest.raises(MutationReceiptError, match="already claimed"):
        store.claim(action_id=ACTION, fencing_token=FENCE, capability_id=CAP, target_id=TARGET, contract_digest=DIGEST)


def test_claim_different_fence_same_action_is_allowed(store: MutationReceiptStore):
    store.claim(action_id=ACTION, fencing_token=7, capability_id=CAP, target_id=TARGET, contract_digest=DIGEST)
    receipt = store.claim(action_id=ACTION, fencing_token=8, capability_id=CAP, target_id=TARGET, contract_digest=DIGEST)
    assert receipt.fencing_token == 8


def test_complete_transitions_to_succeeded(store: MutationReceiptStore):
    store.claim(action_id=ACTION, fencing_token=FENCE, capability_id=CAP, target_id=TARGET, contract_digest=DIGEST)
    completed = store.complete(action_id=ACTION, fencing_token=FENCE, status=MutationStatus.MUTATION_SUCCEEDED, provider_code="restart_ok")
    assert completed.mutation_status is MutationStatus.MUTATION_SUCCEEDED
    loaded = store.get(action_id=ACTION, fencing_token=FENCE)
    assert loaded.mutation_status is MutationStatus.MUTATION_SUCCEEDED


def test_complete_transitions_to_failed(store: MutationReceiptStore):
    store.claim(action_id=ACTION, fencing_token=FENCE, capability_id=CAP, target_id=TARGET, contract_digest=DIGEST)
    store.complete(action_id=ACTION, fencing_token=FENCE, status=MutationStatus.MUTATION_FAILED, provider_code="exit_1")
    loaded = store.get(action_id=ACTION, fencing_token=FENCE)
    assert loaded.mutation_status is MutationStatus.MUTATION_FAILED


def test_complete_transitions_to_unknown(store: MutationReceiptStore):
    store.claim(action_id=ACTION, fencing_token=FENCE, capability_id=CAP, target_id=TARGET, contract_digest=DIGEST)
    store.complete(action_id=ACTION, fencing_token=FENCE, status=MutationStatus.UNKNOWN_OUTCOME, provider_code="timeout")
    loaded = store.get(action_id=ACTION, fencing_token=FENCE)
    assert loaded.mutation_status is MutationStatus.UNKNOWN_OUTCOME


def test_complete_rejects_if_not_claimed(store: MutationReceiptStore):
    with pytest.raises(MutationReceiptError, match="not found"):
        store.complete(action_id=ACTION, fencing_token=FENCE, status=MutationStatus.MUTATION_SUCCEEDED, provider_code="ok")


def test_complete_cannot_change_completed_receipt(store: MutationReceiptStore):
    store.claim(action_id=ACTION, fencing_token=FENCE, capability_id=CAP, target_id=TARGET, contract_digest=DIGEST)
    store.complete(action_id=ACTION, fencing_token=FENCE, status=MutationStatus.MUTATION_SUCCEEDED, provider_code="ok")
    with pytest.raises(MutationReceiptError, match="not found"):
        store.complete(action_id=ACTION, fencing_token=FENCE, status=MutationStatus.MUTATION_FAILED, provider_code="retry")


def test_concurrent_claim_single_winner(tmp_path: Path):
    import threading
    store = MutationReceiptStore(str(tmp_path / "receipts.db"))
    results = []
    errors = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        try:
            receipt = store.claim(action_id=ACTION, fencing_token=FENCE, capability_id=CAP, target_id=TARGET, contract_digest=DIGEST)
            results.append(receipt)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()
    # Exactly one winner; the loser gets a MutationReceiptError
    assert len(results) == 1
    assert len(errors) == 1
    assert "already claimed" in str(errors[0])


def test_receipt_survives_store_recreation(tmp_path: Path):
    db_path = str(tmp_path / "receipts.db")
    store1 = MutationReceiptStore(db_path)
    store1.claim(action_id=ACTION, fencing_token=FENCE, capability_id=CAP, target_id=TARGET, contract_digest=DIGEST)
    store2 = MutationReceiptStore(db_path)
    loaded = store2.get(action_id=ACTION, fencing_token=FENCE)
    assert loaded is not None
    assert loaded.mutation_status is MutationStatus.RECEIPT_CREATED


def test_unknown_outcome_never_reset_to_not_started(tmp_path: Path):
    store = MutationReceiptStore(str(tmp_path / "receipts.db"))
    store.claim(action_id=ACTION, fencing_token=FENCE, capability_id=CAP, target_id=TARGET, contract_digest=DIGEST)
    store.complete(action_id=ACTION, fencing_token=FENCE, status=MutationStatus.UNKNOWN_OUTCOME, provider_code="timeout")
    # Attempting to "complete" again to reset to a non-unknown state must fail
    # because the receipt is no longer in RECEIPT_CREATED state.
    with pytest.raises(MutationReceiptError):
        store.complete(action_id=ACTION, fencing_token=FENCE, status=MutationStatus.MUTATION_SUCCEEDED, provider_code="retry")


def test_invalid_inputs_are_rejected(store: MutationReceiptStore):
    with pytest.raises(MutationReceiptError):
        store.claim(action_id="", fencing_token=FENCE, capability_id=CAP, target_id=TARGET, contract_digest=DIGEST)
    with pytest.raises(MutationReceiptError):
        store.claim(action_id=ACTION, fencing_token=0, capability_id=CAP, target_id=TARGET, contract_digest=DIGEST)
    with pytest.raises(MutationReceiptError):
        store.claim(action_id=ACTION, fencing_token=-1, capability_id=CAP, target_id=TARGET, contract_digest=DIGEST)
