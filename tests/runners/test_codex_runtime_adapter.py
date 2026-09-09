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
import queue
import threading
from types import SimpleNamespace

import pytest

from ksadk.codex.client import CodexClient
from ksadk.codex.phase import CodexPhaseTracker
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.events.canonical import (
    ContinuationCreated,
    InteractionRequested,
    ItemCompleted,
    ItemFailed,
    ItemStarted,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    UsageReported,
)
from ksadk.runtime.adapter import (
    CONVERSATION_PREPROCESSING_METADATA_KEY,
    CancelResult,
    PauseResult,
    ResumePayload,
    ResumeTarget,
    StartRequest,
)

# ---- helpers for valid codex notification messages ----


def _turn_started(thread_id: str, turn_id: str = "turn-1") -> dict:
    return {
        "method": "turn/started",
        "params": {
            "threadId": thread_id,
            "turn": {"id": turn_id, "status": "inProgress"},
        },
    }


def _turn_completed(thread_id: str, turn_id: str = "turn-1") -> dict:
    return {
        "method": "turn/completed",
        "params": {
            "threadId": thread_id,
            "turn": {"id": turn_id, "status": "completed", "items": []},
        },
    }


def _item_started(thread_id: str, item_id: str, *, turn_id: str = "turn-1", **item_fields) -> dict:
    item = {"id": item_id, "type": "agentMessage", "text": "", "phase": "final_answer"}
    item.update(item_fields)
    return {
        "method": "item/started",
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "item": item,
        },
    }


def _item_completed(
    thread_id: str,
    item_id: str,
    *,
    turn_id: str = "turn-1",
    **item_fields,
) -> dict:
    item = {"id": item_id, "type": "agentMessage", "text": "done", "phase": "final_answer"}
    item.update(item_fields)
    return {
        "method": "item/completed",
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "item": item,
        },
    }


def _approval_request(thread_id: str, item_id: str, *, turn_id: str = "turn-1") -> dict:
    return {
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "itemId": item_id,
            "command": "git status",
            "cwd": "/workspace",
        },
    }


async def _simple_turn_lifecycle(thread_id: str, *, text: str = "done"):
    """Yield a minimal valid turn with item and turn lifecycle boundaries."""
    yield _turn_started(thread_id)
    yield _item_started(thread_id, "m1")
    yield _item_completed(thread_id, "m1", text=text)
    yield _turn_completed(thread_id)


class _ControllableCodex(CodexClient):
    def __init__(self, *, block: bool = True) -> None:
        self._block = block
        self._release = asyncio.Event()
        self.started_threads: list[str] = []
        self.resumed_threads: list[str] = []
        self.interrupted: list[str] = []
        self._seq = 0
        self.resolved_approvals: list[tuple[str, str]] = []
        self.resolved_interactions: list[tuple[str, dict]] = []
        self.close_calls = 0

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
            yield _turn_started(thread_id)
            # Emit an approval request so the canonical adapter can track
            # pending_approvals from InteractionRequested events.
            yield {
                "id": "req-approval-1",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": thread_id,
                    "turnId": "turn-1",
                    "itemId": "call-1",
                    "approvalId": "call-1",
                    "command": "git status",
                    "cwd": "/workspace",
                },
            }
            if self._block:
                await self._release.wait()
            yield _item_started(thread_id, "m1")
            yield _item_completed(thread_id, "m1", text="done")
            yield _turn_completed(thread_id)

        return gen()

    async def interrupt_active_turn(self, thread_id: str) -> bool:
        self.interrupted.append(thread_id)
        # The native SDK emits terminal notifications after interrupt; release
        # the fixture stream to model that lifecycle instead of relying on
        # cancellation of the async generator itself.
        self._release.set()
        return True

    async def close(self) -> None:
        self.close_calls += 1

    async def resolve_approval(self, approval_id: str, decision: str) -> bool:
        self.resolved_approvals.append((approval_id, decision))
        return True

    async def resolve_interaction(self, interaction_id: str, data: dict) -> bool:
        self.resolved_interactions.append((interaction_id, data))
        return True


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
                yield _turn_started(thread_id)
                yield _item_started(thread_id, "m1")
                yield _item_completed(thread_id, "m1", text="done")
                yield _turn_completed(thread_id)

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

    assert client.prompts == ["User: previous\n[上一轮已回复: answer]\nUser: current"]
    assert isinstance(events[-1], RunCompleted)


@pytest.mark.asyncio
async def test_resumed_native_thread_does_not_duplicate_transport_history() -> None:
    class _PromptCodex(_ControllableCodex):
        def __init__(self) -> None:
            super().__init__(block=False)
            self.prompts: list[object] = []

        def run_turn(self, thread_id, prompt, *, config=None):
            self.prompts.append(prompt)

            async def gen():
                yield _turn_started(thread_id)
                yield _item_started(thread_id, "m1")
                yield _item_completed(thread_id, "m1", text="done")
                yield _turn_completed(thread_id)

            return gen()

    client = _PromptCodex()
    adapter = CodexRuntimeAdapter(client)
    request = StartRequest(
        input="current",
        user_id="u",
        session_id="s",
        metadata={
            "thread_id": "codex_thread_existing",
            CONVERSATION_PREPROCESSING_METADATA_KEY: {
                "messages": [
                    {"role": "user", "content": "previous"},
                    {"role": "assistant", "content": "answer"},
                    {"role": "user", "content": "current"},
                ]
            },
        },
    )

    handle = await adapter.start(request)
    _events = [event async for event in adapter.stream(handle)]

    assert handle.run_id == "codex_thread_existing"
    assert client.prompts == ["current"]
    assert not any(isinstance(event, ContinuationCreated) for event in _events)


