from __future__ import annotations

from dataclasses import replace

from ksadk.harness.memory_consolidation import MemoryConsolidationQueue
from ksadk.memory.models import MemoryRecord
from ksadk.memory.providers.local_sqlite import SqliteMemoryProvider


def _record(memory_id: str, *, version: int, slot: str = "report:language") -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        tenant_id="local",
        workspace_id="local",
        scope="user",
        scope_id="u1",
        memory_type="profile",
        content=f"report language version {version}",
        summary=f"version {version}",
        status="active",
        confidence=1.0,
        importance=0.8,
        valid_from="",
        valid_to="",
        expires_at="",
        source_session_id="",
        source_event_ids=[],
        source_seq_range=None,
        content_hash=f"hash-{version}",
        version=version,
        metadata={"slot_key": slot},
    )


def test_background_consolidation_keeps_newest_slot_fact():
    provider = SqliteMemoryProvider()
    first = provider.upsert(_record("m1", version=1), expected_version=None)
    second = provider.upsert(_record("m2", version=2), expected_version=None)
    # Ensure deterministic timestamp ordering is not required; version wins.
    provider.upsert(replace(second, updated_at=first.updated_at), expected_version=second.version)
    queue = MemoryConsolidationQueue(provider)

    task = queue.submit(scope="user", scope_id="u1")
    duplicate = queue.submit(scope="user", scope_id="u1")
    completed = queue.run_pending(limit=1)[0]

    assert duplicate.task_id == task.task_id
    assert completed.status == "succeeded"
    assert completed.scanned == 2
    assert completed.superseded == 1
    assert provider.get("m1").status == "superseded"
    assert provider.get("m2").status == "active"


class _BrokenProvider:
    def search(self, request):
        raise TimeoutError("memory service unavailable")


def test_worker_failure_is_recorded_without_escaping():
    queue = MemoryConsolidationQueue(_BrokenProvider())
    task = queue.submit(scope="user", scope_id="u1")

    completed = queue.run_pending(limit=1)[0]

    assert completed.task_id == task.task_id
    assert completed.status == "failed"
    assert completed.error_code == "provider_error"
