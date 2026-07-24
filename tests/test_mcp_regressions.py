"""Regression coverage for MCP detection and API response contracts."""

import pytest

from ksadk.api.client import AgentEngineAPIError, AgentEngineClient
from ksadk.builders.mcp_builder import MCPCodeBuilder
from ksadk.detection import FrameworkType
from ksadk.detection.mcp_detector import MCPDetector


def test_mcp_detection_result_supports_code_builder_fingerprinting(tmp_path):
    (tmp_path / "server.py").write_text(
        "from fastmcp import FastMCP\n\n" "mcp = FastMCP(name='demo')\n",
        encoding="utf-8",
    )

    detection_result = MCPDetector(str(tmp_path)).detect()

    assert detection_result.type is FrameworkType.FASTMCP
    fingerprint = MCPCodeBuilder(tmp_path)._build_input_fingerprint(detection_result)
    assert fingerprint["fingerprint"]


def test_mcp_list_response_normalizes_plural_acronym_key():
    response = AgentEngineClient._to_snake_case({"MCPs": [{"MCPId": "mcp-1"}], "Total": 1})

    assert response == {"mcps": [{"mcp_id": "mcp-1"}], "total": 1}


@pytest.mark.asyncio
async def test_delete_mcp_uses_deleted_response_flag(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com")
    monkeypatch.setattr(client, "_action", lambda *_args, **_kwargs: {"deleted": False})

    assert await client.delete_mcp("mcp-1") is False


@pytest.mark.asyncio
async def test_delete_mcp_preserves_api_error(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com")

    def fail_delete(*_args, **_kwargs):
        raise RuntimeError("control-plane delete failed")

    monkeypatch.setattr(client, "_action", fail_delete)

    with pytest.raises(RuntimeError, match="control-plane delete failed"):
        await client.delete_mcp("mcp-1")


def test_action_error_preserves_request_context(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com")
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: {
            "Code": 500,
            "Message": "internal error",
            "RequestId": "req-delete-1",
            "Action": "DeleteMCP",
        },
    )

    with pytest.raises(AgentEngineAPIError) as exc_info:
        client._action("DeleteMCP", {"Id": "mcp-1"})

    assert exc_info.value.details == {
        "request_id": "req-delete-1",
        "action": "DeleteMCP",
    }
