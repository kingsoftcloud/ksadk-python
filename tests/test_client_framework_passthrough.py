"""Client framework tests."""

import pytest

from ksadk.api.client import AgentEngineClient


def _build_create_payload() -> dict:
    return {
        "name": "deepagents-demo",
        "framework": "deepagents",
        "artifact_type": "Code",
        "artifact_path": "ks3://bucket/path/code.zip",
        "region": "cn-beijing-6",
    }


@pytest.mark.asyncio
async def test_create_agent_preserves_deepagents_when_server_supports_it(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"agent_id": "ar-new"}

    monkeypatch.setattr(client, "_action", fake_action)

    result = await client.create_agent(_build_create_payload())

    assert result["agent_id"] == "ar-new"
    assert len(calls) == 1
    assert calls[0][0] == "CreateAgentProduct"
    assert calls[0][1]["Framework"] == "deepagents"

