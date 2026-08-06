"""RuntimeAdapter 框架共用 contract test (goal-07 + goal-09)。

**同一套 contract test 跑 ADK / LangGraph / Codex 三个 adapter**,统一强制覆盖(对未来
第 4 框架同样适用,见 goal-15):

- cancel 全语义:活跃 turn interrupt / 无活跃 turn 记 pending-cancel /
  **级联丢弃 pending 工具审批** / CancelResult 各枚举值(INTERRUPTED_ACTIVE_TURN /
  PENDING_CANCEL_RECORDED / NOT_RUNNING)。
- resume:ResumeTarget / ResumePayload 分离入参,框架各自目标(ADK invocation_id /
  LangGraph checkpoint_id / Codex thread_id)。
- checkpoint:capability 诚实声明(ADK forward-only vs LangGraph time-travel vs Codex thread)。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from ksadk.codex.client import CodexClient
from ksadk.codex.runtime import CodexRuntime
from ksadk.events.runtime_event import RuntimeEvent
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.adapter import (
    CancelResult,
    CheckpointCapability,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    StartRequest,
)
from ksadk.runtime.framework_adapters import ADKRuntimeAdapter, LangGraphRuntimeAdapter
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter

# ---------------------------------------------------------------------------
# 第 4 框架试金石(goal-15)—— 内联 fixture(原 ksadk/runtime/miniflow_adapter.py,
# 非生产能力,移入 tests 作测试用例)。
# MiniFlow:最小但真实的 BaseRunner 框架,resume 目标 = ``snapshot_id``(与 ADK
# ``invocation_id`` / LangGraph ``checkpoint_id`` / Codex ``thread_id`` 并列的第 4 种,
# 证明 resume union 泛化)。接入零平台改动:只 override ``_resume_native`` 与
# ``_checkpoint_capability``,消费方 ``registry.register`` 注册。
# ---------------------------------------------------------------------------

#: MiniFlow 框架标识(注册进 RuntimeRegistry 的 runtime_type)。
MINIFLOW_RUNTIME_TYPE = "miniflow"


class MiniFlowRuntimeAdapter(RunnerRuntimeAdapter):
    """MiniFlow 框架 adapter(第 4 个,试金石)。

    MiniFlow 的 checkpoint 模型:按 flow ``snapshot_id`` 恢复(支持按 snapshot 回滚/fork,
    与 LangGraph time-travel 类似但目标命名不同,证明接口不绑定特定框架的命名)。
    """

    def __init__(self, runner: BaseRunner) -> None:
        super().__init__(runner, runtime_type=MINIFLOW_RUNTIME_TYPE)

    async def _resume_native(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: Optional[ResumePayload],
    ) -> Optional[dict]:
        # MiniFlow:恢复目标 = snapshot_id。注入 runner 消费的 ``checkpoint_resume`` +
        # ``framework_ref.miniflow.snapshot_id``(与 ADK/LG 同构,键名随框架)。
        return {
            "checkpoint_resume": True,
            "run_id": target.id,
            "framework_ref": {"miniflow": {"snapshot_id": target.id}},
            "input": payload.data if payload else None,
        }

    def _checkpoint_capability(self) -> CheckpointCapability:
        # 冻结 G0.3 CheckpointCapability.rollback_scope ∈ {turn,invocation,none}:
        # 第 4 框架必须映射到其一(不开接口后门)。MiniFlow snapshot 按 turn 回滚/fork。
        return CheckpointCapability(
            supported=True,
            granularity="snapshot",
            rollback_scope="turn",
            fork_supported=True,
            durable=False,
            shared_across_pods=False,
            reason="MiniFlow resume/fork via flow snapshot_id (turn-scoped rollback)",
        )


class _ContractRunner(BaseRunner):
    """受控 runner(ADK/LG):先发一条 approval(记 pending 审批),再阻塞等中断,最后 final。"""

    def __init__(self, *, block: bool = True, with_approval: bool = True) -> None:
        super().__init__(detection_result=None, project_dir=".")
        self._block = block
        self._with_approval = with_approval
        self._release = asyncio.Event()
        self.stream_interrupted = False
        self.received_inputs: list[dict[str, Any]] = []

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
        return {"output": "done"}

    async def stream(self, input_data: dict[str, Any]):
        self.received_inputs.append(input_data)
        if self._with_approval:
            yield {"type": "approval", "call_id": "call-1"}
        if self._block:
            try:
                await self._release.wait()
            except (asyncio.CancelledError, GeneratorExit):
                self.stream_interrupted = True
                raise
        yield {"type": "final", "output": "done"}

    def describe_checkpoint_capability(self) -> dict[str, Any]:
        return {
            "Supported": True,
            "Backend": "fixture",
            "Scope": "test",
            "Durable": False,
            "SharedAcrossPods": False,
            "Reason": "fixture emits native checkpoint references",
        }


class _FakeCodexClient(CodexClient):
    """受控 codex 后端:与 _ContractRunner 同构——先发 approval,再阻塞等中断,最后 completed。"""

    def __init__(self, *, block: bool = True, with_approval: bool = True) -> None:
        self._block = block
        self._with_approval = with_approval
        self._release = asyncio.Event()
        self.started_threads: list[str] = []
        self.resumed_threads: list[str] = []
        self.interrupted: list[str] = []
        self.stream_interrupted = False
        self._seq = 0

    async def start_thread(self, config=None) -> str:
        self._seq += 1
        thread_id = f"codex_thread_{self._seq}"
        self.started_threads.append(thread_id)
        return thread_id

    async def resume_thread(self, thread_id: str, config=None) -> str:
        self.resumed_threads.append(thread_id)
        return thread_id

    def run_turn(self, thread_id, prompt, *, config=None):
        async def gen():
            if self._with_approval:
                yield {"method": "execCommand/approvalRequest", "params": {"id": "call-1"}}
            if self._block:
                try:
                    await self._release.wait()
                except (asyncio.CancelledError, GeneratorExit):
                    self.stream_interrupted = True
                    raise
            yield {
                "method": "item/completed",
                "params": {"item": {"id": "m1", "phase": "final_answer", "text": "done"}},
            }

        return gen()

    async def interrupt_active_turn(self, thread_id: str) -> bool:
        self.interrupted.append(thread_id)
        # 仅记录 interrupt;turn 的本地中断由 runtime 取消 chunk task 产生
        # (CancelledError 进入生成器 in-flight await,与 ADK/LangGraph 同机制)。
        return True

    async def close(self) -> None:
        return None


def _make_adk(**kw):
    runner = _ContractRunner(**kw)
    return ADKRuntimeAdapter(runner), runner


def _make_langgraph(**kw):
    runner = _ContractRunner(**kw)
    return LangGraphRuntimeAdapter(runner), runner


def _make_codex(**kw):
    client = _FakeCodexClient(**kw)
    return CodexRuntime(client), client


def _make_miniflow(**kw):
    runner = _ContractRunner(**kw)
    return MiniFlowRuntimeAdapter(runner), runner


#: (factory, framework, resume_kind) —— 同一 contract test 跑三遍。
#: resume_kind 是 G0.3 冻结的闭 union {invocation_id,thread_id,checkpoint_id};
#: 第 4 框架(MiniFlow)的 snapshot 本质是 checkpoint,经通用 ``checkpoint_id`` kind 接入,
#: 框架特定细节放 ``framework_ref.miniflow.snapshot_id``(接口抽象层级的正确性证据)。
ADAPTERS = [
    (_make_adk, "adk", "invocation_id"),
    (_make_langgraph, "langgraph", "checkpoint_id"),
    (_make_codex, "codex", "thread_id"),
    (_make_miniflow, "miniflow", "checkpoint_id"),
]


async def _drain(adapter, handle, events: list):
    async for event in adapter.stream(handle):
        events.append(event)


@pytest.mark.parametrize("factory,framework,resume_kind", ADAPTERS)
class TestAdapterContract:
    # ---- 六动词 + stream 返回 RuntimeEvent ----

    @pytest.mark.asyncio
    async def test_stream_returns_runtime_events(self, factory, framework, resume_kind):
        adapter, _ = factory(block=False)
        handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
        events = [e async for e in adapter.stream(handle)]
        assert all(isinstance(e, RuntimeEvent) for e in events)
        assert any(e.event_type == "run.started" for e in events)

    # ---- cancel:活跃 turn 中断 ----

    @pytest.mark.asyncio
    async def test_cancel_active_turn_returns_interrupted(self, factory, framework, resume_kind):
        adapter, runner = factory()
        handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
        events: list = []
        consume = asyncio.create_task(_drain(adapter, handle, events))
        await asyncio.sleep(0.1)  # 让 stream 进入 approval + 阻塞
        result = await adapter.cancel(handle)
        assert result is CancelResult.INTERRUPTED_ACTIVE_TURN
        await asyncio.wait_for(consume, timeout=2)
        # 锁死真实中断:CancelledError 必须真打进 runner 生成器 in-flight 的 await
        # (非仅停止消费事件)。ADK/LG 无原生 cancel 方法,asyncio 任务取消即真实机制。
        assert runner.stream_interrupted is True

    @pytest.mark.asyncio
    async def test_cancel_cascades_pending_approvals(self, factory, framework, resume_kind):
        """级联丢弃 pending 工具审批:cancel 时该 turn 的 pending 审批被清空。"""
        adapter, _ = factory(with_approval=True)
        handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
        events: list = []
        consume = asyncio.create_task(_drain(adapter, handle, events))
        await asyncio.sleep(0.1)  # approval(call-1)已记录
        result = await adapter.cancel(handle)
        assert result is CancelResult.INTERRUPTED_ACTIVE_TURN
        assert adapter.last_cancel_dropped_approvals == {"call-1"}
        await asyncio.wait_for(consume, timeout=2)

    # ---- cancel:无活跃 turn 记 pending ----

    @pytest.mark.asyncio
    async def test_cancel_without_active_turn_records_pending(
        self, factory, framework, resume_kind
    ):
        adapter, _ = factory()
        handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
        # 未启动 stream(无活跃 turn),但 invocation 已知 → pending。
        result = await adapter.cancel(handle)
        assert result is CancelResult.PENDING_CANCEL_RECORDED
        # 下一个 turn 启动时消费 pending:立即给 canceled。
        events = [e async for e in adapter.stream(handle)]
        assert any(e.event_type == "run.canceled" for e in events)

    # ---- cancel:未知 invocation → NOT_RUNNING ----

    @pytest.mark.asyncio
    async def test_cancel_unknown_returns_not_running(self, factory, framework, resume_kind):
        adapter, _ = factory()
        unknown = RunHandle(run_id="inv_nope", session_id="s", runtime_type="x")
        result = await adapter.cancel(unknown)
        assert result is CancelResult.NOT_RUNNING

    # ---- CancelResult 枚举值覆盖 ----

    @pytest.mark.asyncio
    async def test_cancel_result_enum_values_reachable(self, factory, framework, resume_kind):
        assert CancelResult.INTERRUPTED_ACTIVE_TURN is not CancelResult.PENDING_CANCEL_RECORDED
        assert CancelResult.NOT_RUNNING is not CancelResult.FAILED
        assert len(list(CancelResult)) == 4

    # ---- resume:双参数分离 + 框架目标 ----

    @pytest.mark.asyncio
    async def test_resume_separates_target_and_payload(self, factory, framework, resume_kind):
        adapter, runner = factory(block=False)
        handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
        target = ResumeTarget(kind=resume_kind, id="ref-1")
        payload = ResumePayload(kind="approval_decision", call_id="call-1", data={"d": "ok"})
        if framework == "langgraph":
            handle.native_ref["known_checkpoint_ids"] = ["ref-1"]
            handle.native_ref["pending_approval_ids"] = ["call-1"]
        resumed = await adapter.resume(handle, target, payload)

        if framework == "codex":
            # Codex 走自己的 thread_resume 机制(native_ref 驱动,见 P0-1)。
            resume_input = resumed.native_ref.get("resume_input")
            assert resume_input is not None
            assert resume_input.get(resume_kind) == "ref-1"
            assert resume_input["payload"] == {"d": "ok"}
            return

        # ADK / LangGraph:resume 必须把 ``checkpoint_resume`` + 框架 ``framework_ref``
        # 真注入下一次 stream() 的 runner 输入(非摆设 native_ref)。
        _ = [e async for e in adapter.stream(resumed)]
        assert runner.received_inputs, "resume 后 stream 未触发 runner"
        injected = runner.received_inputs[-1]
        assert injected.get("checkpoint_resume") is True
        framework_ref = injected.get("framework_ref") or {}
        if framework == "adk":
            assert framework_ref.get("adk", {}).get("invocation_id") == "ref-1"
            assert injected.get("run_id") == "ref-1"
        elif framework == "langgraph":
            assert framework_ref.get("langgraph", {}).get("checkpoint_id") == "ref-1"
        elif framework == "miniflow":
            assert framework_ref.get("miniflow", {}).get("snapshot_id") == "ref-1"
            assert injected.get("run_id") == "ref-1"
        # payload 作为 resume 输入传给 runner。
        assert injected.get("input") == {"d": "ok"}

    # ---- checkpoint:capability 诚实声明(框架差异) ----

    @pytest.mark.asyncio
    async def test_checkpoint_capability_honest(self, factory, framework, resume_kind):
        adapter, _ = factory()
        handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
        handle.native_ref["checkpoint_id"] = "fixture-checkpoint"
        capability = (await adapter.checkpoint(handle)).capability
        if framework == "adk":
            # ADK forward-only:delta/invocation,不支持 fork。
            assert capability.granularity == "delta"
            assert capability.rollback_scope == "invocation"
            assert capability.fork_supported is False
        elif framework in ("langgraph", "miniflow"):
            # LangGraph time-travel / MiniFlow snapshot:snapshot/turn,支持 fork。
            assert capability.granularity == "snapshot"
            assert capability.rollback_scope == "turn"
            assert capability.fork_supported is True
        else:
            # Codex:thread resume/fork。
            assert capability.fork_supported is True
