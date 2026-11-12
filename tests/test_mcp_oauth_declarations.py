import pytest

from ksadk.deployment.mcp import component_mcp_payload, project_mcp_declarations, redact_mcp_secrets
from ksadk.mcp_runtime import MCPServerConfig
from ksadk.mcp_runtime.oauth.client import BrokerError
from ksadk.mcp_runtime.oauth.langgraph import validate_tool_arguments


@pytest.mark.parametrize("client_options", [{}, {"client_id": "fixture-client", "client_ref": "fixture-ref"}])
@pytest.mark.parametrize("entrypoint", ["project", "component", "config", "environment"])
def test_invalid_project_oauth_declaration_never_exposes_resolved_secret(client_options, entrypoint):
    import json

    from ksadk.cli.error_utils import cli_error_from_exception
    from ksadk.mcp_runtime import load_mcp_server_configs

    secret = "FAKESECRET"
    declaration = {
        "name": "fixture", "url": "https://provider.example/mcp", "auth_mode": "oauth",
        "oauth": {"scopes": ["r"], **client_options},
    }
    with pytest.raises(ValueError) as caught:
        if entrypoint == "project":
            declaration["oauth"]["client_secret_env"] = "FIXTURE_CLIENT_SECRET"
            project_mcp_declarations(
                {"mcp_servers": [declaration]}, environment={"FIXTURE_CLIENT_SECRET": secret},
            )
        else:
            declaration["oauth"]["client_secret"] = secret
            if entrypoint == "component":
                component_mcp_payload([declaration])
            elif entrypoint == "config":
                MCPServerConfig(**declaration)
            else:
                load_mcp_server_configs(json.dumps([declaration]))

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret not in repr(cli_error_from_exception(caught.value))


def test_published_declaration_has_no_internal_id_and_secrets_are_write_only(monkeypatch):
    monkeypatch.setenv("TEST_MCP_CLIENT_SECRET", "fixture-secret-only")
    declaration = {
        "name": "cloud",
        "url": "https://example.com/mcp",
        "auth_mode": "oauth",
        "oauth": {
            "scopes": ["read"],
            "client_id": "c",
            "client_secret_env": "TEST_MCP_CLIENT_SECRET",
            "token_endpoint_auth_method": "client_secret_post",
        },
    }
    config = {"mcp_servers": [declaration]}
    values = project_mcp_declarations(config)
    assert "fixture-secret-only" not in repr(values)
    assert "client_secret" not in config["mcp_servers"][0]["oauth"]
    payload = component_mcp_payload(values)
    assert payload[0]["oauth"]["client_secret"] == "fixture-secret-only"
    assert "fixture-secret-only" not in repr(redact_mcp_secrets(payload))
    MCPServerConfig(**payload[0])
    assert project_mcp_declarations({}) is None
    assert project_mcp_declarations({"mcp_servers": []}) == []
    assert project_mcp_declarations({"mcp_servers": [
        {"auth_mode": "static_key", "api_key": "static-fixture"}
    ]}) == []



def test_callapi_rules_constrain_actual_arguments():
    schema = {
        "type": "object",
        "required": ["product", "apiName", "apiVersion"],
        "properties": {
            "product": {"const": "eip"},
            "apiName": {"const": "DescribeAddresses"},
            "apiVersion": {"const": "2016-03-04"},
        },
    }
    credentials = {"allowed_tools": ["CallApi"], "tool_policies": {"CallApi": schema}}
    args = {"product": "eip", "apiName": "DescribeAddresses", "apiVersion": "2016-03-04"}
    validate_tool_arguments(credentials, "CallApi", args)
    for changed in [{**args, "apiName": "DeleteAddress"}, {"product": "eip"}]:
        with pytest.raises(BrokerError, match="MCP_TOOL_ARGUMENTS_NOT_ALLOWED"):
            validate_tool_arguments(credentials, "CallApi", changed)
    # MCP schemas define required inputs; developer policies only add limits.
    validate_tool_arguments({"allowed_tools": ["CallApi"]}, "CallApi", args)
    with pytest.raises(BrokerError, match="MCP_TOOL_NOT_ALLOWED"):
        validate_tool_arguments(credentials, "OtherTool", {})


def test_client_secret_from_project_env_never_enters_runtime_env(tmp_path):
    from ksadk.deployment.mcp import exclude_mcp_client_secrets

    (tmp_path / ".env").write_text("CUSTOM_OAUTH_SECRET=local-fixture-secret\n")
    values = project_mcp_declarations(
        {
            "mcp_servers": [
                {
                    "name": "cloud",
                    "url": "https://example.com/mcp",
                    "auth_mode": "oauth",
                    "oauth": {
                        "scopes": ["read"],
                        "client_id": "c",
                        "client_secret_env": "CUSTOM_OAUTH_SECRET",
                        "token_endpoint_auth_method": "client_secret_post",
                    },
                }
            ]
        },
        project_dir=tmp_path,
    )
    assert component_mcp_payload(values)[0]["oauth"]["client_secret"] == "local-fixture-secret"
    env = {
        "CUSTOM_OAUTH_SECRET": "local-fixture-secret",
        "ALIAS": "prefix-local-fixture-secret",
        "BUSINESS": "keep",
    }
    assert exclude_mcp_client_secrets(env, values) == {"BUSINESS": "keep"}


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact", ["Code", "Container"])
async def test_agent_publish_payload_keeps_oauth_connections_on_both_paths(monkeypatch, artifact):
    from ksadk.api.client import AgentEngineClient

    client = AgentEngineClient(base_url="https://platform.example", access_key="", secret_key="")
    calls = []
    monkeypatch.setattr(
        client, "_action", lambda action, params: calls.append((action, params)) or {}
    )
    components = {
        "MCPServers": [
            {
                "name": "cloud",
                "url": "https://provider.example/mcp",
                "auth_mode": "oauth",
                "oauth": {"scopes": ["read"]},
            }
        ]
    }
    await client.create_agent(
        {
            "name": "fixture",
            "framework": "langgraph",
            "artifact_type": artifact,
            "artifact_path": "ks3://bucket/code.zip"
            if artifact == "Code"
            else "registry.example/team/image@sha256:" + "a" * 64,
            "component_config": components,
        }
    )
    assert calls[-1][1]["Advanced"]["ComponentConfig"] == components
    assert calls[-1][1]["DeploymentType"] == artifact
    await client.update_agent("agent", {"component_config": {"MCPServers": []}})
    assert calls[-1][1]["Advanced"]["ComponentConfig"] == {"MCPServers": []}
