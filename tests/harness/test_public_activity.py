from ksadk.harness.public_activity import tool_public_action


def test_only_display_safe_fields_leave_tool_payload():
    assert tool_public_action(
        {
            "name": "web_search",
            "args": {"query": "ADK checkpoint", "api_key": "private"},
            "result": "private",
        }
    ) == {"text": "搜索资料：ADK checkpoint"}
    assert tool_public_action(
        {
            "name": "write_workspace_file",
            "args": {"path": "/private/work/报告.md", "content": "private"},
        }
    ) == {"text": "保存文件：报告.md"}
    assert tool_public_action({"name": "unknown", "args": {"query": "private"}}) is None


def test_url_credentials_queries_and_fragments_are_removed():
    assert tool_public_action(
        {
            "name": "web_fetch",
            "args": {"url": "https://user:pass@example.com/docs?token=private#private"},
        }
    ) == {"text": "查看网页：example.com/docs", "href": "https://example.com/docs"}
    for url in [
        "file:///etc/passwd",
        "http://127.0.0.1/admin",
        "http://localhost/x",
        "https://example.com/token/private",
        "http://[::1]/",
        "http://service.internal/",
    ]:
        assert tool_public_action({"name": "web_fetch", "args": {"url": url}}) is None


def test_sensitive_queries_and_command_arguments_are_not_displayed():
    for query in ["api_key=private", "Bearer private", "password:private", "token private"]:
        assert tool_public_action({"name": "web_search", "args": {"query": query}}) is None
    assert (
        tool_public_action(
            {"name": "web_search", "args": {"query": "https://me:private@example.com/docs"}}
        )
        is None
    )
    assert tool_public_action(
        {"name": "exec_command", "args": {"cmd": "rg 'secret123' /private/file"}}
    ) == {"text": "运行命令：rg（参数未展示）"}
    assert (
        tool_public_action({"name": "exec_command", "args": {"cmd": "SECRET=private tool"}}) is None
    )


def test_canonical_tool_projection_includes_fact_without_raw_arguments():
    import json

    from ksadk.harness.events import EventType, RuntimeEvent
    from ksadk.harness.managed_runtime import _project_event

    event = RuntimeEvent(
        event_id="e",
        timestamp=1,
        user_id="u",
        seq_id=1,
        event_type=EventType.TOOL_CALL_BEGIN,
        agent_id="a",
        session_id="s",
        invocation_id="r",
        payload={
            "name": "web_search",
            "call_id": "c",
            "args": {"query": "ADK", "api_key": "private"},
        },
    )
    data = json.loads(_project_event(event)[0].snapshot.parts[0].text)
    assert data["details"]["public_action"] == {"text": "搜索资料：ADK"}
    assert "private" not in str(data)
