from ksadk.toolsets import agentengine_tool_dispatcher


def test_dispatcher_list_returns_error_for_unknown_include_without_raising():
    result = agentengine_tool_dispatcher(action="list", include="file")

    assert result["ok"] is False
    assert result["error_type"] == "unknown_tool"
    assert result["tool_name"] == "file"

