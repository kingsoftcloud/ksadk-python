from __future__ import annotations

import sqlite3

import pytest

from ksadk.a2a.resume_store import A2AResumeState, SQLiteA2AResumeStateStore
from ksadk.runtime import ResumeTarget, RunHandle


@pytest.mark.asyncio
async def test_sqlite_resume_store_survives_reopen_without_storing_raw_key(tmp_path) -> None:
    path = tmp_path / "resume.sqlite3"
    state = A2AResumeState(
        handle=RunHandle(
            run_id="run-1",
            session_id="internal-session-1",
            runtime_type="test",
            native_ref={"checkpoint_id": "checkpoint-1"},
        ),
        target=ResumeTarget(kind="checkpoint_id", id="checkpoint-1"),
        payload_kind="approval_decision",
        call_id="approval-1",
    )
    key = "verified-owner\x1finternal-session-1\x1fprotocol-task-1"

    first = SQLiteA2AResumeStateStore(path)
    await first.put(key, state)

    second = SQLiteA2AResumeStateStore(path)
    assert await second.get(key) == state

    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT resume_key_sha256, state_json FROM ksadk_a2a_resume_states"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert key not in row[0]
    assert key not in row[1]

    await second.delete(key)
    assert await second.get(key) is None
