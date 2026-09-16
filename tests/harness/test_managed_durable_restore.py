"""Adapter 层跨实例 Handle 持久化与 durable_restore 契约。"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ksadk.harness.config import HarnessConfig
from ksadk.harness.engine.base import ExecutionEngineError
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.managed_runtime import ManagedHarnessRuntimeAdapter
from ksadk.harness.reasoner import (
    HarnessReasoningTurn,
    HarnessToolCall,
)
from ksadk.harness.runtime_server import DeploymentRunStore
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.plugins.providers.harness_managed import build_managed_provider_adapter
from ksadk.runtime import (
    CancelResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeLaunchContext,
    StartRequest,
)
from ksadk.studio.plugin_kernel_adapter import StudioPluginKernelAdapter
from ksadk.studio.run_service import StudioRunSpec


class _Reasoner:
    async def complete(self, **_kwargs):
        return HarnessReasoningTurn(final_text="ok")


def _config() -> HarnessConfig:
    return HarnessConfig(model="glm-5.2", prompt="restore assistant")


def _request() -> StartRequest:
    return StartRequest(
        input="hello",
        user_id="user",
        session_id="session-1",
        agent_id="agent-1",
        runtime_type="harness",
        metadata={"invocation_id": "inv-restore-1"},
    )


class _FakeEngine:
    """最小引擎替身：可设定 attach 是否存在未决 Checkpoint。"""

    def __init__(self, *, pending: bool, status: str = "awaiting_approval") -> None:
        self._pending = pending
        self._status = status
        self.attached: list[str] = []
        self.resumed: list[str] = []
        self.stream_events: list[RuntimeEvent] = []

    def is_handle_attached(self, handle) -> bool:  # noqa: ANN001
        return False

    async def compile(self, _spec):  # noqa: ANN001
        return object()

    async def start(self, request, _compiled):  # noqa: ANN001
        from ksadk.runtime import RunHandle

        return RunHandle(
            run_id=request.metadata["invocation_id"],
            runtime_type="harness",
            session_id=request.session_id,
            native_ref={"thread_id": f"thread-{request.metadata['invocation_id']}"},
        )

    async def attach(self, handle, _compiled):  # noqa: ANN001
        if not self._pending:
            raise ExecutionEngineError(
                f"attach 失败：thread {handle.native_ref.get('thread_id')!r} 无未决 Checkpoint"
            )
        self.attached.append(handle.run_id)
        return handle

    async def snapshot_state(self, _handle):  # noqa: ANN001, ANN202
        return SimpleNamespace(status=SimpleNamespace(value=self._status))

    async def resume(self, handle, _target, _payload):  # noqa: ANN001, ANN202
        self.resumed.append(handle.run_id)
        return handle

    async def cancel(self, _handle):  # noqa: ANN001, ANN202
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    async def stream(self, _handle):  # noqa: ANN001
        for event in self.stream_events:
            yield event


def _adapter(engine: _FakeEngine, state_dir: Path) -> ManagedHarnessRuntimeAdapter:
    from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec

    spec = HarnessSpec(
        agent_revision_ref="agent-revision://restore-agent@1",
        model=ModelBinding(profile_ref="model-profile://m@1"),
        prompt=PromptSpec(instructions="p"),
    )
    adapter = ManagedHarnessRuntimeAdapter(
        spec, reasoner=_Reasoner(), engine=engine, durable=True
    )
    adapter._run_store = DeploymentRunStore(state_dir)
    return adapter


@pytest.mark.asyncio
async def test_adapter_persists_run_handle_for_later_restore(tmp_path: Path) -> None:
    adapter = await build_managed_provider_adapter(
        _config(),
        agent_name="restore-agent",
        workspace_root=tmp_path,
        reasoner=_Reasoner(),
        state_dir=tmp_path / "state",
    )
    handle = await adapter.start(_request())

    payload = json.loads((tmp_path / "state" / "runs.json").read_text(encoding="utf-8"))
    assert handle.run_id in payload["handles"]
    assert payload["handles"][handle.run_id]["native_ref"].get("thread_id")
    assert payload["handles"][handle.run_id]["runtime_type"] == "harness"


@pytest.mark.asyncio
async def test_adapter_removes_durable_handle_after_cancellation(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    adapter = _adapter(_FakeEngine(pending=True), state_dir)
    handle = await adapter.start(_request())

    result = await adapter.cancel(handle)

    assert result is CancelResult.INTERRUPTED_ACTIVE_TURN
    payload = json.loads((state_dir / "runs.json").read_text(encoding="utf-8"))
    assert payload["runs"][handle.run_id]["status"] == "canceled"
    assert handle.run_id not in payload["handles"]


@pytest.mark.asyncio
async def test_adapter_removes_durable_handle_before_yielding_terminal_event(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    engine = _FakeEngine(pending=True)
    adapter = _adapter(engine, state_dir)
    handle = await adapter.start(_request())
    engine.stream_events.append(
        RuntimeEvent(
            event_id="terminal",
            timestamp=1.0,
            user_id="user",
            seq_id=1,
            event_type=EventType.RUN_COMPLETED,
            agent_id="agent-1",
            session_id=handle.session_id,
            invocation_id=handle.run_id,
            payload={"status": "completed"},
        )
    )

    stream = adapter.stream(handle)
    terminal = await anext(stream)

    assert terminal.event_type == "run.completed"
    payload = json.loads((state_dir / "runs.json").read_text(encoding="utf-8"))
    assert payload["runs"][handle.run_id]["status"] == "completed"
    assert handle.run_id not in payload["handles"]


@pytest.mark.asyncio
async def test_adapter_restores_pending_handle_in_new_instance(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    first = _adapter(_FakeEngine(pending=True), state_dir)
    handle = await first.start(_request())

    # 模拟进程重启：全新引擎实例 + 同一状态目录，且 Checkpoint 存在未决状态
    second = _adapter(_FakeEngine(pending=True), state_dir)
    restored = await second.durable_restore(handle)

    assert restored is not None
    assert restored.run_id == handle.run_id
    # 幂等：再次 restore 返回同一已附着的运行态
    again = await second.durable_restore(handle)
    assert again is not None and again.run_id == handle.run_id
    assert second._engine.attached.count(handle.run_id) == 1


@pytest.mark.asyncio
async def test_adapter_restore_rejects_settled_run(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    first = _adapter(_FakeEngine(pending=True), state_dir)
    handle = await first.start(_request())

    # 已完结的 run：attach 诚实报"无未决 Checkpoint"，不能伪造恢复成功。
    second = _adapter(_FakeEngine(pending=False), state_dir)
    with pytest.raises(ExecutionEngineError, match="无未决 Checkpoint"):
        await second.durable_restore(handle)


@pytest.mark.asyncio
async def test_adapter_cold_restore_continues_non_interactive_checkpoint(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    first = _adapter(_FakeEngine(pending=True), state_dir)
    handle = await first.start(_request())
    engine = _FakeEngine(pending=True, status="paused")
    second = _adapter(engine, state_dir)

    restored = await second.durable_restore(handle)

    assert restored == handle
    assert engine.attached == [handle.run_id]
    assert engine.resumed == [handle.run_id]


@pytest.mark.asyncio
async def test_memory_adapter_rejects_durable_restore(tmp_path: Path) -> None:
    from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
    from ksadk.kernel.errors import UnsupportedControlError

    adapter = ManagedHarnessRuntimeAdapter(
        HarnessSpec(
            agent_revision_ref="agent-revision://memory-agent@1",
            model=ModelBinding(profile_ref="model-profile://m@1"),
            prompt=PromptSpec(instructions="p"),
        ),
        reasoner=_Reasoner(),
        engine=_FakeEngine(pending=True),
    )
    with pytest.raises(UnsupportedControlError, match="no durable checkpoint"):
        await adapter.durable_restore(
            RunHandle(
                run_id="run-memory",
                session_id="session-1",
                runtime_type="harness",
                native_ref={"thread_id": "thread-memory"},
            )
        )


@pytest.mark.asyncio
async def test_studio_plugin_kernel_restores_managed_harness_handle(tmp_path: Path) -> None:
    """Exercise the real Studio proxy -> managed Harness restore contract."""

    state_dir = tmp_path / "state"
    first = _adapter(_FakeEngine(pending=True), state_dir)
    handle = await first.start(_request())
    second = _adapter(_FakeEngine(pending=True), state_dir)

    class _PluginRuntime:
        async def kernel_adapter(self, _spec, *, session_id):  # noqa: ANN001, ANN202
            assert session_id == handle.session_id
            return second

    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(
            runtime_type="harness",
            project_dir=tmp_path,
        ),
        build_id="build-harness",
        agent_id="agent-1",
        model="fixture-model",
        request_config={},
        manifest_sha256="sha256:fixture",
        plugin_bundle_root=tmp_path,
    )
    proxy = StudioPluginKernelAdapter(_PluginRuntime(), spec)

    assert proxy.capabilities().durable_restore.supported is True
    restored = await proxy.durable_restore(handle)

    assert restored.run_id == handle.run_id
    assert restored.session_id == handle.session_id
    assert restored.runtime_type == "harness"
    assert proxy._provider_handle(restored) == handle
    assert second._engine.attached == [handle.run_id]


@pytest.mark.asyncio
async def test_studio_proxy_cold_restores_real_sqlite_checkpoint(tmp_path: Path) -> None:
    """Cold-start a new adapter and finish an approval from SQLite state.

    This covers the real Studio proxy and ManagedLangGraphEngine rather than a
    fake ``attach`` implementation.  The second adapter shares only the
    checkpoint file and persisted platform handle with the first one.
    """

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    spec = HarnessSpec(
        agent_revision_ref="agent-revision://cold-restore@1",
        model=ModelBinding(profile_ref="model-profile://fixture@1"),
        prompt=PromptSpec(instructions="approval fixture"),
    )
    executed: list[str] = []

    async def dangerous(_arguments):  # noqa: ANN001, ANN202
        executed.append("dangerous")
        return "approved result"

    class _ScriptedReasoner:
        def __init__(self, turns):  # noqa: ANN001
            self.turns = list(turns)

        async def complete(self, **_kwargs):  # noqa: ANN003, ANN202
            return self.turns.pop(0)

    def proxy(adapter: ManagedHarnessRuntimeAdapter) -> StudioPluginKernelAdapter:
        class _PluginRuntime:
            async def kernel_adapter(self, _spec, *, session_id):  # noqa: ANN001, ANN202
                assert session_id == "cold-session"
                return adapter

        return StudioPluginKernelAdapter(
            _PluginRuntime(),
            StudioRunSpec(
                launch_context=RuntimeLaunchContext(
                    runtime_type="harness", project_dir=tmp_path
                ),
                build_id="build-cold-restore",
                agent_id="cold-agent",
                model="fixture-model",
                request_config={},
                manifest_sha256="sha256:cold-restore",
                plugin_bundle_root=tmp_path,
            ),
        )

    db_path = str(tmp_path / "checkpoints.sqlite")
    first_context = AsyncSqliteSaver.from_conn_string(db_path)
    first_saver = await first_context.__aenter__()
    try:
        first_engine = ManagedLangGraphEngine(
            reasoner=_ScriptedReasoner(
                [
                    HarnessReasoningTurn(
                        tool_calls=(
                            HarnessToolCall(
                                call_id="approval-1",
                                name="dangerous",
                                arguments={"confirmed": True},
                            ),
                        )
                    )
                ]
            ),
            checkpointer=first_saver,
            tools={"dangerous": dangerous},
            approval_required={"dangerous"},
        )
        first_adapter = ManagedHarnessRuntimeAdapter(
            spec, engine=first_engine, durable=True
        )
        first_proxy = proxy(first_adapter)
        handle = await first_proxy.start(
            StartRequest(
                input="run dangerous tool",
                user_id="user",
                session_id="cold-session",
                agent_id="cold-agent",
                runtime_type="harness",
            )
        )
        first_events = [event async for event in first_proxy.stream(handle)]
        assert first_events[-1].event_type == "run.interrupted"
        assert executed == []
    finally:
        with contextlib.suppress(Exception):
            await first_context.__aexit__(None, None, None)

    second_context = AsyncSqliteSaver.from_conn_string(db_path)
    second_saver = await second_context.__aenter__()
    try:
        second_engine = ManagedLangGraphEngine(
            reasoner=_ScriptedReasoner(
                [HarnessReasoningTurn(final_text="completed after restart")]
            ),
            checkpointer=second_saver,
            tools={"dangerous": dangerous},
            approval_required={"dangerous"},
        )
        second_adapter = ManagedHarnessRuntimeAdapter(
            spec, engine=second_engine, durable=True
        )
        second_proxy = proxy(second_adapter)
        restored = await second_proxy.durable_restore(handle)
        await second_proxy.resume(
            restored,
            ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
            ResumePayload(
                kind="approval_decision", call_id="approval-1", data="approved"
            ),
        )
        second_events = [event async for event in second_proxy.stream(restored)]
        assert second_events[-1].event_type == "run.completed"
        assert executed == ["dangerous"]
    finally:
        with contextlib.suppress(Exception):
            await second_context.__aexit__(None, None, None)
