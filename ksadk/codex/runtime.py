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
import base64
import binascii
import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ksadk.codex.client import CodexClient
from ksadk.events.adapters.codex import CodexAdapterContext, CodexEventAdapter
from ksadk.events.canonical import (
    ErrorInfo,
    InteractionRequested,
    InteractionResolved,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.identity import stable_event_id, stable_item_id, stable_scope_id
from ksadk.kernel.contracts import RuntimeCapability, RuntimeCapabilityMatrix
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
    continuation_preexisting: bool = False


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
        self._closed = False

    # ---- capability matrix(v1,诚实声明) ----

    def capabilities(self) -> RuntimeCapabilityMatrix:
        """Codex 真实矩阵:thread 级 cancel/pause/resume + 审批 submit + snapshot
        checkpoint 均为后端原生能力;attach/durable_restore 未实现(线程表在本进程,
        attach seam 缺失),steer/inject 无原生通道。
        """

        def _unavailable(reason: str) -> RuntimeCapability:
            return RuntimeCapability(supported=False, mode="unavailable", reason=reason)

        return RuntimeCapabilityMatrix(
            cancel=RuntimeCapability(supported=True, mode="native"),
            pause=RuntimeCapability(supported=True, mode="native"),
            resume=RuntimeCapability(supported=True, mode="native"),
            submit_interaction=RuntimeCapability(supported=True, mode="native"),
            attach=_unavailable("codex_process_local_thread_table"),
            steer=_unavailable("runtime_no_native_steer"),
            inject=_unavailable("runtime_no_native_inject"),
            checkpoint=RuntimeCapability(supported=True, mode="native"),
            durable_restore=_unavailable("codex_durable_restore_requires_attach_seam"),
            goal=RuntimeCapability(supported=True, mode="native"),
            loop=_unavailable("codex_loop_requires_run_control_spec"),
            plan=RuntimeCapability(supported=True, mode="native"),
        )

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
            # AgentKernel creates one adapter/transport per durable turn and
            # closes it after the canonical terminal event.  The next turn
            # therefore resumes the native thread from a new app-server
            # process; an ephemeral Codex thread has no rollout and cannot be
            # resumed across that transport boundary.
            thread_config.setdefault("ephemeral", False)
            thread_id = await self._client.start_thread(thread_config)
        self._known_threads.add(thread_id)
        thread = _CodexThread(
            thread_id=thread_id,
            continuation_preexisting=bool(provided),
        )
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
        thread = _CodexThread(
            thread_id=target.id,
            continuation_preexisting=True,
        )
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
        if self._closed:
            return
        self._closed = True
        thread = self._threads.pop(handle.run_id, None)
        self._requests.pop(handle.run_id, None)
        active = thread is not None and thread.streaming and not thread.done
        if active:
            thread.interrupt_event.set()
        try:
            if active:
                await self._client.interrupt_active_turn(thread.thread_id)
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
            yield self._make_run_canceled(
                handle,
                reason=f"pending_cancel:{CancelResult.PENDING_CANCEL_RECORDED.value}",
            )
            return

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
            async for event in self._map_codex_stream(handle, thread, run_input):
                yield event
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
            yield self._make_run_failed(handle, "codex turn timed out")
        except Exception as exc:  # noqa: BLE001  通用兜底:任何异常都发 RunFailed
            self._do_not_persist.add(handle.run_id)
            yield self._make_run_failed(handle, str(exc))
        finally:
            thread.streaming = False
            thread.done = True
            self._threads.pop(handle.run_id, None)

    async def _map_codex_stream(
        self,
        handle: RunHandle,
        thread: _CodexThread,
        prompt: Any,
    ) -> AsyncIterator[RuntimeEvent]:
        request = thread.__dict__.get("_start_request") or thread.__dict__.get("_request_config")
        adapter = CodexEventAdapter(
            known_thread_ids=(thread.thread_id,)
            if thread.continuation_preexisting
            else (),
        )
        context = CodexAdapterContext(run_id=self._event_run_id(handle))
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
                    # Runtime interrupt (user pause) — adapter doesn't know;
                    # emit canonical RunInterrupted explicitly.
                    yield self._make_run_interrupted(
                        handle,
                        reason="user_pause" if thread.paused else "runtime_interrupt",
                    )
                    return
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                chunk = chunk_task.result()
                if chunk is _STREAM_STOP:
                    return
                # TODO(runtime-event-v2): use real native cursor from chunk if
                # available; fallback to thread:seq for now.
                native_cursor = f"{thread.thread_id}:{self._next_seq()}"
                # autoApprovalReview 不产生 canonical 事件(adapter 静默),但
                # cancel 级联丢弃审批的契约依赖 runtime 的 pending 跟踪。
                chunk_method = (
                    str((chunk or {}).get("method") or "")
                    if isinstance(chunk, dict)
                    else ""
                )
                if chunk_method in {
                    "item/autoApprovalReview/started",
                    "item/autoApprovalReview/completed",
                }:
                    review_params = chunk.get("params") or {}
                    review_id = str(
                        review_params.get("reviewId")
                        or review_params.get("review_id")
                        or ""
                    )
                    if review_id:
                        if chunk_method.endswith("started"):
                            thread.pending_approvals.add(review_id)
                        else:
                            thread.pending_approvals.discard(review_id)
                for event in adapter.map_protocol_message(
                    chunk,
                    context,
                    native_cursor=native_cursor,
                    timestamp=time.time(),
                ):
                    event = self._with_caller_scope(event, request)
                    # 跟踪 pending 审批(cancel 级联丢弃契约依赖该集合)。
                    if isinstance(event, InteractionRequested):
                        if event.interaction_id:
                            thread.pending_approvals.add(event.interaction_id)
                        call_id = getattr(event.request, "call_id", None)
                        if call_id:
                            thread.pending_approvals.add(str(call_id))
                    elif isinstance(event, InteractionResolved):
                        thread.pending_approvals.discard(event.interaction_id)
                        call_id = getattr(event.response, "call_id", None)
                        if call_id:
                            thread.pending_approvals.discard(str(call_id))
                    if isinstance(event, (RunCompleted, RunFailed, RunCanceled)):
                        # The Kernel stops consuming as soon as it persists a
                        # canonical terminal fact, so generator ``finally`` may
                        # not run before worker cleanup calls ``close``. Mark the
                        # native turn terminal before yielding that fact; close
                        # must terminate the transport without sending a stale
                        # turn/interrupt RPC to an already-completed app-server.
                        thread.done = True
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

    # ---- canonical run.* helpers (for runtime-owned lifecycle) ----

    def _event_run_id(self, handle: RunHandle) -> str:
        """事件的 canonical run_id:调用方 invocation_id 优先,退回 thread id。

        ``handle.run_id`` 是 codex 原生 thread id(resume/cancel 按 thread 寻址);
        但 canonical RuntimeEvent 的 run_id 必须与调用方
        ``StartRequest.metadata['invocation_id']`` 一致(conversation kernel 的
        event scope 校验),否则 hosted/web 执行路径会在首个事件上 fail。
        """
        request = self._requests.get(handle.run_id)
        if request is not None:
            invocation_id = str(
                (getattr(request, "metadata", None) or {}).get("invocation_id") or ""
            ).strip()
            if invocation_id:
                return invocation_id
        return handle.run_id

    def _make_source(self, handle: RunHandle) -> SourceRef:
        request = self._requests.get(handle.run_id)
        return SourceRef(
            framework="codex",
            native_run_id=handle.run_id,
            metadata={
                "agent_id": (
                    str(request.agent_id or "codex") if request is not None else "codex"
                ),
                "user_id": (
                    request.user_id
                    if request is not None
                    else str(handle.native_ref.get("user_id") or "user")
                ),
                "session_id": handle.session_id,
                "invocation_id": (
                    str(request.metadata.get("invocation_id") or handle.run_id)
                    if request is not None
                    else handle.run_id
                ),
            },
        )

    def _with_caller_scope(self, event: RuntimeEvent, request: Any) -> RuntimeEvent:
        """把调用方 scope(request 的 agent/user/session/invocation)并入事件 source。"""

        if request is None:
            return event
        caller_scope = {
            "agent_id": str(getattr(request, "agent_id", "") or "codex"),
            "user_id": str(getattr(request, "user_id", "") or "user"),
            "session_id": str(getattr(request, "session_id", "") or ""),
            "invocation_id": str(
                (getattr(request, "metadata", None) or {}).get("invocation_id")
                or ""
            ),
        }
        merged = {**caller_scope, **dict(event.source.metadata or {})}
        # adapter 自身字段优先;仅补齐缺失的调用方 scope 键。
        for key, value in caller_scope.items():
            if not merged.get(key):
                merged[key] = value
        source = event.source.model_copy(update={"metadata": merged})
        return event.model_copy(update={"source": source})

    def _canonical_kwargs(
        self,
        handle: RunHandle,
        *,
        scope_id: str,
        item_id: str,
        event_type: str,
        part_id: str,
    ) -> dict[str, Any]:
        framework = "codex"
        run_id = self._event_run_id(handle)
        n = self._next_seq()
        return {
            "schema_version": 2,
            "event_id": stable_event_id(
                framework, scope_id, item_id, event_type, part_id, run_id, n
            ),
            "seq": n,
            "timestamp": time.time(),
            "run_id": run_id,
            "scope_id": scope_id,
            "source": self._make_source(handle),
        }

    def _make_run_canceled(
        self, handle: RunHandle, *, reason: str | None = None
    ) -> RunCanceled:
        framework = "codex"
        run_id = self._event_run_id(handle)
        scope_id = stable_scope_id(framework, run_id)
        item_id = stable_item_id(framework, run_id, "$run")
        return RunCanceled(
            **self._canonical_kwargs(
                handle,
                scope_id=scope_id,
                item_id=item_id,
                event_type="run.canceled",
                part_id="run",
            ),
            status="canceled",
            reason=reason,
        )

    def _make_run_interrupted(
        self, handle: RunHandle, *, reason: str | None = None
    ) -> RunInterrupted:
        framework = "codex"
        run_id = self._event_run_id(handle)
        scope_id = stable_scope_id(framework, run_id)
        item_id = stable_item_id(framework, run_id, "$run")
        return RunInterrupted(
            **self._canonical_kwargs(
                handle,
                scope_id=scope_id,
                item_id=item_id,
                event_type="run.interrupted",
                part_id="run",
            ),
            status="interrupted",
            reason=reason,
        )

    def _make_run_failed(
        self, handle: RunHandle, error_message: str
    ) -> RunFailed:
        framework = "codex"
        run_id = self._event_run_id(handle)
        scope_id = stable_scope_id(framework, run_id)
        item_id = stable_item_id(framework, run_id, "$run")
        return RunFailed(
            **self._canonical_kwargs(
                handle,
                scope_id=scope_id,
                item_id=item_id,
                event_type="run.failed",
                part_id="run",
            ),
            status="failed",
            error=ErrorInfo(
                code="codex_runtime_failed",
                message=error_message,
                source="codex",
                scope_id=scope_id,
                source_ref=self._make_source(handle),
            ),
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


def _coerce_prompt_text(value: Any) -> Any:
    """把 canonical message 形态的 input 压成 SDK 可接受的文本。

    ``openai-codex`` 0.147 的 run input 只接受 TextInput/str;请求侧没有
    conversation preprocessing 时 ``request.input`` 可能是
    ``[{role, content}]`` 历史列表,直接透传会 ``unsupported input item``。
    """
    if isinstance(value, str) or value is None:
        return value
    if isinstance(value, dict):
        content = value.get("content") if "role" in value else value.get("text")
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            text = content.get("text") or content.get("content")
            if isinstance(text, str):
                return text
        return str(value)
    if isinstance(value, list):
        texts = [
            text
            for text in (_coerce_prompt_text(item) for item in value)
            if isinstance(text, str) and text
        ]
        return "\n".join(texts) if texts else str(value)
    return str(value)


def _request_prompt(request: StartRequest) -> Any:
    """Render canonical conversation history for a native Codex turn.

    只传 user 消息历史 + 当前 input，不传 assistant 的完整回复（避免 codex 复述历史）。
    assistant 回复用简短摘要代替，仅维持对话结构。
    """
    # A resumed Codex thread already owns its transcript. Re-sending Studio's
    # transport-neutral history would duplicate every prior turn after refresh.
    if str(request.metadata.get("thread_id") or "").strip():
        return (
            request.input
            if _is_structured_turn_input(request.input)
            else _coerce_prompt_text(request.input)
        )

    conversation = request.conversation_preprocessing()
    if conversation is None or not conversation.messages:
        # Keep native text/image/mention parts intact for _build_run_input().
        # Flattening this list turns an image dict into user-visible text.
        return (
            request.input
            if _is_structured_turn_input(request.input)
            else _coerce_prompt_text(request.input)
        )

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


def _is_structured_turn_input(value: Any) -> bool:
    return isinstance(value, list) and any(
        isinstance(item, dict) and isinstance(item.get("type"), str)
        for item in value
    )


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
            if not kind and "role" in item:
                # canonical conversation message({role, content});当前 input
                # 已由 prompt(或 conversation preprocessing)承载,跳过历史项。
                continue
            if kind in {"text", "input_text"}:
                text = str(
                    prompt
                    if not text_replaced and isinstance(prompt, str)
                    else item.get("text") or ""
                )
                text_replaced = True
                if text:
                    native_items.append(TextInput(text=text))
            elif kind in {"image", "input_image"} and (
                item.get("url") or item.get("image_url")
            ):
                native_items.append(
                    ImageInput(url=str(item.get("url") or item.get("image_url")))
                )
            elif kind == "localImage" and item.get("path"):
                native_items.append(LocalImageInput(path=str(item["path"])))
            elif kind == "input_file" and (
                item.get("file_data")
                or str(item.get("file_url") or "").startswith("data:")
            ):
                file_path = _materialize_inline_file(
                    str(item.get("file_data") or item.get("file_url")),
                    str(item.get("filename") or "attachment"),
                )
                if file_path is not None:
                    # App Server's ``mention`` input is presentation metadata:
                    # current Codex versions do not include it in the model's
                    # user message.  Always add an explicit model-visible
                    # attachment context as well.  Small textual files are
                    # inlined deterministically; binary/large files expose a
                    # sandbox-readable path that Codex can inspect with tools.
                    native_items.append(
                        TextInput(text=_attachment_context_text(file_path, item))
                    )
                    native_items.append(
                        MentionInput(name=file_path.name, path=str(file_path))
                    )
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


def _materialize_inline_file(data_url: str, filename: str) -> Path | None:
    """Materialize a bounded Studio inline attachment for native Codex."""

    match = re.fullmatch(r"data:([^;,]+)?;base64,([A-Za-z0-9+/=\s]+)", data_url)
    encoded = match.group(2) if match is not None else data_url.strip()
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None
    if not payload or len(payload) > 10 * 1024 * 1024:
        return None
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename).name).strip(".-")
    safe_name = safe_name[:120] or "attachment"
    digest = hashlib.sha256(payload).hexdigest()
    path = Path("/tmp/ksadk-codex-attachments") / digest / safe_name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(payload)
    return path


