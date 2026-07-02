from ksadk.toolsets import agentengine_tool_dispatcher
from ksadk.toolsets import get_agentengine_tools
from ksadk.toolsets import (
    clear_external_tools,
    read_workspace_file,
    edit_workspace_file,
    list_workspace_files,
    register_external_tools,
    search_workspace_files,
    tool_dispatcher,
    tool_search,
    multi_edit_workspace_file,
)
from ksadk.toolsets.workspace_state import clear_read_state
from ksadk.runtime_context import PlatformInvocationContext, platform_invocation_scope, tool_execution_scope


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


def test_tool_dispatcher_is_canonical_and_agentengine_dispatcher_is_alias():
    canonical = tool_dispatcher(action="describe", tool_name="component_status")
    compat = agentengine_tool_dispatcher(action="describe", tool_name="component_status")

    assert canonical["ok"] is True
    assert canonical == compat
    names = {tool.name for tool in get_agentengine_tools(include=["tool_dispatcher"])}
    assert names == {"tool_dispatcher"}


def test_tool_search_discovers_workspace_edit_tools():
    result = tool_search("edit a file after reading it", profile="coding", max_results=5)

    assert result["ok"] is True
    names = [item["name"] for item in result["results"]]
    assert "read_workspace_file" in names
    assert "edit_workspace_file" in names
    assert result["deferred_tool_names"][:2]
    assert all("score" in item and item["score"] > 0 for item in result["results"])


def test_tool_search_discovers_registered_framework_mcp_tools():
    class WeatherForecastTool:
        name = "weather_forecast"
        description = "Get weather forecast for a city from the weather MCP server."
        args = {"city": {"type": "string", "description": "City name"}}

    clear_external_tools()
    try:
        registered = register_external_tools(
            [WeatherForecastTool()],
            group="mcp:weather",
            boundary="framework_managed_mcp_tool",
        )

        result = tool_search("weather forecast", profile="coding", max_results=5)
    finally:
        clear_external_tools()

    assert registered == ["weather_forecast"]
    forecast = next(item for item in result["results"] if item["name"] == "weather_forecast")
    assert forecast["group"] == "mcp:weather"
    assert forecast["boundary"] == "framework_managed_mcp_tool"
    assert forecast["execution"] == "external"
    assert forecast["args"]["city"]["type"] == "string"
    assert "weather_forecast" in result["deferred_tool_names"]


def test_coding_profile_exposes_focused_direct_tools_and_deferred_mode_exposes_search_dispatcher():
    coding_names = {tool.name for tool in get_agentengine_tools(profile="coding", mode="direct")}
    deferred_names = {tool.name for tool in get_agentengine_tools(profile="coding", mode="deferred")}

    assert {"read_workspace_file", "edit_workspace_file", "multi_edit_workspace_file", "tool_search"} <= coding_names
    assert deferred_names == {"tool_search", "tool_dispatcher"}


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


def test_workspace_read_returns_line_metadata_and_records_state(monkeypatch, tmp_path):
    clear_read_state()
    monkeypatch.setattr("ksadk.toolsets.workspace.resolve_local_session_dir", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "demo.py").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = read_workspace_file("demo.py", start_line=2, end_line=3)

    assert result["ok"] is True
    assert result["content"] == "2 | two\n3 | three"
    assert result["start_line"] == 2
    assert result["end_line"] == 3
    assert result["total_lines"] == 3
    assert result["line_count"] == 2
    assert result["read_range"] == {"start": 2, "end": 3}
    assert result["suggested_action"] == ""
    assert result["partial"] is True
    assert result["mtime_ns"] > 0


def test_workspace_edit_requires_prior_read(monkeypatch, tmp_path):
    clear_read_state()
    monkeypatch.setattr("ksadk.toolsets.workspace.resolve_local_session_dir", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "demo.py").write_text("print('hello')\n", encoding="utf-8")

    result = edit_workspace_file("demo.py", "hello", "world")

    assert result["ok"] is False
    assert result["error_type"] == "file_not_read"
    assert "read_workspace_file" in result["suggested_action"]


