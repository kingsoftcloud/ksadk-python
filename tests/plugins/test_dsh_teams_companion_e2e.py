"""Real official Core + six Cordis bundles + trusted Python companion IPC."""

from __future__ import annotations

import asyncio
import os
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest

from ksadk.plugins.companions import CompanionError
from ksadk.plugins.dsh_home import studio_dsh_home
from ksadk.plugins.dsh_teams import (
    TEAMS_OPERATIONS,
    configure_teams_profile,
    teams_companion_definition,
)
from ksadk.plugins.dsh_toolchain import DshToolchainManager
from ksadk.studio.dsh_capability_service import StudioDshCapabilityService
from ksadk.studio.dsh_models import DshStudioModels
from ksadk.studio.dsh_provider_registration import StudioDshProviderRegistrationManager

pytestmark = pytest.mark.skipif(
    os.environ.get("KSADK_DSH_TOOLCHAIN_E2E") != "1",
    reason="set KSADK_DSH_TOOLCHAIN_E2E=1 for isolated official Core installation",
)


@pytest.mark.asyncio
async def test_teams_graph_real_tools_and_companion_lifecycle(tmp_path, monkeypatch) -> None:
    toolchains = tmp_path / "toolchains"
    toolchain = DshToolchainManager(base_dir=toolchains)
    toolchain.install()
    monkeypatch.setenv("AGENTENGINE_PLUGIN_TOOLCHAIN_HOME", str(toolchains))
    for key in ("KSADK_DSH_HOME", "KSADK_DSH_PROFILE", "KSADK_DSH_BIN"):
        monkeypatch.delenv(key, raising=False)
    workspace = tmp_path / "studio"
    workspace.mkdir()
    registration = StudioDshProviderRegistrationManager.discover_or_create_workspace_default(
        workspace
    )
    assert registration is not None
    events = []
    bound_artifacts = []
    trusted = object()

    class Application:
        def bind_artifact(self, artifact):
            assert artifact.plugin_id == "io.ksadk.teams"
            assert len(artifact.components) == 6
            assert all(item.version == "0.1.0" for item in artifact.components)
            assert all(item.source_digest for item in artifact.components)
            bound_artifacts.append(artifact)

        async def start(self):
            assert bound_artifacts
            events.append("start")

        async def revoke(self):
            events.append("revoke")

        async def close(self):
            events.append("close")

        async def invoke(self, principal, operation, arguments, call_id):
            assert principal is trusted
            assert operation == "team_context"
            assert arguments == {}
            events.append("invoke")
            return {"callId": call_id, "value": "actual Python companion"}

    capability = StudioDshCapabilityService.discover_or_create_workspace_default(workspace)
    capability.configure_companions([teams_companion_definition(Application())])
    model_credential = "fixture-only-model-credential-not-valid"
    capability.model_projection = lambda: DshStudioModels(
        providers={"privacy-fixture": {
            "displayName": "Privacy fixture", "baseURL": "http://127.0.0.1:1/v1",
            "api": "openai-completions", "apiKeyEnv": "KSADK_STUDIO_MODEL_FIXTURE",
            "models": [{"id": "fixture-model", "name": "Fixture"}],
        }},
        environment={"KSADK_STUDIO_MODEL_FIXTURE": model_credential},
        default_provider="privacy-fixture", default_model="fixture-model",
    )

    async def client_boot_has_teams() -> bool:
        lease = await capability.connector_lease()
        async with httpx.AsyncClient(trust_env=False, follow_redirects=True) as browser:
            response = await browser.get(lease.browser_url())
            assert response.status_code == 200
            assert "__DSH_BOOT__" in response.text
            manager = capability.companion_manager
            if manager is not None:
                configuration = manager.configuration
                handle = manager.issue_invocation(
                    "io.ksadk.teams",
                    trusted,
                    operations=frozenset({"team_context"}),
                )
                sensitive = [
                    configuration["secret"], configuration["socketPath"], handle,
                    lease._bearer_token, model_credential,
                ]
                documents = [
                    response.text,
                    capability._host._overlay_text(),
                    capability._host.descriptor.model_dump_json(),
                ]

                class Scripts(HTMLParser):
                    def __init__(self):
                        super().__init__()
                        self.urls = set()

                    def handle_starttag(self, tag, attrs):
                        attributes = dict(attrs)
                        url = (
                            attributes.get("src")
                            if tag == "script"
                            else (
                                attributes.get("href")
                                if tag == "link"
                                and (
                                    attributes.get("as") == "script"
                                    or attributes.get("rel") == "modulepreload"
                                )
                                else None
                            )
                        )
                        if url and url.startswith("/") and not url.startswith("//"):
                            self.urls.add(url)

                scripts = Scripts()
                scripts.feed(response.text)
                assert scripts.urls
                assert any(url.startswith("/plugins/??") for url in scripts.urls)
                for url in scripts.urls:
                    if url.startswith("/static/"):
                        # The parent Studio origin owns its built UI assets;
                        # only DSH /plugins/ client modules come from Core.
                        static = Path(__file__).parents[2] / "ksadk" / "studio" / "static"
                        script = static / url.removeprefix("/static/")
                        assert script.is_file()
                        documents.append(script.read_text(encoding="utf-8"))
                        continue
                    module = await browser.get(response.url.join(url))
                    assert module.status_code == 200, module.url.path
                    documents.append(module.text)
                manager.revoke_invocation(handle)
                if any(value in document for value in sensitive for document in documents):
                    pytest.fail("Private companion material appeared in a public Core response")
            return "@kingsoftcloud/dsh-teams-client" in response.text

    try:
        # Initialize only the real Profile; Teams is explicitly enabled later.
        await registration.bootstrap_official_codex_provider()
        assert events == []
        configure_teams_profile(
            workspace=workspace,
            dsh_home=studio_dsh_home(workspace),
            dsh_command=toolchain.require_command(),
            enabled=True,
        )
        assert events == []
        snapshot = await capability.capability_snapshot()
        assert events == ["start"]
        assert TEAMS_OPERATIONS <= {tool.name for tool in snapshot.tools}
        assert await client_boot_has_teams()
        result = await capability.call_companion_tool(
            "io.ksadk.teams",
            trusted,
            "team_context",
            {},
            call_id="native-call-1",
        )
        assert result == {"callId": "native-call-1", "value": "actual Python companion"}
        # A Core master token does not supply a member principal to tools.
        denied = await capability.call_tool(
            call_id="untrusted",
            tool_name="team_context",
            arguments={},
            deadline_ms=5000,
        )
        assert denied["isError"] is True
        assert events == ["start", "invoke"]
        old_manager = capability.companion_manager
        # A crashed Core loses its live component leases even without an API
        # management mutation; authority must stop before the next request.
        capability._host._process.kill()
        for _ in range(200):
            if events[-2:] == ["revoke", "close"]:
                break
            await asyncio.sleep(0.01)
        assert events[-2:] == ["revoke", "close"]
        assert old_manager.active_plugins == frozenset()
        await capability.capability_snapshot()
        assert events[-1] == "start"
        assert len(bound_artifacts) == 2
        assert bound_artifacts[0].plugin_digest == bound_artifacts[1].plugin_digest
        assert capability.companion_manager is not old_manager
        await capability.refresh()
        configure_teams_profile(
            workspace=workspace,
            dsh_home=studio_dsh_home(workspace),
            dsh_command=toolchain.require_command(),
            enabled=False,
        )
        await capability.capability_snapshot()
        assert capability.companion_manager is None
        assert not await client_boot_has_teams()
        assert events[-2:] == ["revoke", "close"]
        with pytest.raises(CompanionError):
            await capability.call_companion_tool(
                "io.ksadk.teams",
                trusted,
                "team_context",
                {},
                call_id="after-disable",
            )
    finally:
        await capability.aclose()
        await registration.aclose()


def test_resource_profile_cannot_own_teams_authority(tmp_path: Path) -> None:
    service = StudioDshCapabilityService.create_workspace_resource_default(tmp_path)

    class Application:
        start = revoke = close = invoke = lambda *args: None

    with pytest.raises(CompanionError, match="COMPANION_PROFILE_MISMATCH"):
        service.configure_companions([teams_companion_definition(Application())])
