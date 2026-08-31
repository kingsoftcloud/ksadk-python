"""RuntimeCapabilityMatrix/v1 诚实能力测试 (agent-kernel Task 5)。

规则:
- ``supported`` 必须对应 adapter 上**真实覆写**的方法(method.__func__ is not base)。
- unsupported 的动词必须 fail-closed 抛 :class:`UnsupportedControlError`
  (pause 是显式状态机,允许返回 ``PauseResult.NOT_SUPPORTED``),禁止空成功。
- steer 不可从 start/stream 推断:没有 runtime 暴露原生 mid-turn steer 通道。
- ``native_capabilities()`` 是 matrix 的单向投影,旧 dict 不反向覆盖 matrix。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

import pytest

from ksadk.events.canonical import RuntimeEvent
from ksadk.kernel.contracts import (
    RuntimeCapability,
    RuntimeCapabilityMatrix,
)
from ksadk.kernel.errors import UnsupportedControlError
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.adapter import (
    BaseRuntime,
    CancelResult,
    CheckpointDescriptor,
    PauseResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)
from ksadk.runtime.framework_adapters import (
    ADKRuntimeAdapter,
    LangGraphRuntimeAdapter,
)
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter

# --------------------------------------------------------------- fixtures


class _FakeRunner(BaseRunner):
    """最小 runner:默认无 checkpoint、无 durable attach seam。"""

    def __init__(
        self,
        *,
        checkpoint: bool = False,
        durable: bool = False,
        shared_across_pods: bool = False,
        attach_seam: bool = False,
    ) -> None:
        self._checkpoint = checkpoint
        self._durable = durable
        self._shared = shared_across_pods
        self._attach_seam = attach_seam

    async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
        return {}

    def load_agent(self) -> Any:
        return None

    def stream(self, input_data: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        async def _empty() -> AsyncIterator[dict[str, Any]]:
            return
            yield {}  # pragma: no cover - signature only

        return _empty()

    def describe_checkpoint_capability(self) -> dict[str, Any]:
        return {
            "Supported": self._checkpoint,
            "Backend": "fixture" if self._checkpoint else "none",
            "Granularity": "snapshot" if self._checkpoint else "none",
            "RollbackScope": "turn" if self._checkpoint else "none",
            "Durable": self._durable,
            "SharedAcrossPods": self._shared,
            "Reason": "" if self._checkpoint else "runner does not expose framework checkpoints",
        }

    attach_seam: bool = False


class _AttachableRunner(_FakeRunner):
    """额外暴露 attach_runtime_handle seam 的 runner。"""

    def attach_runtime_handle(self, handle: RunHandle) -> bool:
        return True


class _MinimalRuntime(BaseRuntime):
    runtime_type = "minimal"

    def native_capabilities(self) -> dict[str, Any]:
        return {}


class _MinimalAdapter(RuntimeAdapter):
    async def start(self, request: StartRequest) -> RunHandle:
        raise AssertionError("not used")

    def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        raise AssertionError("not used")

    async def cancel(self, handle: RunHandle) -> CancelResult:
        return CancelResult.NOT_RUNNING

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: Optional[ResumePayload],
    ) -> RunHandle:
        return handle

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        raise UnsupportedControlError("minimal adapter has no native checkpoint")

    def capabilities(self):
        # 与真实覆写一致:cancel/resume 已覆写且可用;其余诚实 unavailable。
        def unavailable(reason):
            return RuntimeCapability(supported=False, mode="unavailable", reason=reason)

        return RuntimeCapabilityMatrix(
            cancel=RuntimeCapability(supported=True, mode="native"),
            pause=unavailable("runtime_no_native_pause"),
            resume=RuntimeCapability(supported=True, mode="native"),
            submit_interaction=unavailable("runtime_no_live_interaction_channel"),
            attach=unavailable("not_implemented"),
            steer=unavailable("runtime_no_native_steer"),
            inject=unavailable("runtime_no_native_inject"),
            checkpoint=unavailable("runtime_no_native_checkpoint"),
            durable_restore=unavailable("durable_restore_requires_cross_process_checkpoint"),
        )

    async def close(self, handle: RunHandle) -> None:
        return None


def _codex_adapter() -> RuntimeAdapter:
    from ksadk.codex.runtime import CodexRuntimeAdapter

    return CodexRuntimeAdapter(_FakeCodexClient())  # type: ignore[arg-type]


class _FakeCodexClient:
    """CodexClient 替身:capability 查询不允许触碰 client。"""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"capabilities() must not touch the codex client: {name}")


def _all_adapters() -> list[tuple[str, RuntimeAdapter]]:
    return [
        ("minimal", _MinimalAdapter(_MinimalRuntime())),
        ("runner-default", RunnerRuntimeAdapter(_FakeRunner(), runtime_type="ksadk")),
        (
            "runner-checkpoint",
            RunnerRuntimeAdapter(
                _AttachableRunner(
                    checkpoint=True, durable=True, shared_across_pods=True
                ),
                runtime_type="langgraph",
            ),
        ),
        ("adk", ADKRuntimeAdapter(_FakeRunner())),
        ("langgraph", LangGraphRuntimeAdapter(_FakeRunner())),
        ("codex", _codex_adapter()),
    ]


def test_codex_capability_matrix_distinguishes_controls_from_internal_loop() -> None:
    """Goal/Plan 是原生控制；普通 agent loop 不能冒充可选的改进循环。"""

    matrix = _codex_adapter().capabilities()
    for mode_name in ("goal", "plan"):
        capability = getattr(matrix, mode_name)
        assert capability is not None, mode_name
        assert capability.supported is True, mode_name
        assert capability.mode == "native", mode_name
    assert matrix.loop is not None
    assert matrix.loop.supported is False
    assert matrix.loop.mode == "unavailable"
    assert matrix.loop.reason == "codex_loop_requires_run_control_spec"
    assert matrix.interaction_mode == "live_submit"


def test_interaction_mode_tracks_real_provider_delivery_path() -> None:
    """Provider delivery must not be inferred from the submit verb alone."""

    assert ADKRuntimeAdapter(_FakeRunner()).capabilities().interaction_mode == "unavailable"
    assert LangGraphRuntimeAdapter(_FakeRunner()).capabilities().interaction_mode == "unavailable"
    assert (
        LangGraphRuntimeAdapter(_FakeRunner(checkpoint=True)).capabilities().interaction_mode
        == "durable_resume"
    )


_CAPABILITY_METHODS = {
    "cancel": "cancel",
    "pause": "pause",
    "resume": "resume",
    "submit_interaction": "submit",
    "attach": "attach",
    "steer": "steer",
    "inject": "inject",
    "checkpoint": "checkpoint",
    "durable_restore": "durable_restore",
}


_RUN_SEQ = 0


def _handle(runtime_type: str = "fake") -> RunHandle:
    global _RUN_SEQ
    _RUN_SEQ += 1
    return RunHandle(
        run_id=f"run-{_RUN_SEQ}", session_id="session-1", runtime_type=runtime_type
    )


async def _invoke_verb(adapter: RuntimeAdapter, method_name: str) -> Any:
    runtime_type = str(getattr(adapter.runtime, "runtime_type", "fake"))
    handle = _handle(runtime_type)
    if method_name == "resume":
        # runner 系 adapter 需要已知 handle 才会走到 capability fail-closed 分支。
        known = getattr(adapter, "_known_runs", None)
        if isinstance(known, set):
            known.add(handle.run_id)
            sessions = getattr(adapter, "_run_sessions", None)
            if isinstance(sessions, dict):
                sessions[handle.run_id] = handle.session_id
        return await adapter.resume(
            handle,
            ResumeTarget(kind="invocation_id", id=handle.run_id),
            None,
        )
    if method_name == "submit":
        return await adapter.submit(handle, ResumePayload(kind="free_text"))
    if method_name in {"steer", "inject"}:
        return await getattr(adapter, method_name)(handle, {"text": "more"})
    return await getattr(adapter, method_name)(handle)


# --------------------------------------------------------------- base rules


def test_matrix_contract_type_and_defaults() -> None:
    # 直接调用基类默认实现,校验未覆写 capabilities 的 adapter 的诚实默认值。
    adapter = _MinimalAdapter(_MinimalRuntime())
    matrix = RuntimeAdapter.capabilities(adapter)
    assert isinstance(matrix, RuntimeCapabilityMatrix)
    assert matrix.schema_version == 1
    for name in _CAPABILITY_METHODS:
        capability = getattr(matrix, name)
        assert capability.supported is False, name
        assert capability.mode == "unavailable", name
        expected_reason = "not_implemented"
        if name == "steer":
            expected_reason = "runtime_no_native_steer"
        elif name == "inject":
            expected_reason = "runtime_no_native_inject"
        assert capability.reason == expected_reason, name


@pytest.mark.parametrize("runtime_name,adapter", _all_adapters())
@pytest.mark.asyncio
async def test_steer_is_not_inferred_from_start(
    runtime_name: str, adapter: RuntimeAdapter
) -> None:
    matrix = adapter.capabilities()
    assert matrix.steer.supported is False, runtime_name
    assert matrix.steer.mode == "unavailable", runtime_name
    assert matrix.steer.reason == "runtime_no_native_steer", runtime_name
    with pytest.raises(UnsupportedControlError):
        await _invoke_verb(adapter, "steer")


@pytest.mark.parametrize("runtime_name,adapter", _all_adapters())
@pytest.mark.asyncio
async def test_capability_supported_requires_real_override(
    runtime_name: str, adapter: RuntimeAdapter
) -> None:
    matrix = adapter.capabilities()
    for capability_name, method_name in _CAPABILITY_METHODS.items():
        capability = getattr(matrix, capability_name)
        method = getattr(adapter, method_name)
        if capability.supported:
            base = getattr(RuntimeAdapter, method_name)
            assert method.__func__ is not base, (runtime_name, capability_name)
        else:
            assert capability.reason, (runtime_name, capability_name)
            # pause 是显式状态机动词:允许返回 NOT_SUPPORTED,其余必须 fail-closed。
            if capability_name == "pause":
                result = await adapter.pause(_handle())
                assert result is PauseResult.NOT_SUPPORTED, runtime_name
                continue
            with pytest.raises(UnsupportedControlError):
                await _invoke_verb(adapter, method_name)


# --------------------------------------------------------------- per-runtime honest matrices


@pytest.mark.asyncio
async def test_runner_adapter_default_matrix_is_honest() -> None:
    adapter = RunnerRuntimeAdapter(_FakeRunner(), runtime_type="ksadk")
    matrix = adapter.capabilities()
    assert matrix.cancel.supported is True
    assert matrix.cancel.mode == "emulated"
    # 无 checkpoint 的 runner:resume/checkpoint 必须 fail-closed。
    assert matrix.resume.supported is False
    assert matrix.resume.reason == "runtime_no_native_checkpoint"
    assert matrix.checkpoint.supported is False
    assert matrix.attach.supported is False
    assert matrix.attach.reason == "runner_no_durable_attach_seam"
    assert matrix.durable_restore.supported is False
    assert matrix.durable_restore.reason == "durable_restore_requires_cross_process_checkpoint"
    handle = _handle("ksadk")
    adapter._known_runs.add(handle.run_id)  # noqa: SLF001
    adapter._run_sessions[handle.run_id] = handle.session_id  # noqa: SLF001
    with pytest.raises(UnsupportedControlError):
        await adapter.resume(handle, ResumeTarget(kind="invocation_id", id="run-1"), None)


@pytest.mark.asyncio
async def test_runner_adapter_checkpoint_runner_matrix() -> None:
    adapter = RunnerRuntimeAdapter(
        _AttachableRunner(checkpoint=True, durable=True, shared_across_pods=True),
        runtime_type="langgraph",
    )
    matrix = adapter.capabilities()
    assert matrix.resume.supported is True
    assert matrix.checkpoint.supported is True
    assert matrix.attach.supported is True
    assert matrix.durable_restore.supported is True
    assert matrix.pause.supported is False
    assert matrix.submit_interaction.supported is False


@pytest.mark.asyncio
async def test_runner_adapter_in_memory_checkpoint_is_not_durable() -> None:
    # durable=False 的 checkpoint 不允许声明 durable_restore。
    adapter = RunnerRuntimeAdapter(
        _AttachableRunner(checkpoint=True, durable=False),
        runtime_type="langgraph",
    )
    matrix = adapter.capabilities()
    assert matrix.checkpoint.supported is True
    assert matrix.durable_restore.supported is False
    assert matrix.durable_restore.reason == "durable_restore_requires_cross_process_checkpoint"


def test_codex_matrix_is_honest() -> None:
    adapter = _codex_adapter()
    matrix = adapter.capabilities()
    assert matrix.cancel.supported is True and matrix.cancel.mode == "native"
    assert matrix.pause.supported is True and matrix.pause.mode == "native"
    assert matrix.resume.supported is True and matrix.resume.mode == "native"
    assert matrix.submit_interaction.supported is True
    assert matrix.checkpoint.supported is True and matrix.checkpoint.mode == "native"
    # Codex adapter 没有 attach 实现:attach/durable_restore 必须诚实 unavailable。
    assert matrix.attach.supported is False
    assert matrix.attach.reason == "codex_process_local_thread_table"
    assert matrix.durable_restore.supported is False
    assert matrix.inject.supported is False
    assert matrix.inject.reason == "runtime_no_native_inject"


@pytest.mark.asyncio
async def test_codex_unsupported_verbs_fail_closed() -> None:
    adapter = _codex_adapter()
    with pytest.raises(UnsupportedControlError):
        await adapter.attach(_handle("codex"))
    with pytest.raises(UnsupportedControlError):
        await adapter.steer(_handle("codex"), {"text": "x"})
    with pytest.raises(UnsupportedControlError):
        await adapter.inject(_handle("codex"), {"text": "x"})


# --------------------------------------------------------------- legacy projection


@pytest.mark.parametrize("runtime_name,adapter", _all_adapters())
def test_native_capabilities_is_one_way_projection(
    runtime_name: str,
    adapter: RuntimeAdapter,
) -> None:
    matrix = adapter.capabilities()
    legacy = adapter.native_capabilities()
    assert set(legacy) == set(_CAPABILITY_METHODS), runtime_name
    for name in _CAPABILITY_METHODS:
        assert legacy[name] is getattr(matrix, name).supported, (runtime_name, name)


def test_projection_cannot_override_matrix() -> None:
    adapter = _MinimalAdapter(_MinimalRuntime())
    legacy = adapter.native_capabilities()
    assert isinstance(legacy, dict)
    # 写旧 dict 不影响 matrix(单向)。
    legacy["steer"] = True
    assert adapter.capabilities().steer.supported is False
