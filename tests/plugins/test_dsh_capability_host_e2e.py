"""Pinned DSH CLI -> full Cordis Profile -> standard MCP tools E2E."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ksadk.plugins.bridges.dsh import DshProfilePluginBridge, dsh_subprocess_environment
from ksadk.plugins.dsh_home import studio_dsh_home
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
            / "web"
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
        # An independently running DSH (or another Studio workspace) may own
        # the default port. The supervised Core must use an OS-assigned port.
        assert urlsplit(lease.endpoint).port != 3080
        assert lease.web_route_count == 2
        assert any(tool.name == "fixture_echo" for tool in host.descriptor.tools)
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=10, trust_env=False
        ) as browser:
            handoff = await browser.get(lease.browser_url())
            assert handoff.status_code == 303
            assert handoff.headers["location"] == "/"
            assert "HttpOnly" in handoff.headers["set-cookie"]
            core = await browser.get(f"http://127.0.0.1:{handoff.url.port}/")
            assert core.status_code == 200
            assert "<!doctype html" in core.text.lower()
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


@pytest.mark.asyncio
async def test_shipped_studio_profiles_boot_with_supported_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ksadk.plugins.dsh_toolchain import DSH_VERSION
    from ksadk.plugins.providers.harness_dsh import shipped_harness_dsh_bundle
    from ksadk.studio.dsh_capability_service import StudioDshCapabilityService
    from ksadk.studio.dsh_provider_registration import StudioDshProviderRegistrationManager

    toolchains = tmp_path / "toolchains"
    toolchain = DshToolchainManager(base_dir=toolchains)
    toolchain.install()
    monkeypatch.setenv("AGENTENGINE_PLUGIN_TOOLCHAIN_HOME", str(toolchains))
    for key in ("KSADK_DSH_HOME", "KSADK_DSH_PROFILE", "KSADK_DSH_BIN"):
        monkeypatch.delenv(key, raising=False)
    workspace = tmp_path / "studio"
    workspace.mkdir()
    for resource_profile in (False, True):
        manager = (
            StudioDshProviderRegistrationManager.create_workspace_resource_default(workspace)
            if resource_profile
            else StudioDshProviderRegistrationManager.discover_or_create_workspace_default(
                workspace
            )
        )
        assert manager is not None
        capability = (
            StudioDshCapabilityService.create_workspace_resource_default(workspace)
            if resource_profile
            else StudioDshCapabilityService.discover_or_create_workspace_default(workspace)
        )
        try:
            if not resource_profile:
                assert await manager.bootstrap_official_codex_provider() == "installed"
                with DshProfilePluginBridge(
                    dsh_home=studio_dsh_home(workspace), profile="web",
                    dsh_command=toolchain.require_command(), cwd=workspace,
                ) as bridge:
                    harness = bridge.install_plugin(
                        str(shipped_harness_dsh_bundle().root), accept_host_permissions=True,
                    )
                    bridge.set_enabled(harness.name, enabled=True)
            assert await manager.bootstrap_official_resource_plugins() == "installed"
            registrations = await manager.start()
            assert registrations.inventory.state == "ready"
            if not resource_profile:
                assert "plugin://io.ksadk.harness-provider@1.0.0" in registrations.manifests
                assert "plugin://io.ksadk.codex-provider@1.0.0" in registrations.manifests
            snapshot = await capability.capability_snapshot()
            assert snapshot.descriptor.dsh_version == DSH_VERSION
            assert snapshot.inventory.state == "ready"
            names = {tool.name for tool in snapshot.tools}
            assert {
                "load_memory", "save_memory", "execute_skills", "search_knowledge_base",
            } <= names
        finally:
            await capability.aclose()
            await manager.aclose()
