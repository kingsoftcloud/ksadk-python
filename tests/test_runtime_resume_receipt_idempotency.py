from __future__ import annotations

from ksadk.conversations.runtime_resume import _tool_receipt_idempotency_key_for_resume


def _resume_input(checkpoint_id: str) -> dict[str, object]:
    return {
        "run_id": "run-1",
        "checkpoint_id": checkpoint_id,
        "tool_name": "write_workspace_file",
        "tool_args": {"path": "result.txt", "content": "done"},
    }


def test_receipt_key_separates_checkpoints_for_same_run_and_tool() -> None:
    first = _tool_receipt_idempotency_key_for_resume(
        session_id="session-1", resume_input=_resume_input("checkpoint-1")
    )
    second = _tool_receipt_idempotency_key_for_resume(
        session_id="session-1", resume_input=_resume_input("checkpoint-2")
    )

    assert first is not None
    assert second is not None
    assert first != second


def test_receipt_key_replays_idempotently_for_same_checkpoint() -> None:
    first = _tool_receipt_idempotency_key_for_resume(
        session_id="session-1", resume_input=_resume_input("checkpoint-1")
    )
    retry = _tool_receipt_idempotency_key_for_resume(
        session_id="session-1", resume_input={**_resume_input("checkpoint-1")}
    )

    assert retry == first