@pytest.mark.asyncio
async def test_plan_mode_is_forwarded_as_native_collaboration_mode() -> None:
    class _PlanCodex(_ControllableCodex):
        def __init__(self) -> None:
            super().__init__(block=False)
            self.configs: list[dict] = []

        def run_turn(self, thread_id, prompt, *, config=None):
            self.configs.append(dict(config or {}))

            async def gen():
                yield _turn_started(thread_id)
                yield _item_started(thread_id, "m1")
                yield _item_completed(thread_id, "m1", text="plan")
                yield _turn_completed(thread_id)

            return gen()

    client = _PlanCodex()
    adapter = CodexRuntimeAdapter(client)
    handle = await adapter.start(
        StartRequest(
            input="规划迁移",
            user_id="u",
            session_id="s",
            model="glm-5.2",
            config={"collaboration_mode": "plan"},
        )
    )

    events = [event async for event in adapter.stream(handle)]

    assert client.configs == [
        {
            "sandbox_read_only": True,
            "collaboration_mode": "plan",
            "model": "glm-5.2",
        }
    ]
    assert isinstance(events[-1], RunCompleted)


@pytest.mark.asyncio
async def test_default_request_uses_native_codex_agent_loop() -> None:
    class _LoopCodex(_ControllableCodex):
        def __init__(self) -> None:
            super().__init__(block=False)
            self.turn_calls: list[tuple[str, str, dict]] = []

        def run_turn(self, thread_id, prompt, *, config=None):
            self.turn_calls.append((thread_id, prompt, dict(config or {})))
            return _simple_turn_lifecycle(thread_id, text="loop done")

    client = _LoopCodex()
    adapter = CodexRuntimeAdapter(client)
    handle = await adapter.start(StartRequest(input="执行任务", user_id="u", session_id="s"))

    events = [event async for event in adapter.stream(handle)]

    assert client.turn_calls == [(handle.run_id, "执行任务", {"sandbox_read_only": True})]
    assert isinstance(events[-1], RunCompleted)


@pytest.mark.asyncio
async def test_goal_objective_uses_native_goal_operation() -> None:
    class _GoalCodex(_ControllableCodex):
        def __init__(self) -> None:
            super().__init__(block=False)
            self.goal_calls: list[tuple[str, str, dict]] = []

        def run_goal(self, thread_id, objective, *, config=None):
            self.goal_calls.append((thread_id, objective, dict(config or {})))

            async def gen():
                yield _turn_started(thread_id)
                yield _item_started(thread_id, "m1")
                yield _item_completed(thread_id, "m1", text="goal done")
                yield _turn_completed(thread_id)

            return gen()

    client = _GoalCodex()
    adapter = CodexRuntimeAdapter(client)
    handle = await adapter.start(
        StartRequest(
            input="完成 Composer 重构",
            user_id="u",
            session_id="s",
            model="glm-5.2",
            config={"goal_objective": "完成 Composer 重构"},
        )
    )

    events = [event async for event in adapter.stream(handle)]

    assert client.goal_calls == [
        (
            handle.run_id,
            "完成 Composer 重构",
            {"sandbox_read_only": True, "model": "glm-5.2"},
        )
    ]
    assert isinstance(events[-1], RunCompleted)


@pytest.mark.asyncio
async def test_goal_thread_is_started_as_persisted_not_ephemeral() -> None:
    class _PersistedGoalCodex(_ControllableCodex):
        def __init__(self) -> None:
            super().__init__(block=False)
            self.start_configs: list[dict] = []

        async def start_thread(self, config=None) -> str:
            self.start_configs.append(dict(config or {}))
            return await super().start_thread(config)

    client = _PersistedGoalCodex()
    adapter = CodexRuntimeAdapter(client)
    await adapter.start(
        StartRequest(
            input="完成目标",
            user_id="u",
            session_id="s",
            config={"goal_objective": "完成目标", "ephemeral": False},
        )
    )

    assert client.start_configs == [{"sandbox_read_only": True, "ephemeral": False}]


@pytest.mark.asyncio
async def test_structured_image_input_is_preserved_with_bound_skills() -> None:
    from openai_codex import ImageInput, SkillInput, TextInput

    class _InputCodex(_ControllableCodex):
        def __init__(self) -> None:
            super().__init__(block=False)
            self.prompts: list[object] = []

        def run_turn(self, thread_id, prompt, *, config=None):
            self.prompts.append(prompt)

            async def gen():
                yield _turn_started(thread_id)
                yield _item_started(thread_id, "m1")
                yield _item_completed(thread_id, "m1", text="done")
                yield _turn_completed(thread_id)

            return gen()

    client = _InputCodex()
    adapter = CodexRuntimeAdapter(client)
    handle = await adapter.start(
        StartRequest(
            input=[
                {"type": "text", "text": "分析截图"},
                {"type": "image", "url": "data:image/png;base64,AAAA"},
            ],
            user_id="u",
            session_id="s",
            config={"skills": [{"name": "review", "path": "/skills/review"}]},
        )
    )

    _events = [event async for event in adapter.stream(handle)]

    assert client.prompts == [
        [
            SkillInput(name="review", path="/skills/review"),
            TextInput(text="分析截图"),
            ImageInput(url="data:image/png;base64,AAAA"),
        ]
    ]


