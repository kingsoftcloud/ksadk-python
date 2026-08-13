"""CodexRuntimeAdapter — 非 ADK 体系的第三验证样本 (goal-09)。

对执行生命周期负责(不做 veadk 式薄桥接),后端能力面对齐 ``openai-codex`` SDK 真实线程模型
(``thread_start``/``thread.turn``/``handle.stream``/``handle.interrupt``/``thread_resume``):

- **cancel 状态机**(不薄委托给上层):活跃 turn → ``client.interrupt_active_turn``
  (真实 SDK ``handle.interrupt``,终止当前 turn 执行;thread 由 codex 后端托管,无"杀进程"
  概念);无活跃 turn → 记 pending 下个 turn 消费;**级联丢弃 pending 工具审批**(runtime
  自跟踪的 pending 集);返回 ``CancelResult`` 枚举;**被中断的 turn 不持久化其 session**
  (避免 resume 捡到写了一半的会话)。
- **phase 翻译**(:mod:`ksadk.codex.phase`):按 itemId 路由 commentary/final_answer delta,
  映射 RuntimeEvent phase,不混入主逻辑。
- **resume 建模为 thread id**(真实 SDK ``thread_resume``),不套 ADK invocation 模型;
  ResumeTarget(thread id)/ResumePayload(工具结果/HITL 回答)分离。

环境约束:read-only sandbox(默认),单轮含一次工具调用即可跑通。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from ksadk.codex.client import CodexClient
from ksadk.codex.phase import CodexPhaseTracker
from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.runtime.adapter import (
    BaseRuntime,
    CancelResult,
    CheckpointCapability,
    CheckpointDescriptor,
    PauseResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)

logger = logging.getLogger(__name__)

_INTERRUPT_DRAIN_TIMEOUT_SECONDS = 5.0


class _CodexAsBaseRuntime(BaseRuntime):
    """把 CodexClient 包装为 BaseRuntime(原生能力面)。"""

    def __init__(self, client: CodexClient) -> None:
        self._client = client
        self.runtime_type = "codex"

    def native_capabilities(self) -> dict[str, Any]:
        return {
            "Framework": "codex",
            "cancel": "thread",
            "pause": "interrupt_then_resume_thread",
            "resume": "thread_id",
            "live_interaction": True,
        }


@dataclass
class _CodexThread:
    thread_id: str
    turn_id: Optional[str] = None
    streaming: bool = False
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    pending_approvals: set[str] = field(default_factory=set)
    done: bool = False
    interrupted: bool = False
    paused: bool = False
    started_at: int | None = None
    completed_at: int | None = None
    duration_ms: int | None = None
    goal_mode: bool = False


class CodexRuntimeAdapter(RuntimeAdapter):
    """Codex 的 RuntimeAdapter(重托管)。"""

    def __init__(
        self,
        client: CodexClient,
        *,
        sandbox_read_only: bool = True,
        turn_timeout_seconds: Optional[float] = None,
    ) -> None:
        super().__init__(_CodexAsBaseRuntime(client))
        self._client = client
        self._sandbox_read_only = sandbox_read_only
        self._turn_timeout_seconds = turn_timeout_seconds
        self._threads: dict[str, _CodexThread] = {}
        self._requests: dict[str, StartRequest] = {}
        self._known_threads: set[str] = set()
        self._pending_cancels: set[str] = set()
        # 被杀/interrupt 的 session 不持久化(goal-09 契约 1)。
        self._do_not_persist: set[str] = set()
        # 可观测:最近一次 cancel 级联丢弃的审批集(contract test 断言用)。
        self.last_cancel_dropped_approvals: set[str] = set()
        self._seq = 0

    # ---- 六动词 ----

    async def start(self, request: StartRequest) -> RunHandle:
        # 新 thread 由后端分配真实 thread_id(thread_start);metadata 携带的 thread_id
        # 表示接入既有 thread(resume 语义,run_turn 时按 resume 接入)。
        provided = request.metadata.get("thread_id")
        if provided:
            thread_id = str(provided)
        else:
            # 把 model + base_instructions 传给 codex thread(配置契约,见 plan C)
            thread_config: dict[str, Any] = {"sandbox_read_only": self._sandbox_read_only}
            if request.config:
                for key in ("sandbox", "approval_mode", "summary", "ephemeral"):
                    value = request.config.get(key)
                    if key == "ephemeral" and value is not None:
                        thread_config[key] = bool(value)
                    elif value:
                        thread_config[key] = value
            if request.model:
                thread_config["model"] = request.model
            base_instructions = request.config.get("base_instructions")
            if base_instructions:
                thread_config["base_instructions"] = base_instructions
            cwd = request.config.get("cwd")
            if cwd:
                thread_config["cwd"] = str(cwd)
            thread_id = await self._client.start_thread(thread_config)
        self._known_threads.add(thread_id)
        thread = _CodexThread(thread_id=thread_id)
        thread.__dict__["_start_request"] = request
        self._threads[thread_id] = thread
        self._requests[thread_id] = request
        return RunHandle(
            run_id=thread_id,
            session_id=request.session_id,
            runtime_type="codex",
            native_ref={"thread_id": thread_id, "user_id": request.user_id},
        )

    def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        return self._stream_events(handle)

    async def cancel(self, handle: RunHandle) -> CancelResult:
        thread_id = handle.run_id
        thread = self._threads.get(thread_id)
        is_active = thread is not None and not thread.done and thread.streaming
        if not is_active:
            if thread_id in self._known_threads:
                self._pending_cancels.add(thread_id)
                return CancelResult.PENDING_CANCEL_RECORDED
            return CancelResult.NOT_RUNNING
        assert thread is not None
        try:
            # 级联丢弃 pending 工具审批(快照 runtime 自跟踪的 pending 集,供观测)。
            # 真实 SDK 无独立 drain API:interrupt 后 turn 停止,pending 审批随之失效。
            self.last_cancel_dropped_approvals = set(thread.pending_approvals)
            thread.pending_approvals.clear()
            # 真实中断:handle.interrupt() 停活跃 turn(真实 SDK 机制;无"杀进程"概念,
            # thread 由 codex 后端托管,interrupt 即终止当前 turn 的执行)。
            # 先唤醒 Studio 的 stream；等待 request_user_input 时，中断 RPC
            # 可能要等 turn 状态前进，否则取消接口与流会互相等待。
            thread.interrupt_event.set()
            interrupted = (
                await self._client.cancel_goal(thread.thread_id)
                if thread.goal_mode
                else await self._client.interrupt_active_turn(thread.thread_id)
            )
            if not interrupted:
                # 无活跃 handle 可 interrupt(竞态:turn 刚好结束)→ 视为未在运行。
                return CancelResult.NOT_RUNNING
            # 被中断的 session 不持久化(goal-09 契约 1)。
            self._do_not_persist.add(thread_id)
            thread.done = True
            self._threads.pop(thread_id, None)
            self._requests.pop(thread_id, None)
            self._pending_cancels.discard(thread_id)
            return CancelResult.INTERRUPTED_ACTIVE_TURN
        except Exception:  # noqa: BLE001
            logger.exception("codex cancel thread %s 失败", thread_id)
            return CancelResult.FAILED

    async def pause(self, handle: RunHandle) -> PauseResult:
        thread = self._threads.get(handle.run_id)
        if thread is None or thread.done or not thread.streaming:
            return PauseResult.NOT_RUNNING
        if thread.pending_approvals:
            # A blocked native approval must be answered through the interaction
            # card. Interrupting while the SDK reader waits on that approval can
            # deadlock the JSON-RPC command channel.
            return PauseResult.FAILED
        try:
            interrupted = (
                await self._client.pause_goal(thread.thread_id)
                if thread.goal_mode
                else await self._client.interrupt_active_turn(thread.thread_id)
            )
            if not interrupted:
                return PauseResult.NOT_RUNNING
            thread.paused = True
            # ``thread/goal/set(status=paused)`` only prevents the next
            # continuation. Wake the adapter stream as well so the current
            # physical goal turn becomes an honest resumable pause now.
            thread.interrupt_event.set()
            return PauseResult.PAUSED_ACTIVE_TURN
        except Exception:  # noqa: BLE001
            logger.exception("codex pause thread %s 失败", handle.run_id)
            return PauseResult.FAILED

    async def submit(self, handle: RunHandle, payload: ResumePayload) -> None:
        if not payload.call_id:
            raise ValueError("Codex live submit requires call_id")
        raw = payload.data if isinstance(payload.data, dict) else {"decision": payload.data}
        if payload.kind == "approval_decision":
            decision = str(raw.get("decision") or raw.get("name") or "")
            resolved = await self._client.resolve_approval(payload.call_id, decision)
        elif payload.kind == "hitl_answer":
            resolved = await self._client.resolve_interaction(
                payload.call_id,
                {key: value for key, value in raw.items() if key != "decision"},
            )
        else:
            raise ValueError("Codex live submit requires approval_decision or hitl_answer")
        if not resolved:
            raise ValueError(f"interaction {payload.call_id!r} is not pending")
        thread = self._threads.get(handle.run_id)
        if thread is not None:
            thread.pending_approvals.discard(payload.call_id)

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: Optional[ResumePayload],
    ) -> RunHandle:
        # resume 用 thread id 语义(resume_thread_id),不套 ADK invocation 模型。
        if target.kind != "thread_id":
            raise ValueError(
                f"CodexRuntimeAdapter resume 仅支持 thread_id 目标,得到 {target.kind!r}"
            )
        if handle.run_id in self._do_not_persist:
            raise ValueError(f"thread {handle.run_id} 已被中断/杀进程,不持久化,不可 resume")
        self._pending_cancels.discard(handle.run_id)
        self._known_threads.add(target.id)
        thread = _CodexThread(thread_id=target.id)
        thread.__dict__["_resume"] = {"target": target, "payload": payload}
        request = self._requests.get(handle.run_id)
        if request is not None:
            thread.__dict__["_request_config"] = request
        if request is not None and str(request.config.get("goal_objective") or "").strip():
            # Restart the same native Goal operation on the persisted thread;
            # its transcript remains in Codex, while the objective/config are
            # needed to resume Goal rather than issuing a normal text turn.
            thread.goal_mode = True
        self._threads[handle.run_id] = thread
        # 真实恢复:thread_resume 接入后端既有 thread(thread_id 语义,不套 ADK invocation)。
        resume_config: dict[str, Any] = {"sandbox_read_only": self._sandbox_read_only}
        if request is not None and request.config:
            for key in ("sandbox", "approval_mode"):
                value = request.config.get(key)
                if value:
                    resume_config[key] = value
        await self._client.resume_thread(target.id, resume_config)
        handle.native_ref["thread_id"] = target.id
        handle.native_ref["resume_thread_id"] = target.id
        handle.native_ref["resume_payload"] = payload.data if payload else None
        # 与 ADK/LangGraph 一致的 resume_input 结构(供共用 contract test 断言)。
        handle.native_ref["resume_input"] = {
            "type": "codex.resume_thread",
            "thread_id": target.id,
            "payload": payload.data if payload else None,
            "payload_kind": payload.kind if payload else None,
            "call_id": payload.call_id if payload else None,
        }
        return handle

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        return CheckpointDescriptor(
            checkpoint_id=str(handle.native_ref.get("thread_id") or handle.run_id),
            invocation_id=handle.run_id,
            capability=CheckpointCapability(
                supported=True,
                granularity="snapshot",
                rollback_scope="turn",
                fork_supported=True,
                durable=False,
                shared_across_pods=False,
                reason="Codex resume/fork by thread id",
            ),
            ref={"thread_id": handle.native_ref.get("thread_id")},
        )

    async def close(self, handle: RunHandle) -> None:
        thread = self._threads.pop(handle.run_id, None)
        self._requests.pop(handle.run_id, None)
        if thread is not None:
            thread.interrupt_event.set()
        try:
            active_thread_id = thread.thread_id if thread is not None else handle.run_id
            await self._client.interrupt_active_turn(active_thread_id)
        finally:
            # AsyncCodex.close owns terminate/wait/kill for the app-server child.
            await self._client.close()
            self._do_not_persist.add(handle.run_id)
            self._known_threads.discard(handle.run_id)
            self._pending_cancels.discard(handle.run_id)

    # ---- stream → RuntimeEvent(phase 翻译 + 中断竞速) ----

    def _next_thread_id(self) -> str:
        self._seq += 1
        return f"codex_thread_{self._seq}"

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _stream_events(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        thread = self._threads.get(handle.run_id)
        if thread is None:
            thread = _CodexThread(thread_id=handle.run_id)
            self._threads[handle.run_id] = thread

        if handle.run_id in self._pending_cancels:
            self._pending_cancels.discard(handle.run_id)
            yield self._event(
                handle,
                EventType.RUN_CANCELED,
                {
                    "status": "cancelled",
                    "cancel_result": CancelResult.PENDING_CANCEL_RECORDED.value,
                },
            )
            return

        yield self._event(handle, EventType.RUN_STARTED, {"status": "in_progress"})
        tracker = CodexPhaseTracker()
        request = thread.__dict__.get("_start_request")
        resume_state = thread.__dict__.get("_resume")
        if request is not None:
            prompt = _request_prompt(request)
        elif resume_state is not None:
            prompt = _resume_prompt(resume_state.get("payload"))
        else:
            prompt = ""
        run_input = _build_run_input(request, prompt)
        thread.streaming = True
        thread.turn_id = thread.turn_id or f"turn_{thread.thread_id}"
        try:
            async for event in self._map_codex_stream(handle, thread, tracker, run_input):
                yield event
            # 正常结束(非 interrupt):补 RUN_COMPLETED(AGUI 投射器据此发 RunFinished success)
            if not thread.interrupted:
                completed_payload: dict[str, Any] = {
                    "status": "completed",
                    "source": "codex",
                }
                if thread.started_at is not None:
                    completed_payload["started_at"] = thread.started_at
                if thread.completed_at is not None:
                    completed_payload["completed_at"] = thread.completed_at
                if thread.duration_ms is not None:
                    completed_payload["duration_ms"] = thread.duration_ms
                yield self._event(handle, EventType.RUN_COMPLETED, completed_payload)
        except asyncio.CancelledError:
            thread.interrupted = True
            self._do_not_persist.add(handle.run_id)
            await self._client.interrupt_active_turn(thread.thread_id)
            raise
        except TimeoutError:
            self.last_cancel_dropped_approvals = set(thread.pending_approvals)
            thread.pending_approvals.clear()
            self._do_not_persist.add(handle.run_id)
            # Closing the SDK transport terminates and waits for the app-server
            # child even when the stream is stuck between notifications.
            await self._client.close()
            yield self._event(
                handle,
                EventType.RUN_FAILED,
                {"status": "failed", "error": "codex turn timed out"},
            )
        except Exception as exc:  # noqa: BLE001  通用兜底:任何异常都发 RUN_FAILED
            self._do_not_persist.add(handle.run_id)
            yield self._event(
                handle,
                EventType.RUN_FAILED,
                {"status": "failed", "error": str(exc)},
            )
        finally:
            thread.streaming = False
            thread.done = True
            self._threads.pop(handle.run_id, None)

    async def _map_codex_stream(
        self,
        handle: RunHandle,
        thread: _CodexThread,
        tracker: CodexPhaseTracker,
        prompt: Any,
    ) -> AsyncIterator[RuntimeEvent]:
        request = thread.__dict__.get("_start_request") or thread.__dict__.get("_request_config")
        run_config: dict[str, Any] = {"sandbox_read_only": self._sandbox_read_only}
        if request is not None and request.config:
            for key in ("sandbox", "approval_mode", "summary", "collaboration_mode"):
                value = request.config.get(key)
                if key == "approval_mode" and value == "manual":
                    continue
                if value:
                    run_config[key] = value
        if request is not None and request.model:
            run_config["model"] = request.model
        goal_objective = str(
            request.config.get("goal_objective") or ""
            if request is not None and request.config
            else ""
        ).strip()
        if goal_objective:
            thread.goal_mode = True
            codex_gen = self._client.run_goal(
                thread.thread_id,
                goal_objective,
                config=run_config,
            )
        else:
            codex_gen = self._client.run_turn(
                thread.thread_id,
                prompt,
                config=run_config,
            )
        deadline = (
            asyncio.get_running_loop().time() + self._turn_timeout_seconds
            if self._turn_timeout_seconds is not None
            else None
        )
        chunk_task: asyncio.Task[Any] | None = None
        interrupt_task: asyncio.Task[bool] | None = None
        try:
            while True:
                chunk_task = asyncio.ensure_future(_anext_or_stop(codex_gen))
                interrupt_task = asyncio.ensure_future(thread.interrupt_event.wait())
                remaining = (
                    max(0.0, deadline - asyncio.get_running_loop().time())
                    if deadline is not None
                    else None
                )
                done, pending = await asyncio.wait(
                    {chunk_task, interrupt_task},
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    # Keep handle.stream's turn queue registered until transport
                    # close calls SDK MessageRouter.fail_all. Cancelling the
                    # asyncio.to_thread waiter first would strand queue.get in
                    # the executor and hang interpreter shutdown.
                    interrupt_task.cancel()
                    await self._client.close()
                    await asyncio.gather(chunk_task, interrupt_task, return_exceptions=True)
                    raise TimeoutError("codex turn timed out")
                if interrupt_task in done:
                    # ``openai-codex`` implements ``next_turn_notification``
                    # with ``asyncio.to_thread(queue.get)``.  Cancelling that
                    # awaiter unregisters its queue while the worker thread is
                    # still blocked, so neither a later transport close nor
                    # event-loop teardown can wake it.  After interrupting a
                    # turn, keep the queue registered and drain the SDK's
                    # terminal notifications instead.  If the backend fails to
                    # finish promptly, close the transport *before* awaiting
                    # the waiter so MessageRouter.fail_all can release it.
                    drain_deadline = (
                        asyncio.get_running_loop().time()
                        + _INTERRUPT_DRAIN_TIMEOUT_SECONDS
                    )
                    while True:
                        drain_remaining = max(
                            0.0,
                            drain_deadline - asyncio.get_running_loop().time(),
                        )
                        drained, _ = await asyncio.wait(
                            {chunk_task},
                            timeout=drain_remaining,
                        )
                        if not drained:
                            await self._client.close()
                            await asyncio.gather(chunk_task, return_exceptions=True)
                            break
                        try:
                            drained_chunk = chunk_task.result()
                        except Exception:  # noqa: BLE001
                            break
                        if drained_chunk is _STREAM_STOP:
                            break
                        chunk_task = asyncio.ensure_future(_anext_or_stop(codex_gen))
                    chunk_task = None
                    thread.interrupted = True
                    # AGUI 投射器对 RUN_INTERRUPTED 无兜底,必须显式发,否则 raise
                    yield self._event(
                        handle,
                        EventType.RUN_INTERRUPTED,
                        {
                            "status": "paused" if thread.paused else "interrupted",
                            "reason": "user_pause" if thread.paused else "runtime_interrupt",
                        },
                    )
                    return
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                chunk = chunk_task.result()
                if chunk is _STREAM_STOP:
                    return
                event = self._codex_chunk_to_event(handle, thread, tracker, chunk)
                if event is not None:
                    yield event
        finally:
            waiter_tasks = [task for task in (chunk_task, interrupt_task) if task is not None]
            for task in waiter_tasks:
                if not task.done():
                    task.cancel()
            if waiter_tasks:
                await asyncio.gather(*waiter_tasks, return_exceptions=True)
            aclose = getattr(codex_gen, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:  # noqa: BLE001
                    pass

    def _codex_chunk_to_event(
        self,
        handle: RunHandle,
        thread: _CodexThread,
        tracker: CodexPhaseTracker,
        chunk: dict[str, Any],
    ) -> Optional[RuntimeEvent]:
        if not isinstance(chunk, dict):
            return None
        method = str(chunk.get("method") or chunk.get("type") or "")
        params = chunk.get("params") or chunk

        if method == "error":
            raw_error = params.get("error") if isinstance(params, dict) else None
            error = raw_error if isinstance(raw_error, dict) else {}
            message = str(
                error.get("message")
                or (params.get("message") if isinstance(params, dict) else "")
                or raw_error
                or "Codex runtime transport failed"
            )
            if not bool(params.get("will_retry") or params.get("willRetry")) or "401" in message:
                raise RuntimeError(message)
            return None

        if method == "thread/tokenUsage/updated":
            token_usage = params.get("token_usage") or params.get("tokenUsage") or {}
            last = token_usage.get("last") if isinstance(token_usage, dict) else {}
            if not isinstance(last, dict):
                last = {}
            return self._event(
                handle,
                EventType.USAGE_REPORTED,
                {
                    "input_tokens": int(last.get("input_tokens", last.get("inputTokens", 0)) or 0),
                    "cached_tokens": int(
                        last.get("cached_input_tokens", last.get("cachedInputTokens", 0)) or 0
                    ),
                    "output_tokens": int(
                        last.get("output_tokens", last.get("outputTokens", 0)) or 0
                    ),
                    "reasoning_tokens": int(
                        last.get(
                            "reasoning_output_tokens",
                            last.get("reasoningOutputTokens", 0),
                        )
                        or 0
                    ),
                    "total_tokens": int(last.get("total_tokens", last.get("totalTokens", 0)) or 0),
                    "source": "codex",
                },
            )
        if method == "thread/goal/updated":
            goal = params.get("goal") if isinstance(params, dict) else {}
            goal = goal if isinstance(goal, dict) else {}
            status = str(goal.get("status") or "").lower()
            if status in {"paused", "blocked", "usage_limited", "budget_limited"}:
                thread.paused = status == "paused"
                thread.interrupted = True
                return self._event(
                    handle,
                    EventType.RUN_INTERRUPTED,
                    {"status": status, "reason": "goal_status", "goal": goal},
                )
            return self._event(
                handle,
                EventType.RUN_PROGRESS,
                {"native_event": "goal.updated", "native_data": goal},
            )
        if method in {"turn/started", "turn/completed"}:
            raw_turn = params.get("turn")
            turn: dict[str, Any] = raw_turn if isinstance(raw_turn, dict) else {}
            started_at = turn.get("started_at", turn.get("startedAt"))
            completed_at = turn.get("completed_at", turn.get("completedAt"))
            duration_ms = turn.get("duration_ms", turn.get("durationMs"))
            if started_at is not None:
                thread.started_at = int(started_at)
            if completed_at is not None:
                thread.completed_at = int(completed_at)
            if duration_ms is not None:
                thread.duration_ms = max(0, int(duration_ms))
            return None

        if method == "a2ui/surface":
            surface_id = str(params.get("surface_id") or params.get("surfaceId") or "")
            return self._event(
                handle,
                EventType.A2UI_SURFACE_BEGIN,
                {
                    "surface_id": surface_id,
                    "surface": params.get("surface")
                    if isinstance(params.get("surface"), dict)
                    else {},
                },
            )
        if method == "a2ui/interaction":
            interaction_id = str(params.get("interaction_id") or params.get("interactionId") or "")
            if interaction_id:
                thread.pending_approvals.add(interaction_id)
            return self._event(
                handle,
                EventType.A2UI_INTERACTION,
                {
                    "surface_id": str(params.get("surface_id") or params.get("surfaceId") or ""),
                    "interaction_id": interaction_id,
                    "kind": str(params.get("kind") or "form"),
                    "input_schema": params.get("input_schema")
                    if isinstance(params.get("input_schema"), dict)
                    else {},
                    "is_blocking": bool(params.get("is_blocking", True)),
                },
            )

        if method == "item/started":
            tracker.observe_item(params)
            item = params.get("item") or params
            if item.get("type") == "commandExecution":
                call_id = str(item.get("id") or "")
                return self._event(
                    handle,
                    EventType.TOOL_CALL_BEGIN,
                    {
                        "call_id": call_id,
                        "name": "codex.command",
                        "args": {
                            "command": str(item.get("command") or ""),
                            "cwd": str(item.get("cwd") or ""),
                            "command_actions": item.get("commandActions")
                            or item.get("command_actions")
                            or [],
                        },
                    },
                )
            if item.get("type") == "mcpToolCall":
                call_id = str(item.get("id") or "")
                server = str(item.get("server") or "")
                tool = str(item.get("tool") or "")
                return self._event(
                    handle,
                    EventType.TOOL_CALL_BEGIN,
                    {
                        "call_id": call_id,
                        "name": f"mcp.{server}.{tool}" if server else f"mcp.{tool}",
                        "args": {
                            "server": server,
                            "tool": tool,
                            "arguments": item.get("arguments"),
                        },
                    },
                )
            return None
        if method == "item/completed":
            item = params.get("item") or params
            item_type = item.get("type")
            if item_type == "commandExecution":
                tracker.forget_item(params)
                call_id = str(item.get("id") or "")
                return self._event(
                    handle,
                    EventType.TOOL_CALL_END,
                    {
                        "call_id": call_id,
                        "name": "codex.command",
                        "result": {
                            "status": str(item.get("status") or "completed"),
                            "exit_code": item.get("exitCode", item.get("exit_code")),
                            "duration_ms": item.get("durationMs", item.get("duration_ms")),
                            "output": str(
                                item.get("aggregatedOutput") or item.get("aggregated_output") or ""
                            ),
                        },
                    },
                )
            if item_type == "mcpToolCall":
                tracker.forget_item(params)
                call_id = str(item.get("id") or "")
                server = str(item.get("server") or "")
                tool = str(item.get("tool") or "")
                raw_result = item.get("result")
                result_obj: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
                raw_error = item.get("error")
                error_obj: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
                output = self._mcp_result_text(result_obj)
                error_message = str(error_obj.get("message") or "")
                if not output and error_message:
                    output = error_message
                return self._event(
                    handle,
                    EventType.TOOL_CALL_END,
                    {
                        "call_id": call_id,
                        "name": f"mcp.{server}.{tool}" if server else f"mcp.{tool}",
                        "result": {
                            "status": str(item.get("status") or "completed"),
                            "duration_ms": item.get("durationMs", item.get("duration_ms")),
                            "output": output,
                            **({"error": error_message} if error_message else {}),
                        },
                    },
                )
            if item_type != "agentMessage":
                tracker.forget_item(params)
                return None
            phase = tracker.runtime_phase_for_item(params)
            tracker.forget_item(params)
            text = str(item.get("text") or "")
            return self._event(
                handle,
                EventType.TEXT_COMPLETED,
                {"text": text},
                phase=phase or "final_answer",
            )
        if "delta" in method or "Delta" in method or method == "item/agentMessage/delta":
            phase = tracker.runtime_phase_for_delta(params)
            delta = str(params.get("delta") or "")
            if not delta:
                return None
            return self._event(
                handle, EventType.TEXT_DELTA, {"text": delta}, phase=phase or "commentary"
            )
        if method == "item/autoApprovalReview/started":
            review_id = str(params.get("review_id") or params.get("reviewId") or "")
            if review_id:
                thread.pending_approvals.add(review_id)
            return None
        if method == "item/autoApprovalReview/completed":
            review_id = str(params.get("review_id") or params.get("reviewId") or "")
            thread.pending_approvals.discard(review_id)
            return None
        if (
            "approval" in method.lower()
            or "requestPermission" in method
            or "approval" in str(chunk.get("type") or "").lower()
        ):
            call_id = str(
                params.get("id") or params.get("call_id") or params.get("requestId") or ""
            )
            if call_id:
                thread.pending_approvals.add(call_id)
            return self._event(
                handle,
                EventType.APPROVAL_REQUESTED,
                {
                    "approval_id": call_id,
                    "call_id": call_id,
                    "kind": str(params.get("kind") or "tool"),
                    "detail": params.get("detail")
                    if isinstance(params.get("detail"), dict)
                    else params,
                },
            )
        return None

    def _event(
        self,
        handle: RunHandle,
        event_type: str,
        payload: dict,
        *,
        phase: Optional[str] = None,
    ) -> RuntimeEvent:
        request = self._requests.get(handle.run_id)
        return RuntimeEvent.create(
            event_type,
            agent_id=str(request.agent_id or "codex") if request is not None else "codex",
            user_id=(
                request.user_id
                if request is not None
                else str(handle.native_ref.get("user_id") or "user")
            ),
            session_id=handle.session_id,
            invocation_id=(
                str(request.metadata.get("invocation_id") or handle.run_id)
                if request is not None
                else handle.run_id
            ),
            seq_id=self._next_seq(),
            phase=phase,
            payload=payload,
        )

    @staticmethod
    def _mcp_result_text(result: dict[str, Any]) -> str:
        """把 MCP result.content 列表提取为可读文本;无文本时退化 structuredContent。"""
        if not result:
            return ""
        content = result.get("content")
        if isinstance(content, list):
            texts = [
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            texts = [text for text in texts if text]
            if texts:
                return "\n".join(texts)
        structured = result.get("structuredContent", result.get("structured_content"))
        if structured is not None:
            return json.dumps(structured, ensure_ascii=False)
        return ""


_STREAM_STOP = object()


async def _anext_or_stop(gen: AsyncIterator[Any]) -> Any:
    try:
        return await gen.__anext__()
    except StopAsyncIteration:
        return _STREAM_STOP


def _resume_prompt(payload: Optional[ResumePayload]) -> Any:
    if payload is None:
        return ""
    if isinstance(payload.data, str):
        return payload.data
    return json.dumps(payload.data, ensure_ascii=False, sort_keys=True)


def _request_prompt(request: StartRequest) -> Any:
    """Render canonical conversation history for a native Codex turn.

    只传 user 消息历史 + 当前 input，不传 assistant 的完整回复（避免 codex 复述历史）。
    assistant 回复用简短摘要代替，仅维持对话结构。
    """
    # A resumed Codex thread already owns its transcript. Re-sending Studio's
    # transport-neutral history would duplicate every prior turn after refresh.
    if str(request.metadata.get("thread_id") or "").strip():
        return request.input

    conversation = request.conversation_preprocessing()
    if conversation is None or not conversation.messages:
        return request.input

    lines: list[str] = []
    for message in conversation.messages:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        role = str(message.get("role") or "user").strip().lower()
        if role == "assistant":
            # 不传完整回复，只标注"已回复"，避免 codex 复述
            summary = content[:80] + ("..." if len(content) > 80 else "")
            lines.append(f"[上一轮已回复: {summary}]")
        elif role == "user":
            lines.append(f"User: {content}")
    return "\n".join(lines) or request.input


def _build_run_input(request: Optional[StartRequest], prompt: Any) -> Any:
    """Compose Codex skills and native text/image/mention turn input."""
    try:
        from openai_codex import ImageInput, LocalImageInput, MentionInput, SkillInput, TextInput
    except ImportError:
        return prompt
    skills: list[SkillInput] = []
    if request is not None:
        raw = request.config.get("skills") if request.config else None
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                path = str(item.get("path") or "").strip()
                if name and path:
                    skills.append(SkillInput(name=name, path=path))
    native_items: list[Any] = []
    raw_input = request.input if request is not None else prompt
    if isinstance(raw_input, list):
        text_replaced = False
        for item in raw_input:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            if kind == "text":
                text = str(
                    prompt
                    if not text_replaced and isinstance(prompt, str)
                    else item.get("text") or ""
                )
                text_replaced = True
                if text:
                    native_items.append(TextInput(text=text))
            elif kind == "image" and item.get("url"):
                native_items.append(ImageInput(url=str(item["url"])))
            elif kind == "localImage" and item.get("path"):
                native_items.append(LocalImageInput(path=str(item["path"])))
            elif kind == "mention" and item.get("path"):
                native_items.append(
                    MentionInput(
                        name=str(item.get("name") or item["path"]),
                        path=str(item["path"]),
                    )
                )
    else:
        text = prompt if isinstance(prompt, str) else str(prompt or "")
        if text:
            native_items.append(TextInput(text=text))
    combined = [*skills, *native_items]
    if not combined:
        return prompt
    if len(combined) == 1 and isinstance(combined[0], TextInput) and not skills:
        return combined[0].text
    return combined


__all__ = ["CodexRuntimeAdapter"]
