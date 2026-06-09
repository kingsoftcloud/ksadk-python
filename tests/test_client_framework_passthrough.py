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
async def test_create_dashboard_access_link_can_omit_path(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"link_id": "dash-link"}

    monkeypatch.setattr(client, "_action", fake_action)

    await client.create_dashboard_access_link(
        agent_id="ar-openclaw",
        link_type="private",
        path=None,
        expires_seconds=3600,
    )

    assert calls == [
        (
            "CreateDashboardAccessLink",
            {
                "AgentId": "ar-openclaw",
                "LinkType": "private",
                "ForceNew": False,
                "ExpiresSeconds": 3600,
            },
        )
    ]


@pytest.mark.asyncio
async def test_create_agent_forwards_storage_configuration(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"agent_id": "ar-storage"}

    monkeypatch.setattr(client, "_action", fake_action)

    payload = _build_create_payload()
    payload["storage"] = {
        "mount_path": "/home/node/.agentengine",
        "size_gi": 20,
    }

    await client.create_agent(payload)

    assert calls[0][1]["Storage"] == {
        "MountPath": "/home/node/.agentengine",
        "SizeGi": 20,
    }


@pytest.mark.asyncio
async def test_create_agent_forwards_memory_configuration(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"agent_id": "ar-memory"}

    monkeypatch.setattr(client, "_action", fake_action)

    payload = _build_create_payload()
    payload["memory_config"] = {
        "memory_system": "mem0",
        "mem0_instance_id": "c17b20b1-faf7-4c98-91a7-38d1ee581ba1",
        "mem0_instance_name": "mem-demo",
        "mem0_region": "pre-online",
    }

    await client.create_agent(payload)

    assert calls[0][1]["MemoryConfig"] == {
        "MemorySystem": "mem0",
        "Mem0InstanceId": "c17b20b1-faf7-4c98-91a7-38d1ee581ba1",
        "Mem0InstanceName": "mem-demo",
        "Mem0Region": "pre-online",
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


@pytest.mark.asyncio
async def test_update_agent_forwards_storage_disable_configuration(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"agent_id": "ar-storage"}

    monkeypatch.setattr(client, "_action", fake_action)

    await client.update_agent(
        "ar-storage",
        {
            "storage": {
                "mount_path": "/home/node/.agentengine",
                "size_gi": 64,
            }
        },
    )

    assert calls[0][0] == "UpdateAgent"
    assert calls[0][1]["Storage"] == {
        "MountPath": "/home/node/.agentengine",
        "SizeGi": 64,
    }


@pytest.mark.asyncio
async def test_update_agent_forwards_memory_configuration(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"agent_id": "ar-memory"}

    monkeypatch.setattr(client, "_action", fake_action)

    await client.update_agent(
        "ar-memory",
        {
            "memory_config": {
                "memory_system": "openclaw_default",
            }
        },
    )

    assert calls[0][0] == "UpdateAgent"
    assert calls[0][1]["MemoryConfig"] == {
        "MemorySystem": "openclaw_default",
    }


@pytest.mark.asyncio
async def test_update_agent_forwards_observability_configuration(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"agent_id": "ar-observable"}

    monkeypatch.setattr(client, "_action", fake_action)

    await client.update_agent(
        "ar-observable",
        {
            "observability": {
                "langfuse_enabled": True,
            }
        },
    )

    assert calls[0][0] == "UpdateAgent"
    assert calls[0][1]["Advanced"]["EnableObservability"] is True


@pytest.mark.asyncio
async def test_list_agents_normalizes_multi_framework_string(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"Agents": [], "Total": 0}

    monkeypatch.setattr(client, "_action", fake_action)

    await client.list_agents(framework=" langgraph, adk ")

    assert calls[0][0] == "ListAgents"
    assert calls[0][1]["Framework"] == "langgraph,adk"


@pytest.mark.asyncio
async def test_list_agents_accepts_framework_sequences(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"Agents": [], "Total": 0}

    monkeypatch.setattr(client, "_action", fake_action)

    await client.list_agents(framework=["langgraph", "adk"])

    assert calls[0][0] == "ListAgents"
    assert calls[0][1]["Framework"] == "langgraph,adk"


@pytest.mark.asyncio
async def test_run_openclaw_repair_forwards_control_plane_action(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"ok": True, "repair_action": "doctor-fix"}

    monkeypatch.setattr(client, "_action", fake_action)

    result = await client.run_openclaw_repair("ar-openclaw-1")

    assert result == {"ok": True, "repair_action": "doctor-fix"}
    assert calls == [
        (
            "RunOpenClawRepair",
            {
                "AgentId": "ar-openclaw-1",
                "RepairAction": "doctor-fix",
            },
        )
    ]
