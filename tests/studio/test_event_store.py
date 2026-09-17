from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ksadk.studio.contracts import RunRecord, RunStatus
from ksadk.studio.event_store import RunEventStore
from ksadk.studio.run_service import _ProjectedRunEventWriter
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


def test_list_runs_reuses_cached_run_instead_of_reparsing_on_every_poll(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    record = _record("run_cached")
    store.create(record)

    def fail_reparse(_payload: str):
        raise AssertionError("unchanged run file must be served from the stat-aware cache")

    monkeypatch.setattr("ksadk.studio.event_store.json.loads", fail_reparse)

    assert store.list_runs(session_id=record.session_id) == [record]


def test_projected_writer_coalesces_adjacent_token_envelopes() -> None:
    def entry(text: str, *, operation: str = "append") -> tuple[str, dict]:
        return (
            "message.delta",
            {
                "itemId": "item-1",
                "partId": "text-0",
                "operation": operation,
                "text": text,
                "conversationItem": {"payload": {"text": text}},
                "runtimeEvent": {"update": {"text": text}},
            },
        )

    projected = _ProjectedRunEventWriter._coalesce_stream_entries(
        [entry("长"), entry("任务"), entry("最终", operation="replace")]
    )

    assert len(projected) == 2
    assert projected[0][1]["text"] == "长任务"
    assert projected[0][1]["conversationItem"]["payload"]["text"] == "长任务"
    assert projected[0][1]["runtimeEvent"]["update"]["text"] == "长任务"
    assert projected[1][1]["text"] == "最终"


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


def test_interaction_resolution_and_receipt_share_one_durable_transition(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    record = _record("run_interaction", status=RunStatus.WAITING_INPUT)
    store.create(record)

    resolved, action, receipt = store.append_interaction_resolution(
        record.id,
        resolved_type="approval.resolved",
        resolved_data={"interactionId": "approval-1", "revision": 2},
        action_data={
            "interactionId": "approval-1",
            "revision": 2,
            "idempotencyKey": "interaction:approval-1:revision-1",
        },
    )

    assert (resolved.id, action.id) == (1, 2)
    assert receipt == {
        "runId": record.id,
        "interactionId": "approval-1",
        "status": "resolved",
        "revision": 2,
        "resolutionEventId": 1,
        "eventId": 2,
    }
    persisted = json.loads((tmp_path / ".agentkit/runs/run_interaction.json").read_text())
    assert [event["type"] for event in persisted["events"]] == [
        "approval.resolved",
        "a2ui.action",
    ]
    assert persisted["events"][1]["data"]["receipt"] == receipt
