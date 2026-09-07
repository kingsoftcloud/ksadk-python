from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ksadk.kernel.contracts import InjectPayload, SteerPayload
from ksadk.runtime import (
    BaseRuntime,
    CancelResult,
    CheckpointCapability,
    CheckpointDescriptor,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    RuntimeLaunchContext,
    StartRequest,
)
from ksadk.studio.plugin_kernel_adapter import StudioPluginKernelAdapter
from ksadk.studio.run_service import StudioRunSpec


class _Runtime(BaseRuntime):
    runtime_type = "plugin-fixture"

    def native_capabilities(self) -> dict[str, Any]:
        return {}


class _Delegate(RuntimeAdapter):
    def __init__(self) -> None:
        super().__init__(_Runtime())
        self.calls: list[str] = []

    async def start(self, request: StartRequest) -> RunHandle:
        self.calls.append("start")
        return RunHandle(
            run_id="run-1",
            session_id=request.session_id,
            runtime_type="plugin-fixture",
        )

    async def _events(self):  # type: ignore[no-untyped-def]
        if False:  # pragma: no cover - defines one empty async iterator
            yield None

    def stream(self, handle: RunHandle):  # type: ignore[no-untyped-def]
        del handle
        return self._events()

    async def cancel(self, handle: RunHandle) -> CancelResult:
        del handle
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        del target, payload
        return handle

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        self.calls.append("checkpoint")
        return CheckpointDescriptor(
            checkpoint_id="checkpoint-1",
            invocation_id=handle.run_id,
            capability=CheckpointCapability(
                supported=True,
                granularity="snapshot",
                rollback_scope="turn",
                fork_supported=False,
                durable=False,
                shared_across_pods=False,
            ),
        )

    async def steer(self, handle: RunHandle, payload: SteerPayload) -> None:
        del handle, payload
        self.calls.append("steer")

    async def inject(self, handle: RunHandle, payload: InjectPayload) -> None:
        del handle, payload
        self.calls.append("inject")

    async def durable_restore(self, handle: RunHandle) -> RunHandle:
        self.calls.append("durable_restore")
        return handle

    def is_handle_attached(self, handle: RunHandle) -> bool:
        del handle
        return True

    async def close(self, handle: RunHandle) -> None:
        del handle
        self.calls.append("close")


class _PluginRuntime:
    def __init__(self, delegate: RuntimeAdapter) -> None:
        self.delegate = delegate

    async def kernel_adapter(self, spec: StudioRunSpec, *, session_id: str):  # type: ignore[no-untyped-def]
        del spec, session_id
        return self.delegate

    async def close_session_if_dynamic(self, spec: StudioRunSpec, session_id: str) -> None:
        del spec, session_id


def _spec(tmp_path: Path) -> StudioRunSpec:
    return StudioRunSpec(
        launch_context=RuntimeLaunchContext(
            runtime_type="plugin",
            project_dir=tmp_path,
        ),
        build_id="build-1",
        agent_id="agent-1",
        model="fixture-model",
        request_config={},
        manifest_sha256="sha256:fixture",
        plugin_bundle_root=tmp_path,
    )


@pytest.mark.asyncio
async def test_plugin_kernel_adapter_forwards_the_full_declared_control_surface(
    tmp_path: Path,
) -> None:
    delegate = _Delegate()
    adapter = StudioPluginKernelAdapter(_PluginRuntime(delegate), _spec(tmp_path))
    handle = await adapter.start(
        StartRequest(input="hello", user_id="user", session_id="session")
    )

    checkpoint = await adapter.checkpoint(handle)
    await adapter.steer(handle, SteerPayload(content="adjust"))
    await adapter.inject(handle, InjectPayload(context={"key": "value"}))
    restored = await adapter.durable_restore(handle)
    await adapter.close(handle)

    assert checkpoint.checkpoint_id == "checkpoint-1"
    assert restored == handle
    assert adapter.is_handle_attached(handle)
    assert delegate.calls == [
        "start",
        "checkpoint",
        "steer",
        "inject",
        "durable_restore",
        "close",
    ]
