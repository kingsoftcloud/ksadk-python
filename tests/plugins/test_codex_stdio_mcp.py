from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ksadk.plugins.host import PluginHostError
from ksadk.plugins.providers.codex import _resolve_mcp
from ksadk.runtime.factory import (
    _manifest_has_network_mcp,
    _manifest_mcp_overrides,
    create_runtime_adapter,
)
from ksadk.runtime.launch import RuntimeLaunchContext, RuntimeServices
from ksadk.studio.codex_manifest import CodexAgentManifest, CodexRuntimeRef


class _Credentials:
    def resolve(self, reference: str) -> str:
        assert reference == "secret://web-token"
        return "resolved-but-never-serialized"


def _manifest(server: dict) -> CodexAgentManifest:
    return CodexAgentManifest(
        name="stdio-agent",
        version="1.0.0",
        runtime=CodexRuntimeRef(version="0.147.0"),
        model="test-model",
        prompt="Use the selected plugin tool.",
        mcp_servers=[server],
    )


def test_codex_manifest_accepts_stdio_mcp_without_http_url() -> None:
    manifest = _manifest(
        {
            "name": "plugin-tools",
            "transport": "stdio",
            "command": "node",
            "args": ["plugin/server.mjs"],
            "env_refs": {"WEB_TOKEN": "secret://web-token"},
        }
    )

    assert manifest.mcp_servers is not None
    assert manifest.mcp_servers[0]["command"] == "node"


def test_bundle_stdio_mcp_resolves_credentials_as_parent_env_names() -> None:
    servers, environment = _resolve_mcp(
        [
            {
                "name": "plugin-tools",
                "transport": "stdio",
                "command": "node",
                "args": ["plugin/server.mjs"],
                "envRefs": {"WEB_TOKEN": "secret://web-token"},
            }
        ],
        credential_resolver=_Credentials(),
    )

    assert servers == [
        {
            "name": "plugin-tools",
            "transport": "stdio",
            "command": "node",
            "args": ["plugin/server.mjs"],
            "env_refs": {"WEB_TOKEN": "secret://web-token"},
        }
    ]
    assert environment == {"WEB_TOKEN": "resolved-but-never-serialized"}
    overrides = _manifest_mcp_overrides({"mcp_servers": servers})
    assert f"mcp_servers.plugin-tools.command={json.dumps('node')}" in overrides
    assert 'mcp_servers.plugin-tools.args=["plugin/server.mjs"]' in overrides
    assert 'mcp_servers.plugin-tools.env_vars=["WEB_TOKEN"]' in overrides
    assert all("resolved-but-never-serialized" not in item for item in overrides)
    assert _manifest_has_network_mcp({"mcp_servers": servers}) is False


def test_only_http_or_sse_mcp_requests_codex_sandbox_network_access() -> None:
    assert _manifest_has_network_mcp(
        {
            "mcp_servers": [
                {"name": "local", "transport": "stdio", "command": "node", "args": []}
            ]
        }
    ) is False
    assert _manifest_has_network_mcp(
        {
            "mcp_servers": [
                {"name": "remote", "transport": "http", "url": "https://mcp.invalid/rpc"}
            ]
        }
    ) is True


@pytest.mark.parametrize(
    ("server", "expected_network"),
    [
        (
            {"name": "local", "transport": "stdio", "command": "node", "args": []},
            False,
        ),
        (
            {"name": "remote", "transport": "http", "url": "https://mcp.invalid/rpc"},
            True,
        ),
    ],
)
def test_factory_only_enables_workspace_network_for_remote_mcp(
    tmp_path: Path,
    server: dict[str, Any],
    expected_network: bool,
) -> None:
    pytest.importorskip("openai_codex")
    captured: list[Any] = []

    def client_factory(config=None):
        captured.append(config)
        return object()

    create_runtime_adapter(
        RuntimeLaunchContext(
            runtime_type="codex",
            project_dir=tmp_path,
            config={"sandbox": "workspace_write", "mcp_servers": [server]},
            services=RuntimeServices(codex_client_factory=client_factory),
        )
    )

    overrides = tuple(captured[0].config_overrides or ())
    assert ("sandbox_workspace_write.network_access=true" in overrides) is expected_network


@pytest.mark.parametrize(
    "server",
    [
        {"name": "bad", "transport": "stdio", "command": "bash", "args": []},
        {"name": "bad", "transport": "stdio", "command": "node", "args": ["--eval"]},
    ],
)
def test_stdio_mcp_rejects_shell_and_inline_evaluation(server: dict) -> None:
    with pytest.raises(PluginHostError, match="cannot"):
        _resolve_mcp([server], credential_resolver=None)


def test_codex_manifest_rejects_mixed_stdio_and_url() -> None:
    with pytest.raises(ValidationError, match="不能配置 url"):
        _manifest(
            {
                "name": "mixed",
                "transport": "stdio",
                "command": "node",
                "args": [],
                "url": "http://127.0.0.1:1234/mcp",
            }
        )