@pytest.mark.asyncio
async def test_sdk_plan_bridge_injects_app_server_collaboration_mode_payload() -> None:
    from ksadk.codex.client import AsyncCodexClient

    captured: dict[str, object] = {}

    class _LowLevel:
        async def turn_start(self, thread_id, wire_input, *, params):
            captured.update({"thread_id": thread_id, "input": wire_input, "params": params})
            return SimpleNamespace(turn=SimpleNamespace(id="turn-plan"))

    codex = SimpleNamespace(_client=_LowLevel())
    client = AsyncCodexClient.__new__(AsyncCodexClient)
    client._codex = codex
    thread = SimpleNamespace(id="thread-plan")

    handle = await client._start_turn(
        thread,
        "先分析迁移边界",
        {"collaboration_mode": "plan", "model": "glm-5.2"},
    )

    assert handle.id == "turn-plan"
    assert captured["input"] == [{"type": "text", "text": "先分析迁移边界"}]
    params = captured["params"]
    assert isinstance(params, dict)
    assert params["collaborationMode"] == {
        "mode": "plan",
        "settings": {
            "model": "glm-5.2",
            "reasoning_effort": None,
            "developer_instructions": None,
        },
    }


# ---- 契约 1:cancel 中断活跃 turn(真实 SDK handle.interrupt)+ 不持久化被中断 session ----


@pytest.mark.asyncio
async def test_close_after_terminal_event_is_idempotent_without_interrupt():
    client = _ControllableCodex(block=False)
    adapter = CodexRuntimeAdapter(client)
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
    stream = adapter.stream(handle)
    try:
        while True:
            event = await anext(stream)
            if isinstance(event, RunCompleted):
                break

        # Model the Kernel worker: it stops consuming immediately after the
        # canonical terminal fact, before the provider iterator reaches EOF.
        assert adapter._threads[handle.run_id].streaming is True
        await adapter.close(handle)
        await adapter.close(handle)
    finally:
        await stream.aclose()

    assert client.interrupted == []
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_close_interrupts_an_active_turn_once():
    client = _ControllableCodex()
    adapter = CodexRuntimeAdapter(client)
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
    events: list = []
    consume = asyncio.create_task(_run_stream(adapter, handle, events))
    for _ in range(100):
        thread = adapter._threads.get(handle.run_id)
        if thread is not None and thread.streaming:
            break
        await asyncio.sleep(0.01)

    await adapter.close(handle)
    await adapter.close(handle)
    await asyncio.wait_for(consume, timeout=2)

    assert client.interrupted == [handle.run_id]
    assert client.close_calls == 1


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
    # In the canonical path, CodexRuntimeAdapter tracks pending_approvals
    # from InteractionRequested events (canonical interaction_id).
    client = _ControllableCodex()
    adapter = CodexRuntimeAdapter(client)
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
    events: list = []
    consume = asyncio.create_task(_run_stream(adapter, handle, events))
    for _ in range(100):
        if adapter._threads[handle.run_id].pending_approvals:
            break
        await asyncio.sleep(0.01)
    assert adapter._threads[handle.run_id].pending_approvals, (
        "pending_approvals should be non-empty"
    )
    await adapter.cancel(handle)
    # 级联丢弃来自 runtime 自跟踪的 pending 审批集(真实 SDK 无独立 drain API)。
    # The canonical interaction_id is a stable hash of the codex scope/method/interaction id.
    # 本分支的 pending 跟踪同时记录 canonical interaction_id(稳定 hash)
    # 与 request.call_id(用于按 call_id 的 resolve 匹配/级联)。
    dropped = adapter.last_cancel_dropped_approvals
    # Verify the dropped ids match the InteractionRequested event's ids.
    requested_events = [
        event
        for event in events
        if hasattr(event, "event_type") and event.event_type == "interaction.requested"
    ]
    assert len(requested_events) >= 1
    expected_id = requested_events[0].interaction_id
    assert dropped == {expected_id, "call-1"}
    await asyncio.wait_for(consume, timeout=2)


@pytest.mark.asyncio
async def test_pause_interrupts_turn_but_keeps_thread_resumable():
    class _PausableCodex(_ControllableCodex):
        def run_turn(self, thread_id, prompt, *, config=None):
            async def gen():
                yield _turn_started(thread_id)
                await self._release.wait()
                yield _item_started(thread_id, "m1")
                yield _item_completed(thread_id, "m1", text="done")
                yield _turn_completed(thread_id)

            return gen()

    client = _PausableCodex()
    adapter = CodexRuntimeAdapter(client)
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
    events: list = []
    consume = asyncio.create_task(_run_stream(adapter, handle, events))
    await asyncio.sleep(0.05)

    assert await adapter.pause(handle) is PauseResult.PAUSED_ACTIVE_TURN
    await asyncio.wait_for(consume, timeout=2)
    assert handle.run_id not in adapter._do_not_persist
    interrupted = next(event for event in events if isinstance(event, RunInterrupted))
    # Canonical RunInterrupted carries status="interrupted"; the "paused"
    # distinction is encoded in the reason field by the adapter.
    assert interrupted.status == "interrupted"

    resumed = await adapter.resume(
        handle,
        ResumeTarget(kind="thread_id", id=handle.run_id),
        ResumePayload(kind="free_text", data="继续"),
    )
    assert resumed.run_id == handle.run_id
    assert client.resumed_threads == [handle.run_id]


