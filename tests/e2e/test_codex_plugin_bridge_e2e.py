from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from ksadk.plugins.bridges.codex import CodexAppServerPluginBridge, CodexBridgeError
from tests.e2e.codex_responses_stub import DeterministicResponsesStub


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
            marketplace_name = await bridge.add_marketplace(marketplace)
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
            assert installed_detail.skills == ("ksadk-bridge-e2e:bridge-check",)
            skill_name = installed_detail.skills[0]
            installed_skill_paths = tuple(
                codex_home.glob(
                    "plugins/cache/*/*/*/skills/bridge-check/SKILL.md"
                )
            )
            assert len(installed_skill_paths) == 1
            skill_path = str(installed_skill_paths[0].resolve())

            thread = await client.thread_start(
                {
                    "cwd": str(workspace),
                    "model": "ksadk-codex-plugin-stub",
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": True,
                }
            )
            turn = await client.turn_start(
                thread.thread.id,
                [
                    {
                        "type": "text",
                        "text": f"${skill_name} Verify the installed bridge fixture.",
                    },
                    {
                        "type": "skill",
                        "name": skill_name,
                        "path": skill_path,
                    },
                ],
            )
            completed = await asyncio.wait_for(
                client.wait_for_turn_completed(turn.turn.id),
                timeout=10,
            )
            assert completed.turn.status is TurnStatus.completed

            model_request = responses.single_request()
            skill_blocks = [
                text
                for text in model_request.input_texts("user")
                if text.startswith("<skill>")
            ]
            assert len(skill_blocks) == 1
            assert f"<name>{skill_name}</name>" in skill_blocks[0]
            assert f"<path>{skill_path}</path>" in skill_blocks[0]
            assert "Report that the KsADK Codex bridge fixture is available." in skill_blocks[0]

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
        assert not tuple(codex_home.glob("plugins/cache/*/*/*/skills/bridge-check/SKILL.md"))