def test_workspace_read_state_is_isolated_by_tool_session(monkeypatch, tmp_path):
    clear_read_state()
    monkeypatch.setattr("ksadk.toolsets.workspace.resolve_local_session_dir", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "demo.py").write_text("print('hello')\n", encoding="utf-8")

    with tool_execution_scope(session_id="sess-a"):
        assert read_workspace_file("demo.py")["ok"] is True

    with tool_execution_scope(session_id="sess-b"):
        unread = edit_workspace_file("demo.py", "hello", "world")

    with tool_execution_scope(session_id="sess-a"):
        edited = edit_workspace_file("demo.py", "hello", "world")

    assert unread["ok"] is False
    assert unread["error_type"] == "file_not_read"
    assert edited["ok"] is True
    assert (workspace / "demo.py").read_text(encoding="utf-8") == "print('world')\n"


def test_workspace_read_state_default_context_supports_direct_read_then_edit(monkeypatch, tmp_path):
    clear_read_state()
    monkeypatch.setattr("ksadk.toolsets.workspace.resolve_local_session_dir", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "demo.py").write_text("print('hello')\n", encoding="utf-8")

    assert read_workspace_file("demo.py")["ok"] is True
    result = edit_workspace_file("demo.py", "hello", "world")

    assert result["ok"] is True
    assert (workspace / "demo.py").read_text(encoding="utf-8") == "print('world')\n"


