"""CodexRuntime — 非 ADK 体系的第三验证样本,按 Wegent 重托管模式 (goal-09)。

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
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)

logger = logging.getLogger(__name__)


class _CodexAsBaseRuntime(BaseRuntime):
    """把 CodexClient 包装为 BaseRuntime(原生能力面)。"""

    def __init__(self, client: CodexClient) -> None:
        self._client = client
        self.runtime_type = "codex"

    def native_capabilities(self) -> dict[str, Any]:
        return {"Framework": "codex", "cancel": "thread", "resume": "thread_id"}


@dataclass
class _CodexThread:
    thread_id: str
    turn_id: Optional[str] = None
    streaming: bool = False
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    pending_approvals: set[str] = field(default_factory=set)
    done: bool = False
    interrupted: bool = False


class CodexRuntime(RuntimeAdapter):
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
            if request.model:
                thread_config["model"] = request.model
            base_instructions = request.config.get("base_instructions")
            if base_instructions:
                thread_config["base_instructions"] = base_instructions
            thread_id = await self._client.start_thread(thread_config)
        self._known_threads.add(thread_id)
        thread = _CodexThread(thread_id=thread_id)
        thread.__dict__["_start_request"] = request
        self._threads[thread_id] = thread
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
            interrupted = await self._client.interrupt_active_turn(thread.thread_id)
            if not interrupted:
                # 无活跃 handle 可 interrupt(竞态:turn 刚好结束)→ 视为未在运行。
                return CancelResult.NOT_RUNNING
            thread.interrupt_event.set()
            # 被中断的 session 不持久化(goal-09 契约 1)。
            self._do_not_persist.add(thread_id)
            thread.done = True
            self._threads.pop(thread_id, None)
            self._pending_cancels.discard(thread_id)
            return CancelResult.INTERRUPTED_ACTIVE_TURN
        except Exception:  # noqa: BLE001
            logger.exception("codex cancel thread %s 失败", thread_id)
            return CancelResult.FAILED

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: Optional[ResumePayload],
    ) -> RunHandle:
        # resume 用 thread id 语义(resume_thread_id),不套 ADK invocation 模型。
        if target.kind != "thread_id":
            raise ValueError(f"CodexRuntime resume 仅支持 thread_id 目标,得到 {target.kind!r}")
        if handle.run_id in self._do_not_persist:
            raise ValueError(f"thread {handle.run_id} 已被中断/杀进程,不持久化,不可 resume")
        self._pending_cancels.discard(handle.run_id)
        self._known_threads.add(target.id)
        thread = _CodexThread(thread_id=target.id)
        thread.__dict__["_resume"] = {"target": target, "payload": payload}
        self._threads[handle.run_id] = thread
        # 真实恢复:thread_resume 接入后端既有 thread(thread_id 语义,不套 ADK invocation)。
        await self._client.resume_thread(target.id, {"sandbox_read_only": self._sandbox_read_only})
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
            prompt = request.input
        elif resume_state is not None:
            prompt = _resume_prompt(resume_state.get("payload"))
        else:
            prompt = ""
        thread.streaming = True
        thread.turn_id = thread.turn_id or f"turn_{thread.thread_id}"
        try:
            async for event in self._map_codex_stream(handle, thread, tracker, prompt):
                yield event
            # 正常结束(非 interrupt):补 RUN_COMPLETED(AGUI 投射器据此发 RunFinished success)
            if not thread.interrupted:
                yield self._event(handle, EventType.RUN_COMPLETED, {"status": "completed"})
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
        codex_gen = self._client.run_turn(
            thread.thread_id,
            prompt,
            config={"sandbox_read_only": self._sandbox_read_only},
        )
        deadline = (
            asyncio.get_running_loop().time() + self._turn_timeout_seconds
            if self._turn_timeout_seconds is not None
            else None
        )
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
                for task in pending:
                    task.cancel()
                if interrupt_task in done:
                    chunk_task.cancel()
                    thread.interrupted = True
                    # AGUI 投射器对 RUN_INTERRUPTED 无兜底,必须显式发,否则 raise
                    yield self._event(
                        handle, EventType.RUN_INTERRUPTED, {"status": "interrupted"}
                    )
                    return
                chunk = chunk_task.result()
                if chunk is _STREAM_STOP:
                    return
                event = self._codex_chunk_to_event(handle, thread, tracker, chunk)
                if event is not None:
                    yield event
        finally:
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

        if method == "item/started":
            tracker.observe_item(params)
            return None
        if method == "item/completed":
            item = params.get("item") or params
            if item.get("type") != "agentMessage":
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
        if "delta" in method or method == "item/agentMessage/delta":
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
            "approval" in method
            or "requestPermission" in method
            or "approval" in str(chunk.get("type") or "")
        ):
            call_id = str(
                params.get("id") or params.get("call_id") or params.get("requestId") or ""
            )
            if call_id:
                thread.pending_approvals.add(call_id)
            return self._event(
                handle,
                EventType.APPROVAL_REQUESTED,
                {"approval_id": call_id, "call_id": call_id, "kind": "tool", "detail": params},
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
        return RuntimeEvent.create(
            event_type,
            agent_id="codex",
            user_id=str(handle.native_ref.get("user_id") or "user"),
            session_id=handle.session_id,
            invocation_id=handle.run_id,
            seq_id=self._next_seq(),
            phase=phase,
            payload=payload,
        )


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


__all__ = ["CodexRuntime"]
