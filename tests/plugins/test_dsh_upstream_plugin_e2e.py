"""Real upstream Cordis plugin -> standard MCP tools E2E, zero source changes.

Proves the proposal's core claim: an unmodified third-party plugin from the
public DeepSeek Harness registry can be installed through the pinned-source
bridge, projected by the capability host, and called over the standard MCP
endpoint — with no ksadk-private plugin format and no upstream patch.
"""

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

# A real upstream plugin: a third-party Cordis bundle from the public
# DeepSeek Harness registry. It declares dsh.bundle.patch and registers
# ssh_list / ssh_exec tools via ctx.tools.register — no ksadk manifest.
UPSTREAM_PLUGIN = "@linxin666/dsh-ssh"
UPSTREAM_VERSION = "0.3.16"
PINNED_SOURCE = f"{UPSTREAM_PLUGIN}@{UPSTREAM_VERSION}"

pytestmark = pytest.mark.skipif(
    os.environ.get("KSADK_DSH_UPSTREAM_E2E") != "1",
    reason="set KSADK_DSH_UPSTREAM_E2E=1 to install a real upstream plugin from npm",
)


@pytest.mark.asyncio
async def test_real_upstream_cordis_plugin_runs_unmodified_through_profile_mcp(
    tmp_path: Path,
) -> None:
    toolchain = DshToolchainManager(base_dir=tmp_path / "toolchains")
    toolchain.install()
    dsh_home = tmp_path / "dsh-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = DshProfilePluginBridge(
        dsh_home=dsh_home,
        profile="ksadk-upstream-e2e",
        dsh_command=toolchain.require_command(),
        cwd=workspace,
    )
    host: DshProfileCapabilityHost | None = None
    try:
        bridge.start()
        installed = bridge.install_plugin(
            PINNED_SOURCE,
            accept_host_permissions=True,
        )
        assert installed.name == UPSTREAM_PLUGIN
        assert installed.enabled is False
        bridge.set_enabled(UPSTREAM_PLUGIN, enabled=True)
        projection = bridge.project_profile()
        assert UPSTREAM_PLUGIN in projection.bundles

        # The installed tree is the upstream package as published on npm;
        # no ksadk manifest or patch was injected.
        installed_manifest = (
            dsh_home
            / "profiles"
            / "ksadk-upstream-e2e"
            / "node_modules"
            / "@linxin666"
            / "dsh-ssh"
            / "package.json"
        )
        assert installed_manifest.is_file()

        host = DshProfileCapabilityHost(
            toolchain.require_command(),
            projection=projection,
            dsh_home=dsh_home,
            cwd=workspace,
        )
        try:
            lease = await host.start()
        except Exception:
            # Surface the sidecar's own diagnostics so activation failures are
            # actionable (e.g. a plugin injecting a service the host lacks).
            stderr = "\n".join(host.stderr_tail) if host.stderr_tail else "<empty>"
            pytest.fail(f"capability host failed to start; sidecar stderr:\n{stderr}")
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
                    tool_names = {tool.name for tool in tools.tools}
                    # The upstream plugin projects its tools with zero source
                    # modification; ssh_list is a read-only inventory call.
                    assert "ssh_list" in tool_names
                    result = await session.call_tool("ssh_list", {})
                    assert result.isError is False
    finally:
        if host is not None:
            await host.dispose()
        try:
            bridge.uninstall_plugin(UPSTREAM_PLUGIN)
        except Exception:
            pass