@pytest.mark.asyncio
async def test_pause_interrupts_active_goal_and_resume_restarts_same_objective():
    class _PausableGoalCodex(_ControllableCodex):
        def __init__(self) -> None:
            super().__init__(block=False)
            self.goal_calls: list[tuple[str, str]] = []
            self.paused_goals: list[str] = []

        def run_goal(self, thread_id, objective, *, config=None):
            self.goal_calls.append((thread_id, objective))

            async def gen():
                yield _turn_started(thread_id)
                await self._release.wait()
                yield _item_started(thread_id, "m1")
                yield _item_completed(thread_id, "m1", text="done")
                yield _turn_completed(thread_id)

            return gen()

        async def pause_goal(self, thread_id: str) -> bool:
            self.paused_goals.append(thread_id)
            self._release.set()
            return True

    client = _PausableGoalCodex()
    adapter = CodexRuntimeAdapter(client)
    handle = await adapter.start(
        StartRequest(
            input="开始",
            user_id="u",
            session_id="s",
            config={"goal_objective": "完成交互重构", "ephemeral": False},
        )
    )
    events: list = []
    consume = asyncio.create_task(_run_stream(adapter, handle, events))
    await asyncio.sleep(0)

    assert await adapter.pause(handle) is PauseResult.PAUSED_ACTIVE_TURN
    await asyncio.wait_for(consume, timeout=2)
    assert client.paused_goals == [handle.run_id]
    # Canonical RunInterrupted carries status="interrupted" (not "paused").
    assert isinstance(
        next(event for event in events if isinstance(event, RunInterrupted)),
        RunInterrupted,
    )

    resumed = await adapter.resume(
        handle,
        ResumeTarget(kind="thread_id", id=handle.run_id),
        ResumePayload(kind="free_text", data="继续"),
    )
    client._release.set()
    resumed_events = [event async for event in adapter.stream(resumed)]

    assert client.goal_calls == [
        (handle.run_id, "完成交互重构"),
        (handle.run_id, "完成交互重构"),
    ]
    assert isinstance(resumed_events[-1], RunCompleted)


@pytest.mark.asyncio
async def test_submit_resolves_live_codex_approval_without_restarting_turn():
    client = _ControllableCodex(block=False)
    adapter = CodexRuntimeAdapter(client)
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))

    await adapter.submit(
        handle,
        ResumePayload(kind="approval_decision", call_id="call-1", data={"decision": "approve"}),
    )

    assert client.resolved_approvals == [("call-1", "approve")]


@pytest.mark.asyncio
async def test_sdk_approval_bridge_waits_for_explicit_ui_decision():
    from ksadk.codex.client import AsyncCodexClient

    client = AsyncCodexClient.__new__(AsyncCodexClient)
    client._approval_queues = {"thread-1": queue.Queue()}
    client._pending_approvals = {}
    client._approval_lock = threading.Lock()
    result: dict[str, object] = {}

    def request() -> None:
        result.update(
            client._handle_approval_request(
                "item/commandExecution/requestApproval",
                {"threadId": "thread-1", "itemId": "approval-1", "command": "git status"},
            )
        )

    worker = threading.Thread(target=request)
    worker.start()
    approval_event = await asyncio.to_thread(client._approval_queues["thread-1"].get)
    # P0-2：bridge 下发原生 requestApproval JSON-RPC 消息（mapper 只认原生方法）。
    assert approval_event["method"] == "item/commandExecution/requestApproval"
    assert approval_event["params"]["command"] == "git status"
    assert approval_event["id"] == "approval-1"
    assert worker.is_alive()

    assert await client.resolve_approval("approval-1", "approve_session") is True
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert result == {"decision": "acceptForSession"}
    # resolve 后同队列下发 JSON-RPC response（原 id 闭环）。
    response_event = await asyncio.to_thread(client._approval_queues["thread-1"].get)
    assert response_event == {"id": "approval-1", "result": {"decision": "acceptForSession"}}


@pytest.mark.asyncio
async def test_sdk_request_user_input_bridge_waits_for_structured_answers():
    from ksadk.codex.client import AsyncCodexClient

    client = AsyncCodexClient.__new__(AsyncCodexClient)
    client._approval_queues = {"thread-1": queue.Queue()}
    client._pending_approvals = {}
    client._pending_interactions = {}
    client._approval_lock = threading.Lock()
    result: dict[str, object] = {}

    def request() -> None:
        result.update(
            client._handle_server_request(
                "item/tool/requestUserInput",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "question-1",
                    "isBlocking": True,
                    "questions": [
                        {
                            "id": "scope",
                            "header": "检查范围",
                            "question": "需要检查哪些部分？",
                            "isOther": True,
                            "isMultiSelect": True,
                            "options": [
                                {"label": "前端", "description": "只检查 React。"},
                                {"label": "全栈", "description": "同时检查服务端。"},
                            ],
                        }
                    ],
                },
            )
        )

    worker = threading.Thread(target=request, daemon=True)
    worker.start()
    interaction_event = await asyncio.to_thread(client._approval_queues["thread-1"].get, True, 1)
    try:
        assert interaction_event["method"] == "item/tool/requestUserInput"
        assert interaction_event["id"] == "question-1"
        question = interaction_event["params"]["questions"][0]
        assert question["isMultiSelect"] is True
        assert question["isOther"] is True
        assert worker.is_alive()
    finally:
        assert (
            await client.resolve_interaction(
                "question-1", {"scope": ["前端", "全栈"], "note": "忽略生成文件"}
            )
            is True
        )
        worker.join(timeout=1)
    assert not worker.is_alive()
    response_event = client._approval_queues["thread-1"].get(timeout=1)
    assert response_event == {"id": "question-1", "result": result}
    assert result == {
        "answers": {
            "scope": {"answers": ["前端", "全栈"]},
            "note": {"answers": ["忽略生成文件"]},
        }
    }


