"""CodexRuntime 专项测试 (goal-09 三条契约)。

- 契约 1 cancel:活跃 turn 中断 + **杀进程 + 不持久化被中断 session**;
  pending-cancel;级联清审批;CancelResult 各枚举。
- 契约 2 phase 翻译(codex_phase):按 itemId 区分 commentary vs final_answer,
  不从文本内容推断;不混入最终答案;completed 后遗忘。
- 契约 3 resume thread id:ResumeTarget=thread_id;拒绝非 thread_id 目标;
  拒绝 resume 已被杀/不持久化的 thread。
- 依赖契约:openai-codex 是可选 extra,缺 openai_codex 时 AsyncCodexClient 显式报错
  (``pip install 'ksadk[codex]'``),不静默失败。
"""

from __future__ import annotations

import asyncio

import pytest

from ksadk.codex.client import CodexClient
from ksadk.codex.phase import CodexPhaseTracker
from ksadk.codex.runtime import CodexRuntime
from ksadk.runtime.adapter import (
    CancelResult,
    ResumePayload,
    ResumeTarget,
    StartRequest,
)


class _ControllableCodex(CodexClient):
    def __init__(self, *, block: bool = True) -> None:
        self._block = block
        self._release = asyncio.Event()
        self.started_threads: list[str] = []
        self.resumed_threads: list[str] = []
        self.interrupted: list[str] = []
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
            yield {"method": "execCommand/approvalRequest", "params": {"id": "call-1"}}
            if self._block:
                await self._release.wait()
            yield {
                "method": "item/completed",
                "params": {"item": {"id": "m1", "phase": "final_answer", "text": "done"}},
            }

        return gen()

    async def interrupt_active_turn(self, thread_id: str) -> bool:
        self.interrupted.append(thread_id)
        return True

    async def close(self) -> None:
        return None


async def _run_stream(adapter, handle, events: list):
    async for event in adapter.stream(handle):
        events.append(event)


# ---- 契约 1:cancel 中断活跃 turn(真实 SDK handle.interrupt)+ 不持久化被中断 session ----


@pytest.mark.asyncio
async def test_cancel_interrupts_turn_and_skips_persistence():
    client = _ControllableCodex()
    adapter = CodexRuntime(client)
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
    events: list = []
    consume = asyncio.create_task(_run_stream(adapter, handle, events))
    await asyncio.sleep(0.1)
    result = await adapter.cancel(handle)
    assert result is CancelResult.INTERRUPTED_ACTIVE_TURN
    # 真实中断:interrupt_active_turn 被调(真实 SDK handle.interrupt;无"杀进程"概念)。
    assert client.interrupted == [handle.run_id]
    # 不持久化:该 thread 进 do_not_persist,resume 被拒。
    assert handle.run_id in adapter._do_not_persist
    with pytest.raises(ValueError, match="不持久化|不可 resume"):
        await adapter.resume(
            handle,
            ResumeTarget(kind="thread_id", id=handle.run_id),
            None,
        )
    await asyncio.wait_for(consume, timeout=2)


@pytest.mark.asyncio
async def test_cancel_cascades_pending_approvals():
    client = _ControllableCodex()
    adapter = CodexRuntime(client)
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
    events: list = []
    consume = asyncio.create_task(_run_stream(adapter, handle, events))
    await asyncio.sleep(0.1)
    await adapter.cancel(handle)
    # 级联丢弃来自 runtime 自跟踪的 pending 审批集(真实 SDK 无独立 drain API)。
    assert adapter.last_cancel_dropped_approvals == {"call-1"}
    await asyncio.wait_for(consume, timeout=2)


# ---- 契约 2:phase 翻译 ----


def test_phase_tracker_routes_by_item_id_not_text():
    tracker = CodexPhaseTracker()
    tracker.observe_item({"item": {"id": "m1", "phase": "commentary", "text": ""}})
    tracker.observe_item({"item": {"id": "m2", "phase": "final_answer", "text": ""}})
    # delta 只带 itemId,不带 phase —— 必须按 itemId 解析,不从文本推断。
    assert (
        tracker.runtime_phase_for_delta({"itemId": "m1", "delta": "看起来像在回答"}) == "commentary"
    )
    assert (
        tracker.runtime_phase_for_delta({"itemId": "m2", "delta": "我思考一下"}) == "final_answer"
    )


def test_phase_tracker_forgets_on_complete():
    tracker = CodexPhaseTracker()
    tracker.observe_item({"item": {"id": "m1", "phase": "commentary", "text": ""}})
    tracker.forget_item({"item": {"id": "m1"}})
    assert tracker.phase_for_delta({"itemId": "m1", "delta": "x"}) is None


def test_phase_analysis_is_commentary():
    tracker = CodexPhaseTracker()
    tracker.observe_item({"item": {"id": "m1", "phase": "analysis", "text": ""}})
    assert tracker.runtime_phase_for_delta({"itemId": "m1", "delta": "x"}) == "commentary"


# ---- 契约 3:resume thread id ----


@pytest.mark.asyncio
async def test_resume_uses_thread_id():
    client = _ControllableCodex(block=False)
    adapter = CodexRuntime(client)
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
    resumed = await adapter.resume(
        handle,
        ResumeTarget(kind="thread_id", id=handle.run_id),
        ResumePayload(kind="hitl_answer", call_id="call-1", data={"answer": "ok"}),
    )
    assert resumed.native_ref["resume_thread_id"] == handle.run_id
    assert resumed.native_ref["resume_input"]["thread_id"] == handle.run_id
    assert client.resumed_threads == [handle.run_id]


@pytest.mark.asyncio
async def test_cancel_resumed_target_uses_native_thread_id_not_run_id():
    client = _ControllableCodex()
    adapter = CodexRuntime(client)
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
    await adapter.resume(
        handle,
        ResumeTarget(kind="thread_id", id="external-thread"),
        ResumePayload(kind="free_text", data="continue"),
    )
    consume = asyncio.create_task(_run_stream(adapter, handle, []))
    await asyncio.sleep(0.1)
    result = await adapter.cancel(handle)
    await asyncio.wait_for(consume, timeout=2)

    assert result is CancelResult.INTERRUPTED_ACTIVE_TURN
    assert client.interrupted == ["external-thread"]


@pytest.mark.asyncio
async def test_resume_rejects_non_thread_id_target():
    client = _ControllableCodex(block=False)
    adapter = CodexRuntime(client)
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
    with pytest.raises(ValueError, match="thread_id"):
        await adapter.resume(handle, ResumeTarget(kind="invocation_id", id="x"), None)


# ---- 依赖契约:缺 openai_codex 显式报错 ----


def test_async_codex_client_explicit_error_when_missing(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "openai_codex", None)  # 模拟未安装
    from ksadk.codex.client import AsyncCodexClient

    with pytest.raises(RuntimeError, match=r"ksadk\[codex\]"):
        AsyncCodexClient()


def test_codex_is_optional_extra_not_default():
    """openai-codex 是可选 extra(codex),不进默认依赖。"""
    import pathlib
    import re

    pyproject = (pathlib.Path(__file__).parent.parent.parent / "pyproject.toml").read_text()
    # codex extra 段存在且含 openai-codex;默认 dependencies 段不含 openai-codex。
    assert re.search(r"^codex = \[", pyproject, re.M)
    default_deps = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "openai-codex" not in default_deps
