"""Factory 驱动的 RuntimeRegistry 合同。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import ksadk.runtime as runtime_api
from ksadk.events.runtime_event import RuntimeEvent
from ksadk.runtime import (
    BaseRuntime,
    CancelResult,
    CheckpointCapability,
    CheckpointDescriptor,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    RuntimeRegistry,
    StartRequest,
)


class _FixtureRuntime(BaseRuntime):
    runtime_type = "fixture"

    def native_capabilities(self) -> dict[str, object]:
        return {}


class _FixtureAdapter(RuntimeAdapter):
    async def start(self, request: StartRequest) -> RunHandle:
        return RunHandle(
            run_id="run-1",
            session_id=request.session_id,
            runtime_type="fixture",
        )

    async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        if False:
            yield

    async def cancel(self, handle: RunHandle) -> CancelResult:
        return CancelResult.NOT_RUNNING

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        return handle

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        return CheckpointDescriptor(
            checkpoint_id="checkpoint-1",
            invocation_id=handle.run_id,
            capability=CheckpointCapability(
                supported=False,
                granularity="none",
                rollback_scope="none",
                fork_supported=False,
                durable=False,
                shared_across_pods=False,
            ),
        )

    async def close(self, handle: RunHandle) -> None:
        return None


def test_registry_builds_adapter_from_launch_context(tmp_path: Path) -> None:
    """防止调用方重新负责框架分支和 Runner 构造。"""

    received: list[runtime_api.RuntimeLaunchContext] = []

    def factory(context: runtime_api.RuntimeLaunchContext) -> RuntimeAdapter:
        received.append(context)
        return _FixtureAdapter(_FixtureRuntime())

    services = runtime_api.RuntimeServices()
    context = runtime_api.RuntimeLaunchContext(
        runtime_type="fixture",
        project_dir=tmp_path,
        config={"model": "model-a"},
        services=services,
    )
    registry = RuntimeRegistry()
    registry.register("fixture", factory)

    adapter = registry.create(context)

    assert isinstance(adapter, _FixtureAdapter)
    assert received == [context]
    assert context.project_dir == tmp_path
    assert context.services is services
    assert registry.get("FIXTURE") is factory
    assert registry.registered_types() == ["fixture"]


def test_registry_rejects_duplicate_normalized_runtime_type(tmp_path: Path) -> None:
    """防止后注册的实现静默覆盖已冻结的 Runtime Factory。"""

    registry = RuntimeRegistry()

    def factory(_context):
        return _FixtureAdapter(_FixtureRuntime())

    registry.register("fixture", factory)

    with pytest.raises(ValueError, match="duplicate"):
        registry.register(" FIXTURE ", factory)


def test_launch_context_freezes_configuration_snapshot(tmp_path: Path) -> None:
    """防止调用方在 Adapter 创建后悄悄改变同一份启动配置。"""

    source = {"model": "model-a"}
    context = runtime_api.RuntimeLaunchContext(
        runtime_type="fixture",
        project_dir=tmp_path,
        config=source,
    )
    source["model"] = "model-b"

    assert context.config["model"] == "model-a"
    with pytest.raises(TypeError):
        context.config["model"] = "model-c"


@pytest.mark.parametrize("runtime_type", ["", "   "])
def test_registry_rejects_blank_runtime_type(runtime_type: str) -> None:
    registry = RuntimeRegistry()

    with pytest.raises(ValueError, match="runtime type"):
        registry.register(runtime_type, lambda _context: _FixtureAdapter(_FixtureRuntime()))


def test_registry_rejects_factory_returning_non_adapter(tmp_path: Path) -> None:
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _context: object())

    with pytest.raises(TypeError, match="RuntimeAdapter"):
        registry.create(
            runtime_api.RuntimeLaunchContext(runtime_type="fixture", project_dir=tmp_path)
        )


def test_registry_reports_missing_runtime_type(tmp_path: Path) -> None:
    registry = RuntimeRegistry()

    with pytest.raises(KeyError, match="missing"):
        registry.create(
            runtime_api.RuntimeLaunchContext(runtime_type="missing", project_dir=tmp_path)
        )
