from __future__ import annotations


def test_tool_gateway_allows_safe_tools_by_default(monkeypatch):
    from ksadk.tools.gateway import ToolGateway, ToolPolicy

    monkeypatch.delenv("KSADK_TOOL_APPROVAL_MODE", raising=False)
    gateway = ToolGateway({"demo_tool": ToolPolicy(risk_level="low")})

    result = gateway.invoke("demo_tool", lambda value: {"ok": True, "value": value}, "hello")

    assert result == {"ok": True, "value": "hello"}


def test_tool_gateway_returns_approval_request_for_risky_tools_in_strict_mode(monkeypatch):
    from ksadk.tools.gateway import ToolGateway, ToolPolicy

    called = False

    def dangerous_tool():
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", "strict")
    gateway = ToolGateway(
        {
            "dangerous_tool": ToolPolicy(
                risk_level="high",
                side_effects=("workspace_write",),
            )
        }
    )

    result = gateway.invoke("dangerous_tool", dangerous_tool)

    assert called is False
    assert result["ok"] is False
    assert result["type"] == "approval_required"
    assert result["approval_required"] is True
    assert result["approval_request"]["tool_name"] == "dangerous_tool"
    assert result["approval_request"]["risk_level"] == "high"
    assert result["approval_request"]["side_effects"] == ["workspace_write"]


def test_tool_gateway_approved_request_runs_risky_tool(monkeypatch):
    from ksadk.tools.gateway import ToolGateway, ToolPolicy

    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", "strict")
    gateway = ToolGateway({"dangerous_tool": ToolPolicy(risk_level="high")})

    result = gateway.invoke(
        "dangerous_tool",
        lambda: {"ok": True, "ran": True},
        approval={"approved": True},
    )

    assert result == {"ok": True, "ran": True}


def test_approval_interrupt_info_from_gateway_result():
    from ksadk.tools.gateway import approval_interrupt_info_from_result

    result = {
        "ok": False,
        "type": "approval_required",
        "approval_request": {
            "id": "appr_123",
            "tool_name": "write_workspace_file",
            "risk_level": "medium",
            "side_effects": ["workspace_write"],
        },
    }

    interrupt = approval_interrupt_info_from_result(
        result,
        fallback_tool_name="fallback",
        tool_args={"path": "notes.txt"},
        run_id="run-1",
    )

    assert interrupt == {
        "id": "appr_123",
        "approval_request_id": "appr_123",
        "tool_name": "write_workspace_file",
        "arguments": {"path": "notes.txt"},
        "risk_level": "medium",
        "side_effects": ["workspace_write"],
        "run_id": "run-1",
        "server_label": "ksadk",
    }
