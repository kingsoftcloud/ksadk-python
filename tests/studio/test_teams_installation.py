"""Recovery never relies on successful companion startup or existing DB state."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ksadk.plugins.teams.errors import TeamsError
from ksadk.studio.errors import StudioError
from ksadk.studio.plugin_lifecycle import lifecycle_failure
from ksadk.studio.teams_installation import StudioTeamsInstallation


def installation(tmp_path, monkeypatch):
    # Exercise the real installation methods without creating a Studio/DB/Core.
    result = StudioTeamsInstallation.__new__(StudioTeamsInstallation)
    result.available = True
    result.authority_ref = "fixture-authority"
    result._contributed = False
    result._configured_enabled = None
    result._stage = "registered"
    result._failure = None
    result.application = SimpleNamespace(
        runtime=SimpleNamespace(domain=None, last_error=None),
        start=AsyncMock(),
    )
    capabilities = SimpleNamespace(
        companion_manager=None,
        startup_status={},
        capability_snapshot=AsyncMock(side_effect=AssertionError("Must not boot Core")),
    )

    async def reconfigure(operation):
        return await operation()

    result.studio = SimpleNamespace(
        workspace=SimpleNamespace(root=tmp_path),
        start=AsyncMock(),
        dsh_capabilities=capabilities,
        reconfigure_dsh_profile=AsyncMock(side_effect=reconfigure),
    )
    from ksadk.studio import api_plugin_routes

    monkeypatch.setattr(
        api_plugin_routes,
        "_studio_dsh_options",
        lambda _: (tmp_path / "dsh-home", "web", ("/fixture/dsh",), "workspace"),
    )
    return result


@pytest.mark.asyncio
async def test_disable_reconciles_profile_after_startup_failed(tmp_path, monkeypatch):
    result = installation(tmp_path, monkeypatch)
    result._failure = lifecycle_failure(
        TeamsError("authority_in_use", "fixture"), "companion_start"
    )
    mutations = []
    monkeypatch.setattr(
        "ksadk.studio.teams_installation.configure_teams_profile",
        lambda **options: mutations.append(options["enabled"]),
    )
    assert result.status()["health"] == "failed"
    await result.disable()
    assert mutations == [False]
    assert result.status()["configuredEnabled"] is False
    assert result.status()["health"] == "disabled"
    assert result.status()["failure"] is None
    result.studio.dsh_capabilities.capability_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_activation_error_survives_status_read_without_transport_detail(
    tmp_path, monkeypatch
):
    result = installation(tmp_path, monkeypatch)
    result.application.start.side_effect = TeamsError(
        "artifact_migration_required", "private://credential", status=409
    )
    with pytest.raises(StudioError) as failed:
        await result._activate()
    assert failed.value.status_code == 409
    assert failed.value.details["stage"] == "companion_start"
    assert result.status()["configuredEnabled"] is True
    assert result.status()["failure"]["retryable"] is False
    assert "private://credential" not in str(result.status())
    assert "private://credential" not in str(failed.value.as_dict())


@pytest.mark.asyncio
async def test_profile_preflight_failure_is_retained(tmp_path, monkeypatch):
    result = installation(tmp_path, monkeypatch)
    result.available = False
    with pytest.raises(StudioError) as failed:
        await result.enable()
    assert failed.value.code == "authority_profile_unavailable"
    assert result.status()["failure"]["stage"] == "profile_preflight"
    assert result.status()["health"] == "failed"
