import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ksadk.harness.config import HarnessConfig
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.plugins.providers.harness_managed import build_managed_provider_adapter
from ksadk.runtime import RuntimeExecutor, RuntimeLaunchContext, RuntimeRegistry
from ksadk.studio.contracts import RunStatus
from ksadk.studio.provider_recovery import detach_recovered_runs
from ksadk.studio.run_service import StudioRunService, StudioRunSpec
from ksadk.studio.workspace import Workspace


class Reasoner:
    def __init__(self, turn=0):
        self.turn = turn

    async def complete(self, **kwargs):
        self.turn += 1
        if self.turn == 1:
            return HarnessReasoningTurn(tool_calls=(HarnessToolCall(
                call_id="write", name="write_workspace_file",
                arguments={"path": "recovery.txt", "content": "recovered"},
            ),))
        return HarnessReasoningTurn(final_text="recovery completed")


async def wait_for(predicate):
    async with asyncio.timeout(15):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject", "cancel"])
async def test_studio_recreates_provider_and_resumes_pending_approval(tmp_path, decision):
    workspace = Workspace(tmp_path)
    workspace.initialize()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="harness", project_dir=bundle),
        build_id="build", agent_id="agent", manifest_sha256="locked",
        plugin_bundle_root=bundle, request_config={"provider_runtime_adapter": True},
    )

    async def make_service(turn):
        adapter = await build_managed_provider_adapter(
            HarnessConfig(model="fixture", prompt="write"), agent_name="agent",
            workspace_root=bundle, bundle_root=bundle, state_dir=tmp_path / "state",
            reasoner=Reasoner(turn), tool_contracts={"capabilities": {"tools": [{
                "name": "write_workspace_file", "executor": "builtin", "approval": "always",
            }]}},
        )
        service = StudioRunService(workspace, RuntimeExecutor(RuntimeRegistry()))
        service._capture_pcm_evidence = AsyncMock(return_value=None)
        service.plugin_runtime = SimpleNamespace(kernel_adapter_provider=lambda _: lambda: adapter)
        return service

    first = await make_service(0)
    task = asyncio.create_task(first.run(spec, "write test file", session_id="session"))
    await wait_for(lambda: first.event_store.list_runs() and
                   first.event_store.list_runs()[0].status == RunStatus.WAITING_INPUT)
    run = first.event_store.list_runs()[0]
    await wait_for(lambda: first._waiting_modes.get(run.id) == "resume")
    first._detaching_recoveries = True
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert first.event_store.get(run.id).status == RunStatus.WAITING_INPUT
    assert not list((tmp_path / "state").rglob("recovery.txt"))

    second = await make_service(1)
    await second.recover_interrupted(lambda *args, **kwargs: spec)
    await wait_for(lambda: second._waiting_modes.get(run.id) == "resume")
    interaction = next(event for event in second.event_store.events(run.id)
                       if event.type == "approval.requested")
    async def submit():
        return await second.submit_interaction(
            run.id, interaction.data["approvalId"], name=decision, data={"decision": decision},
            expected_revision=1, idempotency_key="approve-recovered",
        )

    submitted, duplicate = await asyncio.gather(submit(), submit())
    assert submitted == duplicate
    await wait_for(lambda: second.event_store.get(run.id).status == RunStatus.COMPLETED)
    result = second.event_store.get(run.id)
    assert result.output == "recovery completed"
    files = list((tmp_path / "state").rglob("recovery.txt"))
    if decision == "approve":
        assert len(files) == 1 and files[0].read_text() == "recovered"
    else:
        assert not files
    assert len(second.event_store.list_runs()) == 1
    assert not list(bundle.iterdir())
    await detach_recovered_runs(second)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch", ["agent", "manifest", "bundle", "run", "session", "capability"]
)
async def test_recovery_rejects_changed_build_or_handle_before_attach(tmp_path, mismatch):
    from ksadk.studio.provider_recovery import restore_waiting_provider_run

    record = SimpleNamespace(
        id="run", agent_id="agent", session_id="session", model="fixture", build_id="build",
        manifest_sha256="locked", runtime_handle={
            "run_id": "run", "session_id": "session", "runtime_type": "harness",
        },
    )
    spec = SimpleNamespace(agent_id="agent", manifest_sha256="locked", plugin_bundle_root=tmp_path)
    if mismatch == "agent":
        spec.agent_id = "other"
    elif mismatch == "manifest":
        spec.manifest_sha256 = "changed"
    elif mismatch == "bundle":
        spec.plugin_bundle_root = None
    elif mismatch == "run":
        record.runtime_handle["run_id"] = "other"
    elif mismatch == "session":
        record.runtime_handle["session_id"] = "other"
    adapter = SimpleNamespace(capabilities=lambda: SimpleNamespace(
        attach=SimpleNamespace(supported=True),
        durable_restore=SimpleNamespace(supported=mismatch != "capability"),
    ))
    service = SimpleNamespace(
        plugin_runtime=SimpleNamespace(kernel_adapter_provider=lambda _: lambda: adapter),
        executor=SimpleNamespace(attach=AsyncMock()),
    )
    assert not await restore_waiting_provider_run(service, record, lambda *a, **k: spec)
    service.executor.attach.assert_not_called()
