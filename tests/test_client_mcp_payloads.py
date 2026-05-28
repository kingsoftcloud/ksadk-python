"""Client MCP payload contract tests."""

import pytest

from ksadk.api.client import AgentEngineClient


@pytest.mark.asyncio
async def test_create_mcp_code_uses_nested_server_schema(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls: list[tuple[str, dict]] = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"mcp_id": "mcp-created"}

    monkeypatch.setattr(client, "_action", fake_action)

    result = await client.create_mcp(
        {
            "name": "demo-mcp",
            "description": "demo",
            "artifact_type": "Code",
            "artifact_path": "ks3://demo-bucket/mcps/demo-mcp/code_20260324120000.zip",
            "region": "pre-online",
            "enable_auth": True,
            "resources": {"cpu": "2", "memory": "4Gi"},
            "scaling": {"min_replicas": 2, "max_replicas": 8, "concurrency": 35},
            "metadata": {"mcp_variable": "server", "tools": ["ping", "add"]},
            "ks3": {
                "access_key": "ak",
                "secret_key": "sk",
                "region": "pre-online",
                "bucket": "demo-bucket",
            },
        }
    )

    assert result["mcp_id"] == "mcp-created"
    assert calls == [
        (
            "CreateMCP",
            {
                "Name": "demo-mcp",
                "Description": "demo",
                "Region": "cn-beijing-6",
                "DeploymentType": "Code",
                "Resource": {"Cpu": 2, "Memory": 4},
                "Scaling": {"MinReplicas": 2, "MaxReplicas": 8, "QpsPerInstance": 35},
                "Access": {"AuthType": "ApiKey"},
                "Advanced": {"McpVariable": "server", "Tools": ["ping", "add"]},
                "CodeConfig": {
                    "Path": "ks3://demo-bucket/mcps/demo-mcp/code_20260324120000.zip",
                    "AccessKey": "ak",
                    "SecretKey": "sk",
                    "Region": "cn-beijing-6",
                    "Bucket": "demo-bucket",
                },
            },
        )
    ]


@pytest.mark.asyncio
async def test_create_mcp_container_uses_nested_container_config(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls: list[tuple[str, dict]] = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"mcp_id": "mcp-created"}

    monkeypatch.setattr(client, "_action", fake_action)

    await client.create_mcp(
        {
            "name": "demo-mcp",
            "artifact_type": "Container",
            "artifact_path": "hub.kce.ksyun.com/agentengine/demo-mcp:v0.3.6",
            "region": "cn-beijing-6",
            "enable_auth": False,
            "metadata": {"mcp_variable": "mcp", "tools": ["ping"]},
            "image_credential": {"username": "demo-user", "password": "demo-pass"},
        }
    )

    assert calls[0][0] == "CreateMCP"
    payload = calls[0][1]
    assert payload["DeploymentType"] == "Container"
    assert payload["Access"] == {"AuthType": "None"}
    assert payload["ContainerConfig"] == {
        "ImageType": "Personal",
        "NameSpace": "agentengine",
        "ImageRepo": "demo-mcp",
        "ImageVersion": "v0.3.6",
        "ImageAddr": "hub.kce.ksyun.com/agentengine/demo-mcp:v0.3.6",
        "UserName": "demo-user",
        "Password": "demo-pass",
    }


@pytest.mark.asyncio
async def test_create_mcp_includes_network_only_when_explicit(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls: list[tuple[str, dict]] = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"mcp_id": "mcp-created"}

    monkeypatch.setattr(client, "_action", fake_action)

    await client.create_mcp(
        {
            "name": "demo-mcp",
            "artifact_type": "Code",
            "artifact_path": "ks3://demo-bucket/mcps/demo-mcp/code.zip",
        }
    )
    await client.create_mcp(
        {
            "name": "demo-mcp",
            "artifact_type": "Code",
            "artifact_path": "ks3://demo-bucket/mcps/demo-mcp/code.zip",
            "network": {
                "enable_public_access": False,
                "enable_vpc_access": True,
                "vpc_id": "vpc-cli",
                "subnet_id": "subnet-cli",
                "security_group_id": "sg-cli",
                "availability_zone": "cn-beijing-6b",
            },
        }
    )

    assert "Network" not in calls[0][1]
    assert calls[1][1]["Network"] == {
        "EnablePublicAccess": False,
        "EnableVpcAccess": True,
        "VpcId": "vpc-cli",
        "SubnetId": "subnet-cli",
        "SecurityGroupId": "sg-cli",
        "AvailabilityZone": "cn-beijing-6b",
    }


@pytest.mark.asyncio
async def test_update_mcp_uses_nested_partial_sections(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls: list[tuple[str, dict]] = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"mcp_id": "mcp-updated"}

    monkeypatch.setattr(client, "_action", fake_action)

    await client.update_mcp(
        "mcp-123",
        {
            "artifact_type": "Container",
            "artifact_path": "hub.kce.ksyun.com/agentengine/demo-mcp:v0.3.7",
            "enable_auth": True,
            "scaling": {"min_replicas": 1, "max_replicas": 3, "concurrency": 12},
            "metadata": {"mcp_variable": "svc", "tools": ["ping", "health"]},
        },
    )

    assert calls[0][0] == "UpdateMCP"
    assert calls[0][1] == {
        "Id": "mcp-123",
        "DeploymentType": "Container",
        "ContainerConfig": {
            "ImageType": "Personal",
            "NameSpace": "agentengine",
            "ImageRepo": "demo-mcp",
            "ImageVersion": "v0.3.7",
            "ImageAddr": "hub.kce.ksyun.com/agentengine/demo-mcp:v0.3.7",
        },
        "Scaling": {"MinReplicas": 1, "MaxReplicas": 3, "QpsPerInstance": 12},
        "Access": {"AuthType": "ApiKey"},
        "Advanced": {"McpVariable": "svc", "Tools": ["ping", "health"]},
    }


@pytest.mark.asyncio
async def test_update_mcp_can_send_network_without_artifact(monkeypatch):
    client = AgentEngineClient(base_url="http://example.com", access_key="", secret_key="")
    calls: list[tuple[str, dict]] = []

    def fake_action(action: str, params: dict):
        calls.append((action, params.copy()))
        return {"mcp_id": "mcp-updated"}

    monkeypatch.setattr(client, "_action", fake_action)

    await client.update_mcp(
        "mcp-123",
        {
            "network": {
                "enable_public_access": False,
                "vpc_id": "vpc-cli",
                "subnet_id": "subnet-cli",
                "security_group_id": "sg-cli",
            },
        },
    )

    assert calls[0] == (
        "UpdateMCP",
        {
            "Id": "mcp-123",
            "Network": {
                "EnablePublicAccess": False,
                "EnableVpcAccess": False,
                "VpcId": "vpc-cli",
                "SubnetId": "subnet-cli",
                "SecurityGroupId": "sg-cli",
            },
        },
    )