_MAX_INLINE_ATTACHMENT_TEXT_BYTES = 64 * 1024
_TEXT_ATTACHMENT_SUFFIXES = {
    ".csv",
    ".html",
    ".htm",
    ".ini",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".py",
    ".rst",
    ".toml",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


def _attachment_context_text(path: Path, item: Mapping[str, Any]) -> str:
    """Build model-visible context for a materialized Responses input file."""

    name = str(item.get("filename") or path.name).replace('"', "'")
    inline_data = item.get("inlineData")
    inline_mime = inline_data.get("mimeType") if isinstance(inline_data, Mapping) else None
    mime_type = str(item.get("mime_type") or inline_mime or "").strip().lower()
    source = str(item.get("file_data") or item.get("file_url") or "")
    data_url_match = re.match(r"data:([^;,]+)", source)
    if not mime_type and data_url_match is not None:
        mime_type = data_url_match.group(1).strip().lower()
    is_text = mime_type.startswith("text/") or path.suffix.lower() in _TEXT_ATTACHMENT_SUFFIXES
    header = f'<uploaded_attachment name="{name}" path="{path}">'
    if not is_text:
        return (
            f"{header}\n"
            "The uploaded file is available at the path above. Read it with an appropriate "
            "tool before answering questions about its contents.\n"
            "</uploaded_attachment>"
        )

    raw = path.read_bytes()
    truncated = len(raw) > _MAX_INLINE_ATTACHMENT_TEXT_BYTES
    text = raw[:_MAX_INLINE_ATTACHMENT_TEXT_BYTES].decode("utf-8", errors="replace")
    suffix = "\n[attachment content truncated]" if truncated else ""
    return f"{header}\n{text}{suffix}\n</uploaded_attachment>"


__all__ = ["CodexRuntimeAdapter"]
