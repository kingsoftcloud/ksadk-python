from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from ksadk.plugins.bridges.codex import CodexAppServerPluginBridge, CodexBridgeError
from ksadk.plugins.codex_manifest import (
    CodexPluginSourceCoordinate,
    snapshot_installed_codex_plugin,
)
from tests.e2e.codex_responses_stub import DeterministicResponsesStub


class _McpStatus(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    tools: dict[str, dict]


class _McpStatusList(BaseModel):
    model_config = ConfigDict(extra="allow")
    data: tuple[_McpStatus, ...]


class _McpToolCall(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    content: tuple[dict, ...]
    structured_content: dict | None = Field(default=None, alias="structuredContent")
    is_error: bool | None = Field(default=None, alias="isError")


class _LoseFirstInstallReceipt:
    """Forward to a real App Server, then lose its first install response."""

    def __init__(self, delegate) -> None:  # noqa: ANN001
        self._delegate = delegate
        self.failed = False

    async def start(self) -> None:
        await self._delegate.start()

    async def close(self) -> None:
        await self._delegate.close()

    async def initialize(self):  # noqa: ANN201
        return await self._delegate.initialize()

    async def request(self, method, params, *, response_model):  # noqa: ANN001, ANN201
        response = await self._delegate.request(
            method,
            params,
            response_model=response_model,
        )
        if method == "plugin/install" and not self.failed:
            self.failed = True
            raise RuntimeError("injected loss after real App Server install")
        return response


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("KSADK_CODEX_PLUGIN_E2E") != "1",
    reason="set KSADK_CODEX_PLUGIN_E2E=1 to exercise the real Codex App Server",
)
async def test_real_codex_app_server_turn_uses_installed_plugin_skill(
    tmp_path: Path,
) -> None:
    """Prove install, model-visible skill injection, and uninstall in one host."""

    from openai_codex.async_client import AsyncCodexClient
    from openai_codex.client import CodexConfig
    from openai_codex.generated.v2_all import TurnStatus

    marketplace = str(Path(__file__).parent / "fixtures" / "codex-marketplace")
    plugin_name = "ksadk-bridge-e2e"
    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    codex_home.mkdir()
    workspace.mkdir()

    with DeterministicResponsesStub() as responses:
        (codex_home / "config.toml").write_text(
            f'''model = "ksadk-codex-plugin-stub"
model_provider = "ksadk_plugin_stub"
approval_policy = "never"
sandbox_mode = "read-only"

[model_providers.ksadk_plugin_stub]
name = "KsADK deterministic plugin E2E"
base_url = "{responses.base_url}"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
requires_openai_auth = false
''',
            encoding="utf-8",
        )
        client = AsyncCodexClient(
            CodexConfig(
                codex_bin=os.getenv("KSADK_CODEX_PLUGIN_E2E_BIN"),
                cwd=str(workspace),
                env={
                    "CODEX_HOME": str(codex_home),
                    "CODEX_APP_SERVER_DISABLE_MANAGED_CONFIG": "1",
                    "RUST_LOG": "warn",
                },
            )
        )
        async with CodexAppServerPluginBridge(transport=client) as bridge:
            marketplace_receipt = await bridge.add_marketplace_with_receipt(marketplace)
            marketplace_name = marketplace_receipt.marketplace_name
            assert Path(marketplace_receipt.installed_root).resolve() == Path(marketplace).resolve()
            before = await bridge.read_plugin(plugin_name, marketplace_name=marketplace_name)
            assert before.inventory.installed is False

            installed = await bridge.install_plugin(
                plugin_name,
                marketplace_name=marketplace_name,
                accept_undeclared_permissions=True,
                install_attempt_id="ksadk-codex-plugin-e2e",
            )
            assert installed.inventory.installed is True
            assert installed.inventory.enabled is True

            installed_detail = await bridge.read_plugin(
                plugin_name,
                marketplace_name=marketplace_name,
            )
            assert set(installed_detail.skills) == {
                "ksadk-bridge-e2e:bridge-audit",
                "ksadk-bridge-e2e:bridge-check",
            }
            assert installed_detail.mcp_servers == ("ksadk-fixture",)
            installed_skill_paths = tuple(
                codex_home.glob("plugins/cache/*/*/*/skills/*/SKILL.md")
            )
            assert len(installed_skill_paths) == 2
            installed_skills_by_name = {
                path.parent.name: path.resolve() for path in installed_skill_paths
            }
            skill_inputs: list[dict[str, str]] = []
            for skill_name in installed_detail.skills:
                skill_component = next(
                    component
                    for component in installed_detail.components
                    if component.kind == "skill" and component.name == skill_name
                )
                assert skill_component.path is not None
                skill_path = Path(skill_component.path)
                expected_source_skill = (
                    Path(marketplace)
                    / "plugins"
                    / plugin_name
                    / "skills"
                    / skill_name.rsplit(":", 1)[-1]
                    / "SKILL.md"
                )
                assert skill_path.resolve() == expected_source_skill.resolve()
                installed_skill = installed_skills_by_name[skill_name.rsplit(":", 1)[-1]]
                skill_inputs.append(
                    {"type": "skill", "name": skill_name, "path": str(installed_skill)}
                )

            installed_plugin_root = installed_skill_paths[0].parents[2]
            snapshot = snapshot_installed_codex_plugin(
                installed_plugin_root,
                source=CodexPluginSourceCoordinate(
                    type="local",
                    requested=str(Path(marketplace).resolve()),
                    resolved=str(installed_plugin_root.resolve()),
                    marketplace_name=marketplace_name,
                ),
            )
            assert snapshot.manifest.name == plugin_name
            assert {(component.kind, component.name) for component in snapshot.components} == {
                ("skill", "bridge-audit"),
                ("skill", "bridge-check"),
                ("mcp", "ksadk-fixture"),
            }

            thread = await client.thread_start(
                {
                    "cwd": str(workspace),
                    "model": "ksadk-codex-plugin-stub",
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": True,
                }
            )
            mcp_status = await client.request(
                "mcpServerStatus/list",
                {
                    "threadId": thread.thread.id,
                    "cursor": None,
                    "limit": 100,
                    "detail": "toolsAndAuthOnly",
                },
                response_model=_McpStatusList,
            )
            fixture_servers = [
                server for server in mcp_status.data if "echo_fixture" in server.tools
            ]
            assert fixture_servers, (
                json.dumps(mcp_status.model_dump(mode="json"), indent=2)
                + "\n"
                + client._sync._stderr_tail()  # noqa: SLF001 - real-host diagnostic
            )
            fixture_server = fixture_servers[0]
            called = await client.request(
                "mcpServer/tool/call",
                {
                    "threadId": thread.thread.id,
                    "server": fixture_server.name,
                    "tool": "echo_fixture",
                    "arguments": {"value": "app-server"},
                },
                response_model=_McpToolCall,
            )
            assert called.is_error is False
            assert called.structured_content == {"echo": "app-server"}
            assert called.content == ({"type": "text", "text": "plugin-echo:app-server"},)
            turn = await client.turn_start(
                thread.thread.id,
                [
                    {
                        "type": "text",
                        "text": " ".join(f"${name}" for name in installed_detail.skills)
                        + " Verify both installed bridge fixture skills.",
                    },
                    *skill_inputs,
                ],
            )
            completed = await asyncio.wait_for(
                client.wait_for_turn_completed(turn.turn.id),
                timeout=10,
            )
            assert completed.turn.status is TurnStatus.completed

            model_request = responses.single_request()
            skill_blocks = [
                text for text in model_request.input_texts("user") if text.startswith("<skill>")
            ]
            assert len(skill_blocks) == 2, json.dumps(model_request.payload, indent=2)
            combined_skills = "\n".join(skill_blocks)
            for item in skill_inputs:
                assert f"<name>{item['name']}</name>" in combined_skills
                assert f"<path>{item['path']}</path>" in combined_skills
            assert "Report that the KsADK Codex bridge fixture is available." in combined_skills
            assert "second KsADK Codex bridge skill was explicitly selected" in combined_skills

            removed = await bridge.uninstall_plugin(installed.inventory.plugin_id)
            assert removed.installed is False
            after = await bridge.read_plugin(plugin_name, marketplace_name=marketplace_name)
            assert after.inventory.installed is False
            assert not installed_skill_paths[0].exists()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("KSADK_CODEX_PLUGIN_E2E") != "1",
    reason="set KSADK_CODEX_PLUGIN_E2E=1 to exercise the real Codex App Server",
)
async def test_real_app_server_failed_install_restores_previous_inventory(
    tmp_path: Path,
) -> None:
    """A real install may commit before its response is lost; compensate it."""

    from openai_codex.async_client import AsyncCodexClient
    from openai_codex.client import CodexConfig

    marketplace = str(Path(__file__).parent / "fixtures" / "codex-marketplace")
    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    codex_home.mkdir()
    workspace.mkdir()
    client = AsyncCodexClient(
        CodexConfig(
            codex_bin=os.getenv("KSADK_CODEX_PLUGIN_E2E_BIN"),
            cwd=str(workspace),
            env={
                "CODEX_HOME": str(codex_home),
                "CODEX_APP_SERVER_DISABLE_MANAGED_CONFIG": "1",
                "RUST_LOG": "warn",
            },
        )
    )
    transport = _LoseFirstInstallReceipt(client)
    async with CodexAppServerPluginBridge(transport=transport) as bridge:
        marketplace_name = await bridge.add_marketplace(marketplace)
        before = await bridge.read_plugin(
            "ksadk-bridge-e2e",
            marketplace_name=marketplace_name,
        )

        with pytest.raises(CodexBridgeError, match="previous inventory was restored"):
            await bridge.install_plugin(
                "ksadk-bridge-e2e",
                marketplace_name=marketplace_name,
                accept_undeclared_permissions=True,
                install_attempt_id="real-lost-receipt",
            )

        after = await bridge.read_plugin(
            "ksadk-bridge-e2e",
            marketplace_name=marketplace_name,
        )
        assert transport.failed is True
        assert after.inventory == before.inventory
        assert not tuple(codex_home.glob("plugins/cache/*/*/*/skills/*/SKILL.md"))
