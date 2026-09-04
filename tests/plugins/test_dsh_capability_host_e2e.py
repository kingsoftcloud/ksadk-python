"""Pinned DSH CLI -> full Cordis Profile -> standard MCP tools E2E."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ksadk.plugins.bridges.dsh import DshProfilePluginBridge
from ksadk.plugins.dsh_toolchain import DshToolchainManager
from ksadk.plugins.providers.dsh_capabilities import DshProfileCapabilityHost

PLUGIN_NAME = "@ksadk-test/dsh-node-tool-plugin"

pytestmark = pytest.mark.skipif(
    os.environ.get("KSADK_DSH_TOOLCHAIN_E2E") != "1",
    reason="set KSADK_DSH_TOOLCHAIN_E2E=1 to install the pinned public npm toolchain",
)


def _fixture_bundle() -> Path:
    return Path(__file__).parents[1] / "fixtures" / "dsh-node-tool-plugin"


@pytest.mark.asyncio
async def test_ordinary_cordis_tool_bundle_runs_through_profile_mcp(
    tmp_path: Path,
) -> None:
    toolchain = DshToolchainManager(base_dir=tmp_path / "toolchains")
    toolchain.install()
    dsh_home = tmp_path / "dsh-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = DshProfilePluginBridge(
        dsh_home=dsh_home,
        profile="ksadk-tool-capability-e2e",
        dsh_command=toolchain.require_command(),
        cwd=workspace,
    )
    host: DshProfileCapabilityHost | None = None
    try:
        bridge.start()
        installed = bridge.install_plugin(
            str(_fixture_bundle()),
            accept_host_permissions=True,
        )
        assert installed.name == PLUGIN_NAME
        assert installed.enabled is False
        bridge.set_enabled(PLUGIN_NAME, enabled=True)
        projection = bridge.project_profile()
        assert PLUGIN_NAME in projection.bundles
        installed_manifest = (
            dsh_home
            / "profiles"
            / "ksadk-tool-capability-e2e"
            / "node_modules"
            / "@ksadk-test"
            / "dsh-node-tool-plugin"
            / "package.json"
        )
        assert installed_manifest.is_file()
        assert "./provider-host" not in installed_manifest.read_text()

        host = DshProfileCapabilityHost(
            toolchain.require_command(),
            projection=projection,
            dsh_home=dsh_home,
            cwd=workspace,
        )
        lease = await host.start()
        assert any(tool.name == "fixture_echo" for tool in host.descriptor.tools)
        async with httpx.AsyncClient(
            headers=lease.headers(), timeout=10, trust_env=False
        ) as mcp_client:
            async with streamable_http_client(
                lease.endpoint,
                http_client=mcp_client,
            ) as (read_stream, write_stream, _session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert "fixture_echo" in {tool.name for tool in tools.tools}
                    result = await session.call_tool("fixture_echo", {"message": "real-cordis"})
                    assert result.isError is False
                    assert result.content[0].text == "real-cordis"
    finally:
        if host is not None:
            await host.dispose()
        try:
            bridge.uninstall_plugin(PLUGIN_NAME)
        except Exception:
            pass