@pytest.mark.asyncio
async def test_codex_runtime_projects_request_user_input_as_a2ui_and_submits_live_answer():
    class _QuestionCodex(_ControllableCodex):
        def run_turn(self, thread_id, prompt, *, config=None):
            async def gen():
                yield {
                    "method": "a2ui/surface",
                    "params": {
                        "surface_id": "input-question-1",
                        "surface": {
                            "components": [
                                {
                                    "id": "form",
                                    "component": "Form",
                                    "props": {"title": "需要你的反馈"},
                                    "children": [
                                        {
                                            "id": "scope",
                                            "component": "MultipleChoice",
                                            "props": {
                                                "name": "scope",
                                                "label": "检查范围",
                                                "options": ["前端", "全栈"],
                                                "allow_other": True,
                                            },
                                        }
                                    ],
                                }
                            ]
                        },
                    },
                }
                yield {
                    "method": "a2ui/interaction",
                    "params": {
                        "surface_id": "input-question-1",
                        "interaction_id": "question-1",
                        "kind": "form",
                        "input_schema": {},
                    },
                }

            return gen()

    client = _QuestionCodex(block=False)
    adapter = CodexRuntimeAdapter(client)
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
    events = [event async for event in adapter.stream(handle)]

    surface = next(
        (event for event in events if hasattr(event, "item_kind") and event.item_kind == "data"),
        None,
    )
    interaction = next(
        (event for event in events if isinstance(event, InteractionRequested)),
        None,
    )
    assert surface is not None
    assert surface.source.protocol == "a2ui"
    assert surface.source.metadata.get("surface_id") == "input-question-1"
    assert interaction is not None, [
        (type(event).__name__, getattr(getattr(event, "error", None), "message", None))
        for event in events
    ]
    assert interaction.source.protocol == "a2ui"
    assert interaction.interaction_id == "question-1"

    await adapter.submit(
        handle,
        ResumePayload(
            kind="hitl_answer",
            call_id="question-1",
            data={"decision": "submit", "scope": "全栈"},
        ),
    )
    assert client.resolved_interactions == [("question-1", {"decision": "submit", "scope": "全栈"})]


@pytest.mark.asyncio
async def test_sdk_manual_approval_routes_thread_requests_to_user_reviewer():
    """The pinned SDK's public ApprovalMode omits its native user reviewer."""

    from openai_codex.generated.v2_all import (
        ApprovalsReviewer,
        AskForApprovalValue,
        SandboxMode,
    )

    from ksadk.codex.client import AsyncCodexClient

    captured: dict[str, object] = {}

    class _WireClient:
        async def thread_start(self, params):
            captured["start"] = params
            return SimpleNamespace(thread=SimpleNamespace(id="thread-manual"))

        async def thread_resume(self, thread_id, params):
            captured["resume"] = params
            return SimpleNamespace(thread=SimpleNamespace(id=thread_id))

    class _Codex:
        def __init__(self) -> None:
            self._client = _WireClient()

        async def _ensure_initialized(self) -> None:
            return None

    client = AsyncCodexClient.__new__(AsyncCodexClient)
    client._codex = _Codex()
    client._threads = {}

    thread_id = await client.start_thread(
        {
            "approval_mode": "manual",
            "sandbox": "workspace-write",
            "cwd": "/tmp/project",
            "ephemeral": True,
        }
    )
    assert thread_id == "thread-manual"
    started = captured["start"]
    assert started.approval_policy.root is AskForApprovalValue.on_request
    assert started.approvals_reviewer is ApprovalsReviewer.user
    assert started.sandbox is SandboxMode.workspace_write

    client._threads.clear()
    resumed_id = await client.resume_thread(
        thread_id,
        {"approval_mode": "manual", "sandbox": "workspace-write"},
    )
    assert resumed_id == thread_id
    resumed = captured["resume"]
    assert resumed.approval_policy.root is AskForApprovalValue.on_request
    assert resumed.approvals_reviewer is ApprovalsReviewer.user


@pytest.mark.asyncio
async def test_manual_approval_mode_reaches_codex_client_lifecycle():
    class _ConfigCodex(_ControllableCodex):
        def __init__(self) -> None:
            super().__init__(block=False)
            self.start_configs: list[dict] = []
            self.resume_configs: list[dict] = []

        async def start_thread(self, config=None) -> str:
            self.start_configs.append(dict(config or {}))
            return await super().start_thread(config)

        async def resume_thread(self, thread_id: str, config=None) -> str:
            self.resume_configs.append(dict(config or {}))
            return await super().resume_thread(thread_id, config)

    client = _ConfigCodex()
    adapter = CodexRuntimeAdapter(client)
    request = StartRequest(
        input="go",
        user_id="u",
        session_id="s",
        config={"approval_mode": "manual", "sandbox": "workspace-write"},
    )

    handle = await adapter.start(request)
    assert client.start_configs[-1]["approval_mode"] == "manual"

    await adapter.resume(
        handle,
        ResumeTarget(kind="thread_id", id=handle.run_id),
        ResumePayload(kind="free_text", data="继续"),
    )
    assert client.resume_configs[-1]["approval_mode"] == "manual"


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
                yield _turn_started(thread_id)
                yield {
                    "method": "item/started",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "turn-1",
                        "item": {
                            "id": "cmd-1",
                            "type": "commandExecution",
                            "command": "sed -n '1,80p' src/demo.py",
                            "cwd": "/workspace",
                            "commandActions": [{"type": "read", "path": "src/demo.py"}],
                            "status": "inProgress",
                        },
                    },
                }
                yield {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "turn-1",
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
                        },
                    },
                }
                yield _turn_completed(thread_id)

            return gen()

    runtime = CodexRuntimeAdapter(_CommandCodex())
    handle = await runtime.start(StartRequest(input="review", user_id="u", session_id="s"))
    events = [event async for event in runtime.stream(handle)]

    # Canonical: tool calls are ItemStarted/ItemCompleted with item_kind="tool_call".
    # The ToolCallContent/ToolResultContent live in the snapshot parts.
    from ksadk.events.content import ToolCallContent, ToolResultContent

    started = next(
        event
        for event in events
        if isinstance(event, ItemStarted) and event.item_kind == "tool_call"
    )
    completed = next(
        event
        for event in events
        if isinstance(event, ItemCompleted) and event.item_kind == "tool_call"
    )
    call_part = started.initial.parts[0]
    assert isinstance(call_part, ToolCallContent)
    assert call_part.call_id == "cmd-1"
    assert call_part.name == "codex.command"
    assert call_part.arguments == {
        "command": "sed -n '1,80p' src/demo.py",
        "cwd": "/workspace",
        "commandActions": [{"type": "read", "path": "src/demo.py"}],
    }
    result_part = completed.snapshot.parts[1]
    assert isinstance(result_part, ToolResultContent)
    assert result_part.call_id == "cmd-1"
    assert result_part.result["status"] == "completed"
    assert result_part.result["exit_code"] == 0
    assert result_part.result["duration_ms"] == 12
    assert result_part.result["output"] == "def divide(a, b): ..."


