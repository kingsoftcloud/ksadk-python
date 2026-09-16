from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ksadk.plugins.dsh_teams import TEAMS_PLUGIN_ID
from ksadk.studio.errors import StudioError
from ksadk.studio.teams_installation import StudioTeamsInstallation
from ksadk.studio.workspace import Workspace
from ksadk.studio.workspace_plugins import WorkspacePluginRegistry


def installation_for(workspace):
    return StudioTeamsInstallation(SimpleNamespace(
        workspace=Workspace(workspace), execution_host=object(),
        builds=SimpleNamespace(list=lambda: []),
        workspace_plugins=WorkspacePluginRegistry(),
        dsh_capabilities=SimpleNamespace(
            configure_companions=lambda definitions: None,
            companion_manager=SimpleNamespace(active_plugins=set()),
        ),
    ))


@pytest.mark.asyncio
async def test_failed_default_activation_is_visible_and_retry_clears_failure(tmp_path):
    installation = installation_for(tmp_path)

    async def configure(enabled):
        assert installation.status()["health"] == "preparing"
        raise StudioError("DSH_CAPABILITY_HOST_UNAVAILABLE", "fixture host unavailable", status_code=503)

    installation._configure = configure
    with pytest.raises(StudioError):
        await installation.enable()
    assert installation.status()["health"] == "error"
    assert installation.status()["reason"] == "DSH_CAPABILITY_HOST_UNAVAILABLE"

    async def recover(enabled):
        assert installation.status()["reason"] is None
        installation.application.runtime.domain = object()
        installation._contributed = True
        installation.studio.dsh_capabilities.companion_manager.active_plugins.add(TEAMS_PLUGIN_ID)

    installation._configure = recover
    await installation.enable()
    assert installation.status()["enabled"] is True
    assert installation.status()["health"] == "ready"


@pytest.mark.asyncio
async def test_another_studio_owning_the_team_store_is_identified(tmp_path):
    installation = installation_for(tmp_path)

    async def configure(enabled):
        raise StudioError("DSH_CAPABILITY_HOST_UNAVAILABLE", "fixture", status_code=503,
                          details={"reason": "authority_in_use"})

    installation._configure = configure
    with pytest.raises(StudioError):
        await installation.enable()
    assert installation.status()["reason"] == "authority_in_use"


@pytest.mark.asyncio
async def test_background_and_manual_enable_share_one_activation(tmp_path):
    installation = installation_for(tmp_path)
    calls = []

    async def configure(enabled):
        calls.append(enabled)
        await asyncio.sleep(0)
        installation.application.runtime.domain = object()
        installation._contributed = True
        installation.studio.dsh_capabilities.companion_manager.active_plugins.add(TEAMS_PLUGIN_ID)

    installation._configure = configure
    await asyncio.gather(installation.enable(), installation.enable())
    assert calls == [True]
