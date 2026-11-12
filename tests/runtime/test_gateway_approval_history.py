"""Typed and snapshot tool results share the same historical approval fence."""

from types import SimpleNamespace

import pytest

from ksadk.events.canonical import ContentSnapshot, InteractionRequested, ItemCompleted, SourceRef
from ksadk.events.content import DataContent, ToolCallContent, ToolResultContent
from ksadk.runtime.adapter import RunHandle
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter


@pytest.mark.parametrize("result_wire", ["typed", "values"])
@pytest.mark.parametrize("history_scope", [None, "live-scope", "other-scope"])
@pytest.mark.parametrize("history_channel", ["values", "updates"])
@pytest.mark.parametrize("history_message_identity", [
    {"id": "different-message-id"}, {"id": None}, {},
], ids=["identified", "null-id", "missing-id"])
def test_replayed_tool_result_does_not_reopen_historical_approval(
    result_wire, history_scope, history_channel, history_message_identity,
):
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

    def snapshot(*, history=False):
        identity = history_message_identity if history else {"id": "different-message-id"}
        data = {
            "messages": [{
                "type": "tool",
                **identity,
                "tool_call_id": "native-write",
                "content": result,
            }],
        }
        if history and history_channel == "updates":
            data = {"tools": data}
        return DataContent(
            part_id="state",
            data=data,
        )

    if history_scope:
        assert not any(
            isinstance(e, InteractionRequested)
            for e in adapter._chunk_to_event(
                handle, None, event(snapshot(history=True), history_scope, history_channel)
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
    fenced = history_scope == "live-scope" and (
        history_channel == "values" or bool(history_message_identity.get("id"))
    )
    assert len(approvals) == (0 if fenced else 1)
    if approvals:
        assert approvals[0].request.call_id == "native-write"
        assert approvals[0].request.detail["arguments"] == {"path": "hello.md"}
