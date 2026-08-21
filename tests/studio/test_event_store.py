from __future__ import annotations

import json
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


def test_event_store_writes_only_run_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _record("run_demo")
    store.create(record)

    payload = json.loads((tmp_path / ".agentkit/runs/run_demo.json").read_text())
    assert set(payload) == {"record"}


def test_event_store_reads_legacy_events_but_drops_them_on_save(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _record("run_legacy")
    path = tmp_path / ".agentkit/runs/run_legacy.json"
    path.write_text(
        json.dumps(
            {
                "record": record.model_dump(by_alias=True, mode="json"),
                "events": [
                    {
                        "id": 1,
                        "runId": record.id,
                        "type": "run.started",
                        "data": {},
                    }
                ],
            }
        )
    )

    assert store.get(record.id) == record
    store.save(record)
    # PCM 保留 Studio lifecycle events (memory.recall.*, …) 以便跨重启读取；
    # legacy 事件同样被保留。
    assert set(json.loads(path.read_text())) == {"record", "events"}
