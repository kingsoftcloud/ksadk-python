import pytest

from ksadk.api.client import AgentEngineClient


@pytest.mark.asyncio
async def test_get_agent_name_uses_get_agent_only(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"AgentId": "ar-demo"}

    monkeypatch.setattr(client, "_action", fake_action)

    result = await client.get_agent(name="demo", include_api_key=True)

    assert result["AgentId"] == "ar-demo"
    assert calls == [("GetAgent", {"Name": "demo", "IncludeApiKey": True})]


@pytest.mark.asyncio
async def test_get_agent_name_does_not_fallback_to_legacy_action(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        if action == "GetAgent":
            raise Exception("HTTP 404 Not Found")
        raise Exception("unexpected legacy action")

    monkeypatch.setattr(client, "_action", fake_action)

    with pytest.raises(Exception, match="HTTP 404"):
        await client.get_agent(name="missing-agent")

    assert calls == [("GetAgent", {"Name": "missing-agent"})]


@pytest.mark.asyncio
async def test_get_agent_by_id_does_not_fallback_on_not_found_with_request_id(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        raise Exception(
            'HTTP 404 POST http://example.com/?Action=GetAgent&Version=2024-06-12: '
            '{"Code":404,"Message":"未找到对应的 Agent","RequestId":"abc-id-123"}'
        )

    monkeypatch.setattr(client, "_action", fake_action)

    with pytest.raises(Exception, match="HTTP 404"):
        await client.get_agent(agent_id="ar-missing")

    assert calls == [("GetAgent", {"AgentId": "ar-missing"})]


@pytest.mark.asyncio
async def test_get_agent_by_id_falls_back_only_for_legacy_field_compat(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        if len(calls) == 1:
            raise Exception(
                'HTTP 422 POST http://example.com/?Action=GetAgent&Version=2024-06-12: '
                '{"detail":[{"loc":["body","AgentId"],"msg":"extra inputs are not permitted"}]}'
            )
        return {"AgentId": "ar-demo"}

    monkeypatch.setattr(client, "_action", fake_action)

    result = await client.get_agent(agent_id="ar-demo")

    assert result["AgentId"] == "ar-demo"
    assert calls == [
        ("GetAgent", {"AgentId": "ar-demo"}),
        ("GetAgent", {"Id": "ar-demo"}),
    ]
