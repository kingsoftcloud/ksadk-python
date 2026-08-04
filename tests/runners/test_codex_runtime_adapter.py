"""CodexRuntimeAdapter 专项测试 (goal-09 三条契约)。

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
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.events.runtime_event import EventType
from ksadk.runtime.adapter import (
    CONVERSATION_PREPROCESSING_METADATA_KEY,
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


@pytest.mark.asyncio
async def test_conversation_preprocessing_preserves_history_in_codex_prompt() -> None:
    """防止统一 Web 切换到 Codex 后只发送当前一条用户消息。"""

    class _PromptCodex(_ControllableCodex):
        def __init__(self) -> None:
            super().__init__(block=False)
            self.prompts: list[object] = []

        def run_turn(self, thread_id, prompt, *, config=None):
            self.prompts.append(prompt)

            async def gen():
                yield {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "id": "m1",
                            "phase": "final_answer",
                            "type": "agentMessage",
                            "text": "done",
                        }
                    },
                }

            return gen()

    client = _PromptCodex()
    adapter = CodexRuntimeAdapter(client)
    request = StartRequest(
        input="current",
        user_id="u",
        session_id="s",
        metadata={
            CONVERSATION_PREPROCESSING_METADATA_KEY: {
                "messages": [
                    {"role": "user", "content": "previous"},
                    {"role": "assistant", "content": "answer"},
                    {"role": "user", "content": "current"},
                ]
            }
        },
    )

    handle = await adapter.start(request)
    events = [event async for event in adapter.stream(handle)]

    assert client.prompts == ["User: previous\nAssistant: answer\nUser: current"]
    assert events[-1].event_type == EventType.RUN_COMPLETED


# ---- 契约 1:cancel 中断活跃 turn(真实 SDK handle.interrupt)+ 不持久化被中断 session ----


@pytest.mark.asyncio
async def test_cancel_interrupts_turn_and_skips_persistence():
    client = _ControllableCodex()
    adapter = CodexRuntimeAdapter(client)
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
    adapter = CodexRuntimeAdapter(client)
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


@pytest.mark.asyncio
async def test_command_execution_is_projected_as_auditable_tool_events():
    class _CommandCodex(_ControllableCodex):
        def __init__(self) -> None:
            super().__init__(block=False)

        def run_turn(self, thread_id, prompt, *, config=None):
            async def gen():
                yield {
                    "method": "item/started",
                    "params": {
                        "item": {
                            "id": "cmd-1",
                            "type": "commandExecution",
                            "command": "sed -n '1,80p' src/demo.py",
                            "cwd": "/workspace",
                            "commandActions": [{"type": "read", "path": "src/demo.py"}],
                            "status": "inProgress",
                        }
                    },
                }
                yield {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "id": "cmd-1",
                            "type": "commandExecution",
                            "command": "sed -n '1,80p' src/demo.py",
                            "cwd": "/workspace",
                            "commandActions": [{"type": "read", "path": "src/demo.py"}],
                            "status": "completed",
                            "exitCode": 0,
                            "durationMs": 12,
                            "aggregatedOutput": "def divide(a, b): ...",
                        }
                    },
                }

            return gen()

    runtime = CodexRuntimeAdapter(_CommandCodex())
    handle = await runtime.start(StartRequest(input="review", user_id="u", session_id="s"))
    events = [event async for event in runtime.stream(handle)]

    begin = next(event for event in events if event.event_type == EventType.TOOL_CALL_BEGIN)
    end = next(event for event in events if event.event_type == EventType.TOOL_CALL_END)
    assert begin.payload == {
        "call_id": "cmd-1",
        "name": "codex.command",
        "args": {
            "command": "sed -n '1,80p' src/demo.py",
            "cwd": "/workspace",
            "command_actions": [{"type": "read", "path": "src/demo.py"}],
        },
    }
    assert end.payload == {
        "call_id": "cmd-1",
        "name": "codex.command",
        "result": {
            "status": "completed",
            "exit_code": 0,
            "duration_ms": 12,
            "output": "def divide(a, b): ...",
        },
    }


def test_async_client_keeps_codex_usage_and_turn_timing_notifications():
    """Break caught: exact SDK usage/timing is discarded before RuntimeAdapter sees it."""

    from ksadk.codex.client import AsyncCodexClient

    class _Payload:
        def __init__(self, value):
            self.value = value

        def model_dump(self, *, mode):
            assert mode == "json"
            return self.value

    class _Notification:
        def __init__(self, method, params):
            self.method = method
            self.payload = _Payload(params)

    fixtures = [
        (
            "thread/tokenUsage/updated",
            {
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "token_usage": {
                    "last": {
                        "input_tokens": 128,
                        "cached_input_tokens": 16,
                        "output_tokens": 32,
                        "reasoning_output_tokens": 8,
                        "total_tokens": 160,
                    }
                },
            },
        ),
        (
            "turn/started",
            {"thread_id": "thread-1", "turn": {"id": "turn-1", "started_at": 10}},
        ),
        (
            "turn/completed",
            {
                "thread_id": "thread-1",
                "turn": {
                    "id": "turn-1",
                    "started_at": 10,
                    "completed_at": 12,
                    "duration_ms": 1340,
                },
            },
        ),
    ]

    assert [
        AsyncCodexClient._notification_to_event_dict(_Notification(method, params))
        for method, params in fixtures
    ] == [
        {"method": method, "params": params}
        for method, params in fixtures
    ]


@pytest.mark.asyncio
async def test_codex_runtime_projects_exact_usage_and_turn_duration():
    """Break caught: Studio reports zero tokens and local stopwatch duration."""

    class _MetricsCodex(_ControllableCodex):
        def __init__(self) -> None:
            super().__init__(block=False)

        def run_turn(self, thread_id, prompt, *, config=None):
            async def gen():
                yield {
                    "method": "turn/started",
                    "params": {
                        "thread_id": thread_id,
                        "turn": {"id": "turn-1", "started_at": 10},
                    },
                }
                yield {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "thread_id": thread_id,
                        "turn_id": "turn-1",
                        "token_usage": {
                            "last": {
                                "input_tokens": 128,
                                "cached_input_tokens": 16,
                                "output_tokens": 32,
                                "reasoning_output_tokens": 8,
                                "total_tokens": 160,
                            }
                        },
                    },
                }
                yield {
                    "method": "turn/completed",
                    "params": {
                        "thread_id": thread_id,
                        "turn": {
                            "id": "turn-1",
                            "started_at": 10,
                            "completed_at": 12,
                            "duration_ms": 1340,
                        },
                    },
                }

            return gen()

    runtime = CodexRuntimeAdapter(_MetricsCodex())
    handle = await runtime.start(StartRequest(input="review", user_id="u", session_id="s"))
    events = [event async for event in runtime.stream(handle)]

    usage = next(event for event in events if event.event_type == EventType.USAGE_REPORTED)
    assert usage.payload == {
        "input_tokens": 128,
        "cached_tokens": 16,
        "output_tokens": 32,
        "reasoning_tokens": 8,
        "total_tokens": 160,
        "source": "codex",
    }
    completed = next(event for event in events if event.event_type == EventType.RUN_COMPLETED)
    assert completed.payload == {
        "status": "completed",
        "started_at": 10,
        "completed_at": 12,
        "duration_ms": 1340,
        "source": "codex",
    }


# ---- 契约 3:resume thread id ----


@pytest.mark.asyncio
async def test_resume_uses_thread_id():
    client = _ControllableCodex(block=False)
    adapter = CodexRuntimeAdapter(client)
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
    adapter = CodexRuntimeAdapter(client)
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
    adapter = CodexRuntimeAdapter(client)
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
