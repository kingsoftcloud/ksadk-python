from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ksadk.a2a.tool_outcomes import (
    A2ASendOutcomeUnknown,
    assert_target_outcome_known,
    tool_error_result,
    unknown_service_outcome,
)
from ksadk.events.canonical import ErrorInfo, ItemCompleted, ItemFailed, SourceRef
from ksadk.events.content import ContentSnapshot, DataContent


def facts():
    common = dict(schema_version=2, event_id="failed", seq=1, timestamp=1.0,
                  run_id="current", scope_id="unknown-child", parent_scope_id="parent",
                  source=SourceRef(framework="a2a"), item_id="status", item_kind="status")
    failed = ItemFailed(**common, error=ErrorInfo(
        code="A2A_SEND_OUTCOME_UNKNOWN", message="Do not resend", source="a2a",
        scope_id="unknown-child",
    ))
    descriptor = ItemCompleted(**{**common, "event_id": "descriptor", "seq": 2,
                                 "item_id": "descriptor", "item_kind": "data"},
                               snapshot=ContentSnapshot(parts=(DataContent(
                                   part_id="descriptor", data={
                                       "schema": "execution.scope/v1", "status": "failed",
                                       "agent": {"id": "target"},
                                   },
                               ),)))
    return failed, descriptor


@pytest.mark.asyncio
async def test_replacement_tool_call_is_blocked_from_durable_current_run_facts():
    failed, descriptor = facts()
    authority = SimpleNamespace(session_id="session", list=AsyncMock(
        side_effect=[[failed], [descriptor]],
    ))
    with pytest.raises(A2ASendOutcomeUnknown) as error:
        await assert_target_outcome_known(authority, "target")
    result = tool_error_result(error.value)
    assert result["state"] == "unknown" and result["retryable"] is False
    assert result["scope_id"] == "unknown-child"
    assert "Do not resend" in result["error_message"]
    assert authority.list.call_args_list[0].kwargs == {
        "event_types": ["item.failed"], "item_kinds": ["status"],
    }
    assert authority.list.call_args_list[1].kwargs == {
        "scope_id": "unknown-child", "event_types": ["item.completed"],
        "item_kinds": ["data"],
    }


@pytest.mark.asyncio
async def test_new_user_run_and_unrelated_target_are_not_permanently_blocked():
    failed, descriptor = facts()
    authority = SimpleNamespace(session_id="session", list=AsyncMock(return_value=[]))
    await assert_target_outcome_known(authority, "target")
    authority.list.side_effect = [[failed], [descriptor]]
    await assert_target_outcome_known(authority, "another-target")


def test_unknown_result_preserves_task_identity_without_raw_exception_text(caplog):
    handle = SimpleNamespace(target_agent_id="target", platform_task_id="a2a-task-original")
    service = SimpleNamespace(handle=lambda scope: handle,
                              snapshot=lambda h: SimpleNamespace(status="unknown"))
    error = unknown_service_outcome(service, "scope")
    assert isinstance(error, A2ASendOutcomeUnknown)
    error.__cause__ = OSError("DO_NOT_LOG_SECRET_PAYLOAD")
    result = tool_error_result(error)
    assert result["task_id"] == "a2a-task-original"
    assert result["error_type"] == "A2A_SEND_OUTCOME_UNKNOWN"
    assert "DO_NOT_LOG_SECRET_PAYLOAD" not in caplog.text
    assert "OSError" in caplog.text
    assert unknown_service_outcome(SimpleNamespace(
        handle=lambda scope: handle, snapshot=lambda h: SimpleNamespace(status="completed"),
    ), "scope") is None


def test_arbitrary_exception_strings_are_not_exposed_as_error_codes(caplog):
    result = tool_error_result(RuntimeError("A2A_SECRET user payload"))
    assert result["error_type"] == "A2A_INVOCATION_FAILED"
    assert "user payload" not in caplog.text


@pytest.mark.asyncio
async def test_native_tool_message_marks_an_uncertain_result_as_error():
    from ksadk.a2a.langgraph_tools import ManagedLangGraphTool

    async def call_remote(message: str):
        """Test managed remote outcome formatting."""
        return A2ASendOutcomeUnknown(agent="target", scope_id="unknown").result

    tool = ManagedLangGraphTool.from_function(coroutine=call_remote)
    result = await tool.ainvoke({"type": "tool_call", "id": "actual-native-id",
                                "name": tool.name, "args": {"message": "research"}})
    assert result.tool_call_id == "actual-native-id" and result.status == "error"
    assert "A2A_SEND_OUTCOME_UNKNOWN" in result.content