async def test_mcp_tool_call_is_projected_as_tool_events():
    class _McpCodex(_ControllableCodex):
        def __init__(self) -> None:
            super().__init__(block=False)

        def run_turn(self, thread_id, prompt, *, config=None):
            async def gen():
                yield _turn_started(thread_id)
                yield {
                    "method": "item/started",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "turn-1",
                        "item": {
                            "id": "mcp-1",
                            "type": "mcpToolCall",
                            "server": "metaso-inner",
                            "tool": "metaso_web_search",
                            "arguments": {"q": "金山云 股价"},
                            "status": "inProgress",
                        },
                    },
                }
                yield {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "turn-1",
                        "item": {
                            "id": "mcp-1",
                            "type": "mcpToolCall",
                            "server": "metaso-inner",
                            "tool": "metaso_web_search",
                            "arguments": {"q": "金山云 股价"},
                            "status": "completed",
                            "durationMs": 640,
                            "result": {
                                "content": [
                                    {"type": "text", "text": "搜索结果第一条"},
                                    {"type": "text", "text": "搜索结果第二条"},
                                ]
                            },
                        },
                    },
                }
                yield _turn_completed(thread_id)

            return gen()

    runtime = CodexRuntimeAdapter(_McpCodex())
    handle = await runtime.start(StartRequest(input="查股价", user_id="u", session_id="s"))
    events = [event async for event in runtime.stream(handle)]

    from ksadk.events.content import ToolCallContent, ToolResultContent

    started = next(
        event
        for event in events
        if isinstance(event, ItemStarted) and event.item_kind == "tool_call"
    )
    completed = next(
        event
        for event in events
        if isinstance(event, ItemCompleted) and event.item_kind == "tool_call"
    )
    call_part = started.initial.parts[0]
    assert isinstance(call_part, ToolCallContent)
    assert call_part.call_id == "mcp-1"
    assert call_part.name == "mcp.metaso-inner.metaso_web_search"
    # Canonical: arguments holds the raw MCP arguments directly (not wrapped).
    assert call_part.arguments == {"q": "金山云 股价"}
    result_part = completed.snapshot.parts[1]
    assert isinstance(result_part, ToolResultContent)
    assert result_part.call_id == "mcp-1"
    assert result_part.result["status"] == "completed"
    assert result_part.result["duration_ms"] == 640
    # Canonical: MCP result.content is passed through as-is (not extracted to
    # a single "output" string like the v1 adapter did).
    content = result_part.result["content"]
    assert content[0]["text"] == "搜索结果第一条"
    assert content[1]["text"] == "搜索结果第二条"


async def test_mcp_tool_call_error_is_surfaced_in_tool_end_event():
    class _McpErrorCodex(_ControllableCodex):
        def __init__(self) -> None:
            super().__init__(block=False)

        def run_turn(self, thread_id, prompt, *, config=None):
            async def gen():
                yield _turn_started(thread_id)
                yield {
                    "method": "item/started",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "turn-1",
                        "item": {
                            "id": "mcp-2",
                            "type": "mcpToolCall",
                            "server": "metaso-inner",
                            "tool": "metaso_web_search",
                            "arguments": {"q": "x"},
                            "status": "inProgress",
                        },
                    },
                }
                yield {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "turn-1",
                        "item": {
                            "id": "mcp-2",
                            "type": "mcpToolCall",
                            "server": "metaso-inner",
                            "tool": "metaso_web_search",
                            "arguments": {"q": "x"},
                            "status": "failed",
                            "durationMs": 88,
                            "error": {"message": "upstream timeout"},
                        },
                    },
                }
                yield _turn_completed(thread_id)

            return gen()

    runtime = CodexRuntimeAdapter(_McpErrorCodex())
    handle = await runtime.start(StartRequest(input="x", user_id="u", session_id="s"))
    events = [event async for event in runtime.stream(handle)]

    # Canonical: failed MCP tool is projected as ItemFailed + ItemUpdated(replace).

    failed = next(event for event in events if isinstance(event, ItemFailed))
    assert failed.item_kind == "tool_call"
    # Canonical: ItemFailed carries a generic error code/message.
    # The specific "upstream timeout" is in the snapshot correction, not the
    # ItemFailed error message (which is "Codex mcpToolCall failed").
    assert failed.error.code == "codex_mcp_tool_failed"


