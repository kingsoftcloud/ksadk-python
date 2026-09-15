from concurrent.futures import ThreadPoolExecutor

import pytest

from ksadk.resource_runtime.operation_ledger import OperationConflict, OperationLedger


def claim(ledger, **changes):
    values = dict(
        authority_digest="tenant-user-agent-binding",
        event_id="stable-turn-event",
        operation="save_memory",
        arguments={"content": "private preference"},
    )
    values.update(changes)
    return ledger.claim(**values)


def test_restart_never_replays_unknown_write(tmp_path):
    first = claim(OperationLedger(tmp_path / "ledger"))
    recovered = claim(OperationLedger(tmp_path / "ledger"))
    assert first.claimed
    assert not recovered.claimed
    assert recovered.operation_id == first.operation_id
    assert recovered.state == "unknown"
    assert (
        b"private preference" not in (tmp_path / "ledger/resource-operations.sqlite3").read_bytes()
    )


def test_concurrent_hosts_claim_once(tmp_path):
    ledgers = [OperationLedger(tmp_path / "ledger") for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        receipts = list(executor.map(claim, ledgers))
    assert sum(receipt.claimed for receipt in receipts) == 1
    assert len({receipt.operation_id for receipt in receipts}) == 1


def test_changed_arguments_rejected_without_new_dispatch(tmp_path):
    ledger = OperationLedger(tmp_path / "ledger")
    claim(ledger)
    with pytest.raises(OperationConflict, match="ARGUMENTS_CHANGED"):
        claim(ledger, arguments={"content": "different"})


def test_authorities_and_events_are_separate(tmp_path):
    ledger = OperationLedger(tmp_path / "ledger")
    receipts = [
        claim(ledger),
        claim(ledger, authority_digest="another-user"),
        claim(ledger, event_id="another-event"),
    ]
    assert all(receipt.claimed for receipt in receipts)
    assert len({receipt.operation_id for receipt in receipts}) == 3


def test_pending_survives_restart_and_terminal_state_cannot_regress(tmp_path):
    ledger = OperationLedger(tmp_path / "ledger")
    receipt = claim(ledger)
    ledger.record(receipt.operation_id, "accepted_pending")
    ledger = OperationLedger(tmp_path / "ledger")
    assert claim(ledger).state == "accepted_pending"
    ledger.record(receipt.operation_id, "searchable")
    with pytest.raises(OperationConflict, match="STATE_CONFLICT"):
        ledger.record(receipt.operation_id, "failed")
    assert claim(ledger).state == "searchable"
    assert not claim(ledger).claimed


def test_public_directory_and_symlink_rejected(tmp_path):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        OperationLedger(public)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    (private / "resource-operations.sqlite3").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="symbolic"):
        OperationLedger(private)


def test_receipt_lookup_requires_owner_and_operation(tmp_path):
    ledger = OperationLedger(tmp_path / "ledger")
    receipt = claim(ledger)
    assert (
        ledger.lookup(
            receipt.operation_id,
            authority_digest="tenant-user-agent-binding",
            operation="save_memory",
        ).state
        == "unknown"
    )
    assert (
        ledger.lookup(
            receipt.operation_id, authority_digest="different-user", operation="save_memory"
        )
        is None
    )
    assert (
        ledger.lookup(
            receipt.operation_id,
            authority_digest="tenant-user-agent-binding",
            operation="execute_skills",
        )
        is None
    )
