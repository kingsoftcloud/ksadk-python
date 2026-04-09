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


@pytest.mark.asyncio
async def test_create_agent_forwards_network_configuration(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"agent_id": "ar-network"}

    monkeypatch.setattr(client, "_action", fake_action)

    payload = _build_create_payload()
    payload["network"] = {
        "enable_public_access": False,
        "enable_vpc_access": True,
        "vpc_id": "vpc-demo",
        "subnet_id": "subnet-demo",
        "security_group_id": "sg-demo",
        "availability_zone": "cn-beijing-6a",
    }

    await client.create_agent(payload)

    assert calls[0][1]["Network"] == {
        "EnablePublicAccess": False,
        "EnableVpcAccess": True,
        "VpcId": "vpc-demo",
        "SubnetId": "subnet-demo",
        "SecurityGroupId": "sg-demo",
        "AvailabilityZone": "cn-beijing-6a",
    }


@pytest.mark.asyncio
async def test_create_agent_forwards_ui_config(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"agent_id": "ar-ui"}

    monkeypatch.setattr(client, "_action", fake_action)

    payload = _build_create_payload()
    payload["ui_config"] = {
        "profile": "custom",
        "path": "/chat",
        "url": "https://ui.example.com/custom-ui/",
    }

    await client.create_agent(payload)

    assert calls[0][1]["UiConfig"] == {
        "Profile": "custom",
        "Path": "/chat",
        "Url": "https://ui.example.com/custom-ui/",
    }


@pytest.mark.asyncio
async def test_update_agent_forwards_network_configuration(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"agent_id": "ar-network"}

    monkeypatch.setattr(client, "_action", fake_action)

    await client.update_agent(
        "ar-network",
        {
            "network": {
                "enable_public_access": True,
                "enable_vpc_access": True,
                "vpc_id": "vpc-demo",
                "subnet_id": "subnet-demo",
                "security_group_id": "sg-demo",
            }
        },
    )

    assert calls[0][0] == "UpdateAgent"
    assert calls[0][1]["Network"] == {
        "EnablePublicAccess": True,
        "EnableVpcAccess": True,
        "VpcId": "vpc-demo",
        "SubnetId": "subnet-demo",
        "SecurityGroupId": "sg-demo",
    }


@pytest.mark.asyncio
async def test_update_agent_forwards_ui_config(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"agent_id": "ar-ui"}

    monkeypatch.setattr(client, "_action", fake_action)

    await client.update_agent(
        "ar-ui",
        {
            "ui_config": {
                "profile": "custom",
                "path": "/chat",
                "url": "https://ui.example.com/custom-ui/",
            }
        },
    )

    assert calls[0][0] == "UpdateAgent"
    assert calls[0][1]["UiConfig"] == {
        "Profile": "custom",
        "Path": "/chat",
        "Url": "https://ui.example.com/custom-ui/",
    }
