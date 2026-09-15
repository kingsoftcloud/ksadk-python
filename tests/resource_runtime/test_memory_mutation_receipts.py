import sqlite3

import pytest

from ksadk.resource_runtime.memory_receipts import MemoryMutationReceipt
from ksadk.resource_runtime.operation_ledger import OperationConflict, OperationLedger


def setup(tmp_path):
    ledger = OperationLedger(tmp_path / "ledger")
    claim = ledger.claim(
        authority_digest="authority",
        event_id="event",
        operation="update_memory",
        arguments={"memoryId": "old-id", "content": "private memory body"},
    )
    result = MemoryMutationReceipt(
        operation_id=claim.operation_id,
        memory_id="old-id",
        new_memory_id="new-id",
        status="succeeded",
        error_code=None,
    )
    return ledger, result


def test_result_and_state_survive_reopen_without_body(tmp_path):
    ledger, result = setup(tmp_path)
    ledger.record_memory_mutation(result, authority_digest="authority", operation="update_memory")
    reopened = OperationLedger(ledger.path.parent)
    assert reopened.memory_mutation_result(
        result.operation_id, authority_digest="authority", operation="update_memory"
    ) == result.model_dump(by_alias=True)
    assert b"private memory body" not in ledger.path.read_bytes()
    for authority, operation in [("other", "update_memory"), ("authority", "delete_memory")]:
        assert (
            reopened.memory_mutation_result(
                result.operation_id, authority_digest=authority, operation=operation
            )
            is None
        )
        with pytest.raises(OperationConflict):
            reopened.record_memory_mutation(result, authority_digest=authority, operation=operation)


def test_failure_during_terminal_commit_rolls_back_result(tmp_path):
    ledger, result = setup(tmp_path)
    with ledger._connect() as connection:
        connection.execute(
            "CREATE TRIGGER fail_terminal BEFORE UPDATE ON operations "
            "BEGIN SELECT RAISE(ABORT, 'fixture'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        ledger.record_memory_mutation(
            result, authority_digest="authority", operation="update_memory"
        )
    assert (
        ledger.lookup(
            result.operation_id, authority_digest="authority", operation="update_memory"
        ).state
        == "unknown"
    )
    assert (
        ledger.memory_mutation_result(
            result.operation_id, authority_digest="authority", operation="update_memory"
        )
        is None
    )


def test_terminal_result_cannot_be_replaced(tmp_path):
    ledger, result = setup(tmp_path)
    ledger.record_memory_mutation(result, authority_digest="authority", operation="update_memory")
    with pytest.raises(OperationConflict, match="RESULT_CONFLICT"):
        ledger.record_memory_mutation(
            result.model_copy(update={"new_memory_id": "different"}),
            authority_digest="authority",
            operation="update_memory",
        )
    assert (
        ledger.memory_mutation_result(
            result.operation_id, authority_digest="authority", operation="update_memory"
        )["newMemoryId"]
        == "new-id"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"status": "unknown", "error_code": "MEMORY_MUTATION_UNKNOWN"},
        {"status": "failed", "error_code": "MEMORY_RECORD_NOT_FOUND"},
        {"error_code": "private server details"},
        {"content": "must not persist"},
    ],
)
def test_receipt_rejects_fabricated_ids_errors_and_bodies(tmp_path, change):
    _, result = setup(tmp_path)
    with pytest.raises(ValueError):
        MemoryMutationReceipt.model_validate({**result.model_dump(), **change})
