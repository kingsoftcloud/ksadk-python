"""Real upstream Cordis plugin -> standard MCP tools E2E, zero source changes.

Proves the proposal's core claim: an unmodified third-party plugin from the
public DeepSeek Harness registry can be installed through the pinned-source
bridge, projected by the capability host, and called over the standard MCP
endpoint — with no ksadk-private plugin format and no upstream patch.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ksadk.plugins.bridges.dsh import DshProfilePluginBridge, dsh_subprocess_environment
from ksadk.plugins.dsh_toolchain import DshToolchainManager
from ksadk.plugins.providers.dsh_capabilities import DshProfileCapabilityHost

# A real upstream third-party plugin from the public npm registry: a minimal
# Cordis tool plugin (inject: ['tools']) that registers a read_file tool.
# Its package.json ships WITHOUT dsh.bundle.patch; the bridge auto-generates
# a minimal patch so the profile loader activates it — the plugin's own code
# is untouched.
UPSTREAM_PLUGIN = "@npm_thanks-for-forest/my-dsh-tool"
UPSTREAM_VERSION = "0.1.2"
PINNED_SOURCE = f"{UPSTREAM_PLUGIN}@{UPSTREAM_VERSION}"
UPSTREAM_TOOL = "read_file"

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
    command = toolchain.require_command()
    subprocess.run(
        [*command, "--profile", "web", "--dump-config"],
        cwd=workspace,
        env=dsh_subprocess_environment(dsh_home=dsh_home),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    bridge = DshProfilePluginBridge(
        dsh_home=dsh_home,
        profile="web",
        dsh_command=command,
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
        # no ksadk manifest was injected. The only added file is an
        # auto-generated cordis.patch.yml (a compatibility shim for plugins
        # that ship without dsh.bundle.patch) plus the patch pointer in
        # package.json — the plugin's lib/ source is untouched.
        installed_pkg = (
            dsh_home
            / "profiles"
            / "web"
            / "node_modules"
            / "@npm_thanks-for-forest"
            / "my-dsh-tool"
        )
        assert (installed_pkg / "package.json").is_file()
        assert (installed_pkg / "lib" / "index.js").is_file()
        # The plugin's source declares inject: ['tools'] and registers
        # read_file — verify it was not modified.
        source = (installed_pkg / "lib" / "index.js").read_text(encoding="utf-8")
        assert "read_file" in source
        assert "ctx.tools.register" in source

        host = DshProfileCapabilityHost(
            toolchain.require_command(),
            projection=projection,
            dsh_home=dsh_home,
            cwd=workspace,
        )
        try:
            lease = await host.start()
        except Exception:
            stderr = "\n".join(host.stderr_tail) if host.stderr_tail else "<empty>"
            pytest.fail(f"capability host failed to start; sidecar stderr:\n{stderr}")
        assert lease.web_route_count == 2
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
                    # The upstream plugin projects its read_file tool with
                    # zero source modification.
                    assert UPSTREAM_TOOL in tool_names, (
                        f"{UPSTREAM_TOOL} not in {tool_names}"
                    )
                    # read_file reads a path; call it on a temp file.
                    probe = workspace / "probe.txt"
                    probe.write_text("upstream-roundtrip", encoding="utf-8")
                    result = await session.call_tool(
                        UPSTREAM_TOOL, {"path": str(probe)}
                    )
                    assert result.isError is False
                    assert result.content[0].text == "upstream-roundtrip"
    finally:
        if host is not None:
            await host.dispose()
        try:
            bridge.uninstall_plugin(UPSTREAM_PLUGIN)
        except Exception:
            pass