def test_async_client_keeps_codex_usage_and_turn_timing_notifications():
    """Break caught: exact SDK usage/timing is discarded before RuntimeAdapter sees it."""

    from ksadk.codex.client import AsyncCodexClient

    class _Payload:
        def __init__(self, value):
            self.value = value

        def model_dump(self, *, mode, **kwargs):
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
    ] == [{"method": method, "params": params} for method, params in fixtures]


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
                        "threadId": thread_id,
                        "turn": {"id": "turn-1", "status": "inProgress"},
                    },
                }
                yield {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "turn-1",
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
                        "threadId": thread_id,
                        "turn": {
                            "id": "turn-1",
                            "status": "completed",
                            "items": [],
                        },
                    },
                }

            return gen()

    runtime = CodexRuntimeAdapter(_MetricsCodex())
    handle = await runtime.start(StartRequest(input="review", user_id="u", session_id="s"))
    events = [event async for event in runtime.stream(handle)]

    usage_events = [event for event in events if isinstance(event, UsageReported)]
    completed = next(event for event in events if isinstance(event, RunCompleted))
    assert len(usage_events) == 1
    assert usage_events[0].input_tokens == 128
    assert usage_events[0].cached_tokens == 16
    assert usage_events[0].output_tokens == 32
    assert usage_events[0].reasoning_tokens == 8
    assert usage_events[0].total_tokens == 160
    assert completed.status == "completed"


@pytest.mark.asyncio
async def test_codex_runtime_projects_camel_case_app_server_usage():
    """The real App Server JSONL transport emits camelCase token usage fields."""

    class _CamelCaseMetricsCodex(_ControllableCodex):
        def __init__(self) -> None:
            super().__init__(block=False)

        def run_turn(self, thread_id, prompt, *, config=None):
            async def gen():
                yield _turn_started(thread_id)
                yield {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "turn-1",
                        "tokenUsage": {
                            "last": {
                                "inputTokens": 4311,
                                "cachedInputTokens": 4096,
                                "outputTokens": 39,
                                "reasoningOutputTokens": 32,
                                "totalTokens": 4350,
                            }
                        },
                    },
                }
                yield _turn_completed(thread_id)

            return gen()

    runtime = CodexRuntimeAdapter(_CamelCaseMetricsCodex())
    handle = await runtime.start(StartRequest(input="review", user_id="u", session_id="s"))
    events = [event async for event in runtime.stream(handle)]

    usage = next(event for event in events if isinstance(event, UsageReported))
    assert (
        usage.input_tokens,
        usage.cached_tokens,
        usage.output_tokens,
        usage.reasoning_tokens,
        usage.total_tokens,
    ) == (4311, 4096, 39, 32, 4350)


@pytest.mark.asyncio
async def test_codex_error_notification_terminates_run_as_failed():
    """Break caught: a 401 transport error is persisted as an empty successful run."""

    class _AuthFailureCodex(_ControllableCodex):
        def __init__(self) -> None:
            super().__init__(block=False)

        def run_turn(self, thread_id, prompt, *, config=None):
            async def gen():
                yield _turn_started(thread_id)
                yield {
                    "method": "error",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "turn-1",
                        "error": {"message": "401 Unauthorized: Missing bearer authentication"},
                        "willRetry": False,
                    },
                }
                yield {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {
                            "id": "turn-1",
                            "status": "failed",
                            "items": [],
                            "error": {"message": "401 Unauthorized: Missing bearer authentication"},
                        },
                    },
                }

            return gen()

    runtime = CodexRuntimeAdapter(_AuthFailureCodex())
    handle = await runtime.start(StartRequest(input="hello", user_id="u", session_id="s"))
    events = [event async for event in runtime.stream(handle)]

    failed = [event for event in events if isinstance(event, RunFailed)]
    assert len(failed) == 1
    # Canonical: RunFailed carries the error in error.message (not payload["error"]).
    assert "401 Unauthorized" in (failed[0].error.message or "")
    assert not any(isinstance(event, RunCompleted) for event in events)


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


