from ksadk.toolsets import agentengine_tool_dispatcher
from ksadk.toolsets import get_agentengine_tools
from ksadk.runtime_context import PlatformInvocationContext, platform_invocation_scope


class _FakeMemoryService:
    def __init__(self):
        self.save_calls = []
        self._backend = None

    def save_text(self, *, user_id: str, content: str, metadata: dict) -> bool:
        self.save_calls.append((user_id, content, metadata))
        return True


class _FailingMemoryService(_FakeMemoryService):
    def __init__(self):
        super().__init__()
        self._backend = type("Backend", (), {"last_error": "write not persisted"})()

    def save_text(self, *, user_id: str, content: str, metadata: dict) -> bool:
        self.save_calls.append((user_id, content, metadata))
        return False


def _context() -> PlatformInvocationContext:
    return PlatformInvocationContext(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        history=[],
        input_content=[],
        input_messages=[],
        input_parts=[],
        attachments=[],
        attachment_results=[],
        current_attachments=[],
        current_attachment_results=[],
        has_current_files=False,
        runner_type="langgraph",
    )


def test_dispatcher_list_returns_error_for_unknown_include_without_raising():
    result = agentengine_tool_dispatcher(action="list", include="file")

    assert result["ok"] is False
    assert result["error_type"] == "unknown_tool"
    assert result["tool_name"] == "file"


def test_dispatcher_langchain_tool_accepts_json_string_arguments():
    dispatcher = next(
        tool
        for tool in get_agentengine_tools(include=["agentengine_tool_dispatcher"])
        if tool.name == "agentengine_tool_dispatcher"
    )

    result = dispatcher.invoke(
        {
            "action": "list",
            "include": "workspace",
            "arguments": '{"unused": true}',
        }
    )

    assert result["ok"] is True
    assert result["tool_count"] > 0


def test_dispatcher_call_accepts_json_string_arguments():
    dispatcher = next(
        tool
        for tool in get_agentengine_tools(include=["agentengine_tool_dispatcher"])
        if tool.name == "agentengine_tool_dispatcher"
    )

    result = dispatcher.invoke(
        {
            "action": "call",
            "tool_name": "component_status",
            "arguments": "{}",
        }
    )

    assert result["ok"] is True
    assert result["tool_name"] == "component_status"
    assert result["result"]["ok"] is True


def test_dispatcher_describe_exposes_langchain_tool_args():
    result = agentengine_tool_dispatcher(action="describe", tool_name="save_memory")

    assert result["ok"] is True
    assert result["tool"]["args"]["content"]["type"] == "string"


def test_dispatcher_save_memory_accepts_key_value_arguments(monkeypatch):
    service = _FakeMemoryService()
    monkeypatch.setattr("ksadk.memory.tool._get_or_create_service", lambda: service)

    with platform_invocation_scope(_context()):
        result = agentengine_tool_dispatcher(
            action="call",
            tool_name="save_memory",
            arguments={"key": "user_name", "value": "张三"},
        )

    assert result == {
        "ok": True,
        "tool_name": "save_memory",
        "result": {"ok": True, "status": "persisted", "message": "记忆已保存。"},
    }
    assert service.save_calls == [
        (
            "user-1",
            "user_name: 张三",
            {
                "agent_id": "demo-agent",
                "session_id": "sess-1",
                "runner_type": "langgraph",
            },
        )
    ]


def test_dispatcher_propagates_save_memory_failure(monkeypatch):
    service = _FailingMemoryService()
    monkeypatch.setattr("ksadk.memory.tool._get_or_create_service", lambda: service)

    with platform_invocation_scope(_context()):
        result = agentengine_tool_dispatcher(
            action="call",
            tool_name="save_memory",
            arguments={"content": "用户喜欢云主机"},
        )

    assert result["ok"] is False
    assert result["tool_name"] == "save_memory"
    assert result["result"]["ok"] is False
    assert "记忆保存失败" in result["result"]["message"]
