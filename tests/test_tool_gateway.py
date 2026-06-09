from __future__ import annotations

from ksadk.tools.gateway import (
    ToolGateway,
    ToolPolicy,
    approval_interrupt_info_from_result,
    default_tool_gateway,
    tool_policy_requires_approval,
)


def test_tool_gateway_imports_public_api():
    gateway = default_tool_gateway({"delete_file": ToolPolicy(risk_level="high")})

    assert isinstance(gateway, ToolGateway)


def test_tool_policy_requires_approval_only_in_strict_mode():
    policy = ToolPolicy(risk_level="high")

    assert tool_policy_requires_approval(policy, approval_mode="off") is False
    assert tool_policy_requires_approval(policy, approval_mode="permissive") is False
    assert tool_policy_requires_approval(policy, approval_mode="strict") is True


def test_tool_gateway_returns_approval_request_in_strict_mode(monkeypatch):
    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", "strict")
    gateway = ToolGateway({"write_file": ToolPolicy(risk_level="medium", side_effects=("workspace_write",))})

    result = gateway.invoke("write_file", lambda: {"ok": True})

    assert result["type"] == "approval_required"
    assert result["approval_required"] is True
    assert result["approval_request"]["tool_name"] == "write_file"
    assert result["approval_request"]["risk_level"] == "medium"
    assert result["approval_request"]["side_effects"] == ["workspace_write"]


def test_tool_gateway_runs_approved_call_in_strict_mode(monkeypatch):
    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", "strict")
    gateway = ToolGateway({"write_file": ToolPolicy(risk_level="medium")})

    assert gateway.invoke("write_file", lambda value: {"ok": True, "value": value}, 3, approval={"approved": True}) == {
        "ok": True,
        "value": 3,
    }


def test_approval_interrupt_info_from_result_normalizes_payload():
    result = {
        "type": "approval_required",
        "approval_request": {
            "id": "appr_123",
            "tool_name": "write_file",
            "tool_args": {"path": "demo.txt"},
            "risk_level": "medium",
            "side_effects": ["workspace_write"],
        },
    }

    interrupt = approval_interrupt_info_from_result(result, fallback_tool_name="fallback", run_id="run_1")

    assert interrupt == {
        "id": "appr_123",
        "approval_request_id": "appr_123",
        "tool_name": "write_file",
        "arguments": {"path": "demo.txt"},
        "risk_level": "medium",
        "side_effects": ["workspace_write"],
        "server_label": "ksadk",
        "run_id": "run_1",
    }
