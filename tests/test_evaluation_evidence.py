import pytest

from ksadk.evaluation.contracts import DataPolicy
from ksadk.evaluation.evidence import EvidenceStore, EvidenceStoreError, project_tool_calls
from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    ItemStarted,
    RunStarted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import TextContent, ToolCallContent, ToolResultContent


def _base(seq: int) -> dict:
    return {
        "schema_version": 2,
        "event_id": f"evt-{seq}",
        "seq": seq,
        "timestamp": float(seq),
        "run_id": "invocation-1",
        "scope_id": "scope-1",
        "source": SourceRef(framework="ksadk"),
    }


def _run_started(seq: int) -> RuntimeEvent:
    return RunStarted(**_base(seq), status="running")


def _tool_started(seq: int, call_id: str, name: str, arguments: object = {}) -> RuntimeEvent:
    return ItemStarted(
        **_base(seq),
        item_id=f"item-{call_id}",
        item_kind="tool_call",
        initial=ContentSnapshot(parts=(
            ToolCallContent(part_id=call_id, call_id=call_id, name=name, arguments=arguments),
        )),
    )


def _tool_completed(seq: int, call_id: str, name: str, result: object, *, is_error: bool = False) -> RuntimeEvent:
    return ItemCompleted(
        **_base(seq),
        item_id=f"item-{call_id}",
        item_kind="tool_call",
        snapshot=ContentSnapshot(parts=(
            ToolCallContent(part_id=call_id, call_id=call_id, name=name, arguments={}),
            ToolResultContent(part_id=f"result-{call_id}", call_id=call_id, result=result, is_error=is_error),
        )),
    )


def test_project_tool_calls_pairs_start_and_completed_snapshots() -> None:
    projected = project_tool_calls([
        _tool_started(2, "call-1", "lookup", {"query": "secret"}),
        _tool_completed(3, "call-1", "lookup", "private"),
    ])
    assert [item.model_dump() for item in projected] == [{
        "call_id": "call-1", "name": "lookup", "status": "SUCCEEDED", "seq_start": 2, "seq_end": 3,
    }]


def test_project_tool_calls_preserves_incomplete_and_failed_calls() -> None:
    projected = project_tool_calls([
        _tool_started(4, "call-open", "open"),
        _tool_completed(7, "call-failed", "write", "denied", is_error=True),
    ])
    assert [(item.call_id, item.status) for item in projected] == [
        ("call-open", "INCOMPLETE"), ("call-failed", "ERROR"),
    ]


@pytest.mark.parametrize("policy, expected_text", [
    (DataPolicy.LOCAL_ONLY, "private answer"),
    (DataPolicy.FULL_TRACE, "private answer"),
    (DataPolicy.REDACTED_TRACE, "[REDACTED]"),
    (DataPolicy.METADATA_ONLY, None),
])
def test_evidence_store_applies_data_policy_and_returns_queryable_trace(tmp_path, policy, expected_text) -> None:
    events = [
        _run_started(1),
        _tool_started(2, "call-1", "lookup", {"token": "secret"}),
        ItemCompleted(
            **_base(3), item_id="message-1", item_kind="message",
            snapshot=ContentSnapshot(parts=(TextContent(part_id="text-1", text="private answer"),)),
        ),
    ]
    store = EvidenceStore(tmp_path)
    trace_ref = store.write_trace("eval-1", events, session_id="session-1", policy=policy)
    trace = store.read_trace(trace_ref)

    assert trace_ref.run_id == "eval-1"
    assert trace_ref.session_id == "session-1"
    assert trace_ref.invocation_id == "invocation-1"
    assert (trace_ref.seq_start, trace_ref.seq_end) == (1, 3)
    text_event = next(item for item in trace["events"] if item["eventType"] == "item.completed")
    parts = text_event.get("event", {}).get("snapshot", {}).get("parts", [])
    assert (parts[0].get("text") if parts else None) == expected_text
    assert "secret" not in repr(trace) or policy in {DataPolicy.LOCAL_ONLY, DataPolicy.FULL_TRACE}


def test_evidence_store_rejects_unscoped_or_escaped_reads(tmp_path) -> None:
    store = EvidenceStore(tmp_path)
    with pytest.raises(EvidenceStoreError):
        store.write_trace("../outside", [_run_started(1)], session_id="session-1")
    with pytest.raises(EvidenceStoreError):
        store.write_trace("eval-1", [_run_started(1)], session_id="session:private")


def test_evidence_store_keeps_trace_paths_within_windows_path_budget(tmp_path) -> None:
    root = tmp_path / ("workspace-" + "x" * 30)
    root.mkdir()
    store = EvidenceStore(root)
    event = RunStarted(**(_base(1) | {"run_id": "run_" + "i" * 32}), status="running")
    trace = store.read_trace(store.write_trace(
        "eval-" + "r" * 32,
        [event],
        session_id="eval-build-session-" + "s" * 24,
    ))
    assert trace["seqEnd"] == 1
