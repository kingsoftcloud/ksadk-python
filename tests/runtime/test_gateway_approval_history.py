"""Typed and snapshot tool results share the same historical approval fence."""

from types import SimpleNamespace

import pytest

from ksadk.events.canonical import ContentSnapshot, InteractionRequested, ItemCompleted, SourceRef
from ksadk.events.content import DataContent, ToolCallContent, ToolResultContent
from ksadk.runtime.adapter import RunHandle
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter


@pytest.mark.parametrize("result_wire", ["typed", "values"])
@pytest.mark.parametrize("history_scope", [None, "live-scope", "other-scope"])
def test_replayed_tool_result_does_not_reopen_historical_approval(result_wire, history_scope):
    adapter = LangGraphRuntimeAdapter(SimpleNamespace())
    handle = RunHandle(run_id="run", session_id="session", runtime_type="langgraph", native_ref={})
    result = {
        "ok": False,
        "type": "approval_required",
        "approval_request": {
            "id": "approval-write",
            "tool_name": "write_file",
            "risk_level": "medium",
            "side_effects": ["workspace_write"],
        },
    }

    def event(part, scope, channel="tools"):
        return ItemCompleted(
            schema_version=2,
            event_id=f"{scope}-{part.part_id}",
            seq=1,
            timestamp=1,
            run_id="run",
            scope_id=scope,
            source=SourceRef(framework="langgraph", metadata={"channel": channel}),
            item_id=part.part_id,
            item_kind="data",
            snapshot=ContentSnapshot(parts=(part,)),
        )

    def snapshot():
        return DataContent(
            part_id="state",
            data={
                "messages": [
                    {
                        "type": "tool",
                        "id": "different-message-id",
                        "tool_call_id": "native-write",
                        "content": result,
                    }
                ]
            },
        )

    if history_scope:
        assert not any(
            isinstance(e, InteractionRequested)
            for e in adapter._chunk_to_event(
                handle, None, event(snapshot(), history_scope, "values")
            )
        )
    call = ToolCallContent(
        part_id="call", call_id="native-write", name="write_file", arguments={"path": "hello.md"}
    )
    adapter._chunk_to_event(handle, None, event(call, "live-scope"))
    result_event = (
        event(
            ToolResultContent(part_id="result", call_id="native-write", result=result), "live-scope"
        )
        if result_wire == "typed"
        else event(snapshot(), "live-scope", "values")
    )
    mapped = adapter._chunk_to_event(handle, None, result_event)
    approvals = [e for e in mapped if isinstance(e, InteractionRequested)]
    assert len(approvals) == (0 if history_scope == "live-scope" else 1)
    if approvals:
        assert approvals[0].request.call_id == "native-write"
        assert approvals[0].request.detail["arguments"] == {"path": "hello.md"}
