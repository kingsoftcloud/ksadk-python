# -*- coding: utf-8 -*-
"""run → cancel → resume → replay 本地 e2e (goal-16)。

一条链:RuntimeAdapter 跑一个 run(产 RuntimeEvent)→ 存 store → adapter.cancel →
adapter.resume(续跑产事件)→ 存 store → CLI ``replay`` 命令回放全部事件。
run/cancel/resume 走 RuntimeAdapter(不经 server 割裂实现);replay 走 CLI 命令。
"""

from __future__ import annotations

import asyncio
from typing import Any

from click.testing import CliRunner

from ksadk.cli.cmd_replay import replay
from ksadk.events.runtime_event import RuntimeEvent
from ksadk.events.store import RuntimeEventStore
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.adapter import ResumePayload, ResumeTarget, StartRequest
from ksadk.runtime.framework_adapters import ADKRuntimeAdapter
from ksadk.sessions.in_memory import InMemorySessionService


class _ChainRunner(BaseRunner):
    """可控 runner:可被 cancel 中断、可被 resume 续跑,产出确定事件。"""

    checkpoint_id = "adk-checkpoint-1"
    invocation_id = "adk-invocation-1"

    def __init__(self):
        super().__init__(detection_result=None, project_dir=".")
        self._release = asyncio.Event()
        self.resumed_with: dict[str, Any] | None = None

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
        return {"output": "done"}

    async def stream(self, input_data: dict[str, Any]):
        if input_data.get("checkpoint_resume"):
            # resume 续跑:记录 resume 输入,产出续跑文本 + final。
            self.resumed_with = input_data
            yield {"type": "delta", "delta": "续跑 "}
            yield {"type": "final", "output": "续跑完成"}
            return
        yield {"type": "delta", "delta": "首段 "}
        yield {
            "type": "checkpoint",
            "metadata": {
                "agentengine": {
                    "framework_ref": {
                        "adk": {
                            "checkpoint_id": self.checkpoint_id,
                            "invocation_id": self.invocation_id,
                        }
                    }
                }
            },
        }
        try:
            await self._release.wait()  # 阻塞等 cancel
        except (asyncio.CancelledError, GeneratorExit):
            raise
        yield {"type": "final", "output": "首段完成"}

    def describe_checkpoint_capability(self) -> dict[str, Any]:
        return {
            "Supported": True,
            "Backend": "fixture",
            "Scope": "invocation",
            "Durable": False,
            "SharedAcrossPods": False,
            "Reason": "fixture emits ADK native checkpoint and invocation references",
        }


def test_run_cancel_resume_replay_chain():
    svc = InMemorySessionService()
    runner = _ChainRunner()
    adapter = ADKRuntimeAdapter(runner)
    store = RuntimeEventStore(svc)

    async def _chain() -> None:
        await svc.create_session(agent_id="a", user_id="u", session_id="s1")
        # 1. run:start + stream(采集事件存 store)
        handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s1"))
        seen: list[RuntimeEvent] = []
        consume = asyncio.create_task(_drain(adapter, handle, seen))
        await asyncio.sleep(0.1)
        # 2. cancel:RuntimeAdapter.cancel(状态机,不经 server 割裂)
        cancel_result = await adapter.cancel(handle)
        assert cancel_result.value in ("interrupted_active_turn", "pending_cancel_recorded")
        await asyncio.wait_for(consume, timeout=2)
        await store.append(seen)
        checkpoint = await adapter.checkpoint(handle)
        assert checkpoint.checkpoint_id == runner.checkpoint_id
        assert checkpoint.ref["invocation_id"] == runner.invocation_id
        # 3. resume:RuntimeAdapter.resume(ADK invocation_id)→ 续跑产事件存 store
        resumed = await adapter.resume(
            handle,
            ResumeTarget(kind="invocation_id", id=runner.invocation_id),
            ResumePayload(kind="hitl_answer", call_id="c1", data={"answer": "继续"}),
        )
        resumed_events = [e async for e in adapter.stream(resumed)]
        await store.append(resumed_events)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_chain())
    finally:
        loop.close()

    # resume 真驱动:runner 收到 checkpoint_resume 输入
    assert runner.resumed_with and runner.resumed_with.get("checkpoint_resume") is True
    assert runner.resumed_with["framework_ref"]["adk"]["invocation_id"] == runner.invocation_id

    # 4. replay:CLI 命令回放 store 全部事件(sync 上下文,避免 asyncio.run 嵌套)
    import ksadk.sessions as _sess

    orig = _sess.resolve_session_service
    _sess.resolve_session_service = lambda: svc
    try:
        result = CliRunner().invoke(replay, ["s1"])
    finally:
        _sess.resolve_session_service = orig

    assert result.exit_code == 0, result.output
    # 首段 + 续跑事件都可回放
    assert "首段" in result.output
    assert "续跑" in result.output


async def _drain(adapter, handle, events: list) -> None:
    async for event in adapter.stream(handle):
        events.append(event)
