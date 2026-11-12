"""A speculative UI warmup has no authority over the next Run's claims."""

import pytest

from ksadk.plugins.host import PluginHost
from ksadk.studio.contracts import RunStatus
from ksadk.studio.runtime_prewarm import prewarm_runtime_build
from ksadk.studio.service import StudioService
from tests.studio.native_control_fixture import ControlledGateway
from tests.studio.test_dsh_provider_registration import _managed_profile, _spec


@pytest.mark.asyncio
async def test_host_warmup_does_not_activate_session_before_actual_run_settings(
    tmp_path, monkeypatch
):
    _managed_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("MODEL_API_KEY", "fixture-key")
    model = ControlledGateway()
    model.release.set()
    studio = StudioService(
        tmp_path / "workspace", model_client=model, native_harness_agent_ids=("agent",)
    )
    calls = []
    original = PluginHost.open_activation

    async def observed(self, *args, **kwargs):
        calls.append(kwargs.get("activation_claims"))
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(PluginHost, "open_activation", observed)
    try:
        studio.create_agent(agent_id="agent", name="Warmup fixture", spec=_spec())
        build = await studio.ensure_current_build("agent")
        await prewarm_runtime_build(studio, build.id)
        assert studio.plugin_runs._hosts  # Host really prepared; not a no-op warmup.
        assert calls == [] and model.requests == []
        result = await studio.run_build(
            build.id, "ACK_NEXT_TURN", "session", approval_mode="never", reasoning_effort="low"
        )
        assert result.status == RunStatus.COMPLETED, result.error
        assert result.output == "ACK_NEXT_TURN"
        assert len(calls) == 1 and len(model.requests) == 1
    finally:
        await studio.aclose()
