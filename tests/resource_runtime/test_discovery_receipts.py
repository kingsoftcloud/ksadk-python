import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ksadk.resource_runtime.discovery_receipts import (
    DiscoverySelectionReceipts,
    DiscoverySkillReceipt,
    discovery_scope_key,
)
from ksadk.resource_runtime.operation_ledger import OperationConflict, OperationLedger
from ksadk.resource_runtime.supervisor import ResourceSupervisor
from tests.resource_runtime.test_broker import scope
from tests.resource_runtime.test_discovery_worker import discovery_initialization
from tests.resource_runtime.test_discovery_worker import skill_upstream as skill_upstream
from tests.resource_runtime.test_skill_packages import archive, ref
from tests.resource_runtime.test_worker_process import freeze_initialization


def test_receipts_are_immutable_scoped_and_bounded(tmp_path):
    ledger = OperationLedger(tmp_path / "ledger")
    receipts = DiscoverySelectionReceipts(ledger)
    key = discovery_scope_key(scope(), "run-a")
    first = DiscoverySkillReceipt.from_ref(ref(archive()))
    second = first.model_copy(update={"version_id": "other-version"})

    def claim(item):
        try:
            receipts.record(key, item)
            return item
        except OperationConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, [first, second]))
    winner = next(item for item in outcomes if item is not None)
    assert len([item for item in outcomes if item is not None]) == 1
    reopened = DiscoverySelectionReceipts(OperationLedger(tmp_path / "ledger"))
    assert reopened.read(key) == (winner,)
    assert reopened.read(discovery_scope_key(scope(), "run-b")) == ()
    other = scope().model_copy(update={"identity": scope().identity.model_copy(
        update={"memory_subject_ref": "other-user"},
    )})
    assert reopened.read(discovery_scope_key(other, "run-a")) == ()
    for index in range(31):
        receipts.record(key, first.model_copy(update={"skill_id": f"skill-{index}"}))
    with pytest.raises(OperationConflict):
        receipts.record(key, first.model_copy(update={"skill_id": "overflow"}))
    assert len(reopened.read(key)) == 32
    assert "archive_uri" not in first.model_dump_json()


async def test_discovery_restart_uses_committed_selection_before_returning_content(
    tmp_path, skill_upstream, monkeypatch,
):
    endpoint, state = skill_upstream
    init = discovery_initialization(endpoint, tmp_path)
    ledger = OperationLedger(tmp_path / "ledger")
    supervisor = ResourceSupervisor("generation-a", operation_ledger=ledger)
    key = discovery_scope_key(init.scopes[0], "run-a")
    store = DiscoverySelectionReceipts(ledger)

    async def load(active):
        return await supervisor._broker.dispatch({
            "v": 1, "requestId": "read", "handle": active.leases[0].handle,
            "deadline": int(time.time() * 1000) + 10000,
            "operation": "load_skill", "arguments": {"skillId": "skill-a"},
        })

    try:
        active = await supervisor.activate(init)
        target = supervisor._broker._discovery_receipts
        record = target.record

        def fail_commit(*args):
            raise OSError("fixture disk failure")

        monkeypatch.setattr(target, "record", fail_commit)
        failed = await load(active)
        assert failed["error"]["code"] == "RESOURCE_WORKER_FAILED"
        assert "original" not in str(failed) and store.read(key) == ()
        monkeypatch.setattr(target, "record", record)
        loaded = await load(active)
        assert loaded["result"]["content"] == "original"
        assert "_discoverySelection" not in str(loaded)
        assert store.read(key)[0].version_id == "version-a"
        count = len(state["calls"])
        await supervisor.aclose()

        # A new generation/activation resumes the same logical run after the
        # remote catalog and package have changed.
        state.update(content=archive({"SKILL.md": "upgraded"}), version_id="version-b")
        payload = init.pipe_payload()
        payload["scopes"][0].update(generationId="generation-b", activationId="activation-b")
        payload["bindings"][0]["restoredSelections"] = [
            DiscoverySkillReceipt.from_ref(ref(state["content"])).model_dump(by_alias=True)
        ]
        supervisor = ResourceSupervisor(
            "generation-b", operation_ledger=OperationLedger(tmp_path / "ledger"),
        )
        active = await supervisor.activate(freeze_initialization(payload))
        restored = await load(active)
        assert restored["result"]["content"] == "original"
        assert restored["result"]["versionId"] == "version-a"
        assert len(state["calls"]) == count
        assert len(store.read(key)) == 1
        # Corrupted selected bytes cannot be replaced with the live version.
        for path in (tmp_path / "cache").glob("*/extracted/SKILL.md"):
            path.write_text("tampered")
        damaged = await load(active)
        assert damaged["result"]["status"] == "failed"
        assert len(state["calls"]) == count
        await supervisor.deactivate(active.activation_id)
        payload["scopes"][0]["activationId"] = "new-run-activation"
        payload["bindings"][0]["selectionRunRef"] = "run-b"
        next_run = await supervisor.activate(freeze_initialization(payload))
        fresh = await load(next_run)
        assert fresh["result"]["content"] == "upgraded"
        assert fresh["result"]["versionId"] == "version-b"
    finally:
        await supervisor.aclose()


async def test_discovery_supervisor_requires_durable_receipts(tmp_path):
    supervisor = ResourceSupervisor("generation-a")
    try:
        with pytest.raises(ValueError, match="RESOURCE_DISCOVERY_RECEIPTS_REQUIRED"):
            await supervisor.activate(
                discovery_initialization("https://skills.example.test", tmp_path)
            )
    finally:
        await supervisor.aclose()


def test_failed_sql_commit_leaves_no_selection(tmp_path):
    import sqlite3

    ledger = OperationLedger(tmp_path / "ledger")
    store = DiscoverySelectionReceipts(ledger)
    key = discovery_scope_key(scope(), "run-a")
    with ledger._connect() as connection:
        connection.execute(
            "CREATE TRIGGER reject_selection BEFORE INSERT ON discovery_selections "
            "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
        )
    with pytest.raises(sqlite3.DatabaseError):
        store.record(key, DiscoverySkillReceipt.from_ref(ref(archive())))
    assert store.read(key) == ()