@pytest.mark.asyncio
async def test_native_questions_wait_for_answer_and_resolve_on_same_stream(tmp_path):
    from ksadk.codex.client import AsyncCodexClient
    from ksadk.runtime import RuntimeExecutor, RuntimeLaunchContext, RuntimeRegistry
    from ksadk.studio.contracts import RunStatus
    from ksadk.studio.run_service import StudioRunService, StudioRunSpec
    from ksadk.studio.workspace import Workspace

    bridge = AsyncCodexClient.__new__(AsyncCodexClient)
    bridge._approval_queues = {"codex_thread_1": queue.Queue()}
    bridge._pending_interactions = {}
    bridge._approval_lock = threading.Lock()
    answer = {}

    class NativeQuestionClient(_ControllableCodex):
        def run_turn(self, thread_id, prompt, *, config=None):
            async def gen():
                yield _turn_started(thread_id)
                worker = asyncio.create_task(
                    asyncio.to_thread(
                        bridge._handle_user_input_request,
                        "item/tool/requestUserInput",
                        {
                            "threadId": thread_id,
                            "turnId": "turn-1",
                            "itemId": "native-q",
                            "questions": [
                                {
                                    "id": "scope",
                                    "header": "范围",
                                    "question": "怎么改？",
                                    "isOther": True,
                                    "isMultiSelect": True,
                                    "options": [{"label": "前端", "description": "UI"}],
                                }
                            ],
                        },
                    )
                )
                yield await asyncio.to_thread(bridge._approval_queues[thread_id].get, True, 3)
                answer.update(await worker)
                yield await asyncio.to_thread(bridge._approval_queues[thread_id].get, True, 3)
                yield _turn_completed(thread_id)

            return gen()

        async def resolve_interaction(self, interaction_id, data):
            return await bridge.resolve_interaction(interaction_id, data)

    registry = RuntimeRegistry()
    registry.register("codex", lambda _: CodexRuntimeAdapter(NativeQuestionClient(block=False)))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="codex", project_dir=tmp_path),
        build_id="b",
        agent_id="a",
    )
    task = asyncio.create_task(service.run(spec, "ask", session_id="s"))
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            runs = service.event_store.list_runs(session_id="s")
            if runs and runs[0].status == RunStatus.WAITING_INPUT:
                break
        assert not task.done(), task.result() if task.done() else None
        record = runs[0]
        assert record.status == RunStatus.WAITING_INPUT
        question = next(
            e for e in service.event_store.events(record.id) if e.type == "a2ui.interaction"
        )
        assert question.data["inputSchema"]["properties"]["scope"]["type"] == "array"
        await service.submit_interaction(
            record.id,
            question.data["interactionId"],
            name="submit",
            data={"scope": ["前端", "请改成 echo 你好"]},
            expected_revision=1,
            idempotency_key="answer",
        )
        completed = await asyncio.wait_for(task, 3)
        assert completed.status == RunStatus.COMPLETED, completed.error
        assert answer == {"answers": {"scope": {"answers": ["前端", "请改成 echo 你好"]}}}
    finally:
        await bridge.resolve_interaction("native-q", {})
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision,expected",
    [
        ("submit", {"action": "accept", "content": {"allowed": True}}),
        ("cancel", {"action": "cancel"}),
        ("skip", {"action": "decline"}),
    ],
)
async def test_mcp_elicitation_reaches_ui_and_preserves_form_response(decision, expected):
    from ksadk.codex.client import AsyncCodexClient

    client = AsyncCodexClient.__new__(AsyncCodexClient)
    client._approval_queues = {"thread-1": queue.Queue()}
    client._pending_interactions = {}
    client._approval_lock = threading.Lock()
    result = {}
    worker = threading.Thread(
        target=lambda: result.update(
            client._handle_server_request(
                "mcpServer/elicitation/request",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "serverName": "figma",
                    "mode": "form",
                    "message": "Allow font inspection?",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"allowed": {"type": "boolean"}},
                    },
                },
            )
        ),
        daemon=True,
    )
    worker.start()
    try:
        event = await asyncio.to_thread(client._approval_queues["thread-1"].get, True, 0.3)
        assert worker.is_alive(), "MCP approval was answered without showing UI"
        assert event["method"] == "mcpServer/elicitation/request"
        assert await client.resolve_interaction(
            event["id"], {"decision": decision, "allowed": True}
        )
    finally:
        for pending in client._pending_interactions.values():
            pending.resolved.set()
        worker.join(timeout=1)
    assert result == expected


@pytest.mark.parametrize(
    "policy, kind, schema, expected",
    [
        (
            {"sandbox": "full-access", "approval_mode": "deny_all"},
            "mcp_tool_call",
            {"type": "object", "properties": {}},
            "accept",
        ),
        (
            {"sandbox": "full-access", "approval_mode": "manual"},
            "mcp_tool_call",
            {"type": "object", "properties": {}},
            "cancel",
        ),
        (
            {"sandbox": "read-only", "approval_mode": "deny_all"},
            "mcp_tool_call",
            {"type": "object", "properties": {}},
            "cancel",
        ),
        (
            {"sandbox": "full-access", "approval_mode": "deny_all"},
            "oauth",
            {"type": "object", "properties": {}},
            "cancel",
        ),
        (
            {"sandbox": "full-access", "approval_mode": "deny_all"},
            "mcp_tool_call",
            {"type": "object", "properties": {"secret": {"type": "string"}}},
            "cancel",
        ),
    ],
)
def test_full_access_only_accepts_native_mcp_tool_approval(policy, kind, schema, expected):
    from ksadk.codex.client import AsyncCodexClient

    client = AsyncCodexClient.__new__(AsyncCodexClient)
    client._approval_queues = {"thread-1": queue.Queue()} if expected == "accept" else {}
    client._approval_lock = threading.Lock()
    client._thread_approval_configs = {"thread-1": policy}
    result = client._handle_server_request(
        "mcpServer/elicitation/request",
        {
            "threadId": "thread-1",
            "mode": "form",
            "requestedSchema": schema,
            "_meta": {"codex_approval_kind": kind},
        },
    )
    assert result["action"] == expected


@pytest.mark.asyncio
async def test_native_compaction_waits_for_completion_and_returns_last_usage():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from ksadk.codex.client import AsyncCodexClient

    client = AsyncCodexClient.__new__(AsyncCodexClient)
    compact = AsyncMock()
    client._threads = {
        "t": SimpleNamespace(
            compact=compact,
            read=AsyncMock(
                side_effect=[
                    SimpleNamespace(thread=SimpleNamespace(turns=[])),
                    SimpleNamespace(
                        thread=SimpleNamespace(turns=[SimpleNamespace(id="compact-1")])
                    ),
                ]
            ),
        )
    }
    events = [
        {"method": "turn/started", "params": {"threadId": "t", "turn": {"id": "compact-1"}}},
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "t",
                "tokenUsage": {"last": {"totalTokens": 100}, "total": {"totalTokens": 9000}},
            },
        },
        {
            "method": "item/completed",
            "params": {"threadId": "t", "item": {"type": "contextCompaction"}},
        },
        {
            "method": "turn/completed",
            "params": {"threadId": "t", "turn": {"id": "compact-1", "status": "completed"}},
        },
    ]
    read = AsyncMock(side_effect=events)
    client._codex = SimpleNamespace(
        _client=SimpleNamespace(
            next_turn_notification=read,
            register_turn_notifications=Mock(),
            unregister_turn_notifications=Mock(),
        )
    )
    client._notification_to_event_dict = lambda event: event
    result = await client.compact_thread("t")
    compact.assert_awaited_once()
    assert read.await_count == 4
    assert result["last"]["totalTokens"] == 100
