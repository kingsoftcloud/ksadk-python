from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ksadk.studio.contracts import RunRecord, RunStatus
from ksadk.studio.event_store import RunEventStore
from ksadk.studio.workspace import Workspace


def _store(tmp_path: Path) -> RunEventStore:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    return RunEventStore(workspace)


def _record(run_id: str, *, status: RunStatus = RunStatus.CREATED) -> RunRecord:
    return RunRecord(
        id=run_id,
        build_id="build_demo",
        agent_id="demo-agent",
        session_id=f"ses_{run_id}",
        trace_id=f"trace_{run_id}",
        input="hello",
        status=status,
        started_at=datetime.now(timezone.utc) if status == RunStatus.RUNNING else None,
    )


def test_event_store_supports_last_event_id_semantics(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _record("run_demo")
    store.create(record)
    store.append(record.id, "run.created", {})
    store.append(record.id, "run.started", {})

    assert [event.id for event in store.events(record.id, after=1)] == [2]


def test_event_store_recovers_persisted_terminal_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _record("run_refresh", status=RunStatus.RUNNING)
    store.create(record)
    store.append(record.id, "run.cancelled", {"status": "cancelled"})

    assert store.recover_interrupted() == 1
    recovered = store.get(record.id)
    assert recovered.status == RunStatus.CANCELLED
    assert recovered.completed_at is not None
    assert recovered.error == {"code": "RUN_CANCELLED", "message": "运行已取消"}


def test_event_store_marks_orphaned_running_run_interrupted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _record("run_orphaned", status=RunStatus.RUNNING)
    store.create(record)
    store.append(record.id, "run.started", {})

    assert store.recover_interrupted() == 1
    recovered = store.get(record.id)
    assert recovered.status == RunStatus.INTERRUPTED
    assert recovered.error == {
        "code": "RUN_INTERRUPTED",
        "message": "Studio 重启后无法重新 attach 上一次本地运行",
    }
    assert store.events(record.id)[-1].type == "run.interrupted"