def test_workspace_edit_checks_mtime_and_updates_read_state(monkeypatch, tmp_path):
    clear_read_state()
    monkeypatch.setattr("ksadk.toolsets.workspace.resolve_local_session_dir", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "demo.py"
    target.write_text("print('hello')\nprint('again')\n", encoding="utf-8")
    assert read_workspace_file("demo.py")["ok"] is True
    target.write_text("print('external')\n", encoding="utf-8")

    stale = edit_workspace_file("demo.py", "external", "changed")

    assert stale["ok"] is False
    assert stale["error_type"] == "file_modified_since_read"
    assert read_workspace_file("demo.py")["ok"] is True
    first = edit_workspace_file("demo.py", "external", "changed")
    second = edit_workspace_file("demo.py", "changed", "changed again")
    assert first["ok"] is True
    assert second["ok"] is True
    assert "changed again" in target.read_text(encoding="utf-8")


def test_workspace_edit_supports_quote_normalization_and_replace_all(monkeypatch, tmp_path):
    clear_read_state()
    monkeypatch.setattr("ksadk.toolsets.workspace.resolve_local_session_dir", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "demo.txt"
    target.write_text("“name”\n“name”\n", encoding="utf-8")
    assert read_workspace_file("demo.txt")["ok"] is True

    ambiguous = edit_workspace_file("demo.txt", '"name"', "value")
    replaced = edit_workspace_file("demo.txt", '"name"', "value", replace_all=True)

    assert ambiguous["ok"] is False
    assert ambiguous["error_type"] == "ambiguous_edit"
    assert replaced["ok"] is True
    assert replaced["used_quote_normalization"] is True
    assert replaced["replacements"] == 2
    assert target.read_text(encoding="utf-8") == "value\nvalue\n"


def test_workspace_edit_returns_match_diagnostics_for_not_found_and_ambiguous(monkeypatch, tmp_path):
    clear_read_state()
    monkeypatch.setattr("ksadk.toolsets.workspace.resolve_local_session_dir", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "demo.py"
    target.write_text("alpha = 1\nalpha = 2\nalphabet = 3\n", encoding="utf-8")
    assert read_workspace_file("demo.py")["ok"] is True

    missing = edit_workspace_file("demo.py", "alpah = 2", "alpha = 20")
    ambiguous = edit_workspace_file("demo.py", "alpha = ", "beta = ")

    assert missing["ok"] is False
    assert missing["error_type"] == "snippet_not_found"
    assert missing["nearby_candidates"]
    assert missing["nearby_candidates"][0]["line"] == 2
    assert ambiguous["ok"] is False
    assert ambiguous["error_type"] == "ambiguous_edit"
    assert [match["line"] for match in ambiguous["matches"]] == [1, 2]


def test_workspace_multi_edit_is_atomic_and_returns_budgeted_diff(monkeypatch, tmp_path):
    clear_read_state()
    monkeypatch.setenv("KSADK_TOOL_RESULT_DIR", str(tmp_path / "tool-results"))
    monkeypatch.setattr("ksadk.toolsets.workspace.resolve_local_session_dir", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "demo.py"
    target.write_text("one = 1\ntwo = 2\nthree = 3\n", encoding="utf-8")
    assert read_workspace_file("demo.py")["ok"] is True

    failed = multi_edit_workspace_file(
        "demo.py",
        edits=[
            {"old_text": "one = 1", "new_text": "one = 10"},
            {"old_text": "missing = 0", "new_text": "missing = 1"},
        ],
    )
    assert failed["ok"] is False
    assert target.read_text(encoding="utf-8") == "one = 1\ntwo = 2\nthree = 3\n"

    result = multi_edit_workspace_file(
        "demo.py",
        edits=[
            {"old_text": "one = 1", "new_text": "one = 10"},
            {"old_text": "three = 3", "new_text": "three = 30"},
        ],
    )

    assert result["ok"] is True
    assert result["edit_count"] == 2
    assert result["replacements"] == 2
    assert "one = 10" in target.read_text(encoding="utf-8")
    assert "diff" in result


def test_workspace_search_supports_regex_glob_context_and_max_results(monkeypatch, tmp_path):
    monkeypatch.setattr("ksadk.toolsets.workspace.shutil.which", lambda _name: None)
    monkeypatch.setattr("ksadk.toolsets.workspace.resolve_local_session_dir", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("alpha\nneedle_123\nomega\n", encoding="utf-8")
    (workspace / "b.txt").write_text("needle_456\n", encoding="utf-8")

    result = search_workspace_files(r"needle_\d+", glob="*.py", is_regex=True, context_lines=1, max_results=1)

    assert result["ok"] is True
    assert len(result["results"]) == 1
    assert result["results"][0]["path"] == "a.py"
    assert result["results"][0]["line"] == 2
    assert "alpha" in result["results"][0]["context_before"]
    assert "omega" in result["results"][0]["context_after"]
    assert result["truncated"] is False
    assert result["searched_path"] == "."
    assert result["match_count"] == 1
    assert result["context_lines"] == 1
    assert result["search_backend"] == "python"


def test_workspace_search_marks_truncated_when_matches_exceed_limit(monkeypatch, tmp_path):
    monkeypatch.setattr("ksadk.toolsets.workspace.shutil.which", lambda _name: None)
    monkeypatch.setattr("ksadk.toolsets.workspace.resolve_local_session_dir", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("needle\n", encoding="utf-8")
    (workspace / "b.py").write_text("needle\n", encoding="utf-8")

    result = search_workspace_files("needle", glob="*.py", max_results=1)

    assert result["ok"] is True
    assert len(result["results"]) == 1
    assert result["truncated"] is True


def test_workspace_list_supports_glob_sort_and_include_dirs(monkeypatch, tmp_path):
    clear_read_state()
    monkeypatch.setattr("ksadk.toolsets.workspace.resolve_local_session_dir", lambda: tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "b.py").write_text("b", encoding="utf-8")
    (workspace / "a.py").write_text("aaaa", encoding="utf-8")
    (workspace / "pkg" / "c.txt").write_text("c", encoding="utf-8")

    result = list_workspace_files(".", glob="*.py", recursive=True, include_dirs=False, sort_by="size")

    assert result["ok"] is True
    assert [entry["path"] for entry in result["entries"]] == ["b.py", "a.py"]
