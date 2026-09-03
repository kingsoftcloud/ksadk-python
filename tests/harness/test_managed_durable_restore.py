"""Adapter 层跨实例 Handle 持久化与 durable_restore 契约。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ksadk.harness.config import HarnessConfig
from ksadk.harness.engine.base import ExecutionEngineError
from ksadk.harness.managed_runtime import ManagedHarnessRuntimeAdapter
from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.harness.runtime_server import DeploymentRunStore
from ksadk.plugins.providers.harness_managed import build_managed_provider_adapter
from ksadk.runtime import StartRequest


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

    def __init__(self, *, pending: bool) -> None:
        self._pending = pending
        self.attached: list[str] = []

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


def _adapter(engine: _FakeEngine, state_dir: Path) -> ManagedHarnessRuntimeAdapter:
    from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec

    spec = HarnessSpec(
        agent_revision_ref="agent-revision://restore-agent@1",
        model=ModelBinding(profile_ref="model-profile://m@1"),
        prompt=PromptSpec(instructions="p"),
    )
    adapter = ManagedHarnessRuntimeAdapter(spec, reasoner=_Reasoner(), engine=engine)
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


@pytest.mark.asyncio
async def test_adapter_restores_pending_handle_in_new_instance(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    first = _adapter(_FakeEngine(pending=True), state_dir)
    handle = await first.start(_request())

    # 模拟进程重启：全新引擎实例 + 同一状态目录，且 Checkpoint 存在未决状态
    second = _adapter(_FakeEngine(pending=True), state_dir)
    restored = await second.durable_restore(handle.run_id)

    assert restored is not None
    assert restored.run_id == handle.run_id
    # 幂等：再次 restore 返回同一已附着的运行态
    again = await second.durable_restore(handle.run_id)
    assert again is not None and again.run_id == handle.run_id
    assert second._engine.attached.count(handle.run_id) == 1


@pytest.mark.asyncio
async def test_adapter_restore_returns_none_for_settled_run(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    first = _adapter(_FakeEngine(pending=True), state_dir)
    handle = await first.start(_request())

    # 已完结的 run：attach 诚实报"无未决 Checkpoint"，restore 返回 None
    second = _adapter(_FakeEngine(pending=False), state_dir)
    assert await second.durable_restore(handle.run_id) is None


@pytest.mark.asyncio
async def test_adapter_restore_returns_none_for_unknown_run(tmp_path: Path) -> None:
    adapter = _adapter(_FakeEngine(pending=True), tmp_path / "state")
    assert await adapter.durable_restore("run_unknown") is None
