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
import time
from contextlib import aclosing
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from ksadk.codex.client import CodexClient, CodexPluginBootstrap
from ksadk.codex.projection import CodexTurnProjection
from ksadk.events.adapters.codex import CodexAdapterContext, CodexEventAdapter
from ksadk.events.canonical import (
    ErrorInfo,
    InteractionRequested,
    InteractionResolved,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunStarted,
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

from ksadk.codex.base_runtime import _CodexAsBaseRuntime

logger = logging.getLogger(__name__)

_INTERRUPT_DRAIN_TIMEOUT_SECONDS = 5.0


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
    cancel_experts: Any = None


class CodexRuntimeAdapter(RuntimeAdapter):
    """Codex 的 RuntimeAdapter(重托管)。"""

    def __init__(
        self,
        client: CodexClient,
        *,
        sandbox_read_only: bool = True,
        turn_timeout_seconds: Optional[float] = None,
        bound_skill_paths: Mapping[str, str] | None = None,
        plugin_bootstrap: CodexPluginBootstrap | None = None,
        turn_projector: CodexTurnProjection | None = None,
        a2a_experts=(),
        managed_a2a_bootstrap=None,
    ) -> None:
        super().__init__(_CodexAsBaseRuntime(client))
        self._client = client
        self._a2a_experts = tuple(a2a_experts)
        self._managed_a2a_bootstrap = managed_a2a_bootstrap
        if self._a2a_experts:
            if managed_a2a_bootstrap is None:
                raise ValueError("A2A_MANAGED_BOOTSTRAP_REQUIRED")
            client.configure_a2a_experts(self._a2a_experts)
        self._sandbox_read_only = sandbox_read_only
        self._turn_timeout_seconds = turn_timeout_seconds
        if turn_projector is not None and bound_skill_paths:
            raise ValueError("Skill paths must be owned by the supplied turn projector")
        if turn_projector is None:
            # Compatibility for direct SDK construction; production factories inject
            # the same official projector, never a second input implementation.
            from ksadk.plugins.providers.codex_turn import CodexTurnProjector

            turn_projector = CodexTurnProjector(bound_skill_paths=bound_skill_paths)
        self._turn_projector = turn_projector
        self._plugin_bootstrap = plugin_bootstrap
        self._plugin_bootstrap_lock = asyncio.Lock()
        self._plugins_bootstrapped = False
        self._threads: dict[str, _CodexThread] = {}
        self._requests: dict[str, StartRequest] = {}
        self._canonical_roots: dict[str, RunStarted] = {}
        self._recovery_records: dict[str, Any] = {}
        self._known_threads: set[str] = set()
        self._pending_cancels: set[str] = set()
        # 被杀/interrupt 的 session 不持久化(goal-09 契约 1)。
        self._do_not_persist: set[str] = set()
        # 可观测:最近一次 cancel 级联丢弃的审批集(contract test 断言用)。
        self.last_cancel_dropped_approvals: set[str] = set()
        self._seq = 0
        self._closed = False
        self._owned_by_app = False

    def configure_managed_a2a(self, bootstrap) -> None:
        if self._managed_a2a_bootstrap is bootstrap:
            return
        if self._threads or self._known_threads:
            raise RuntimeError("cannot change managed A2A after a native thread starts")
        if self._managed_a2a_bootstrap is not None:
            raise ValueError("A2A_BOOTSTRAP_IDENTITY_CONFLICT")
        self._managed_a2a_bootstrap = bootstrap

    # ---- capability matrix(v1,诚实声明) ----

    def capabilities(self) -> RuntimeCapabilityMatrix:
        """Native controls plus explicitly configured managed-expert recovery.

        Ordinary Codex threads retain their existing process-local declaration.
        Recovery needs both an explicit native home and durable A2A storage.
        """

        def _unavailable(reason: str) -> RuntimeCapability:
            return RuntimeCapability(supported=False, mode="unavailable", reason=reason)

        recovery = False
        if self._a2a_experts and getattr(self._client, "_checkpoint_home", None):
            bootstrap = self._managed_a2a_bootstrap
            try:
                recovery = bool(bootstrap.outbound_enabled
                                and bootstrap.invocation_checkpoint_store is not None)
            except (AttributeError, RuntimeError):
                recovery = False

        bootstrap = self._managed_a2a_bootstrap
        scoped_cancel = bool(
            bootstrap is not None and getattr(bootstrap, "scoped_cancel_available", False)
        )
        return RuntimeCapabilityMatrix(
            scoped_cancel=(RuntimeCapability(supported=True, mode="emulated")
                           if scoped_cancel else None),
            cancel=RuntimeCapability(supported=True, mode="native"),
            pause=RuntimeCapability(supported=True, mode="native"),
            resume=RuntimeCapability(supported=True, mode="native"),
            submit_interaction=RuntimeCapability(supported=True, mode="native"),
            attach=(RuntimeCapability(supported=True, mode="native") if recovery
                    else _unavailable("codex_process_local_thread_table")),
            steer=_unavailable("runtime_no_native_steer"),
            inject=_unavailable("runtime_no_native_inject"),
            checkpoint=RuntimeCapability(supported=True, mode="native"),
            durable_restore=(RuntimeCapability(supported=True, mode="native") if recovery
                             else _unavailable("codex_durable_restore_requires_attach_seam")),
            goal=(
                _unavailable("a2a_codex_expert_goal_unsupported")
                if self._a2a_experts
                else RuntimeCapability(supported=True, mode="native")
            ),
            loop=_unavailable("codex_loop_requires_run_control_spec"),
            plan=RuntimeCapability(supported=True, mode="native"),
            interaction_mode="live_submit",
        )

    # ---- 六动词 ----

    async def start(self, request: StartRequest) -> RunHandle:
        # A failed bootstrap must leave no native thread behind. The success
        # bit is set only after marketplace/add, every plugin/install, and the
        # enabled-inventory reconciliation all complete on this same client.
        await self._bootstrap_plugins_once()
        # 新 thread 由后端分配真实 thread_id(thread_start);metadata 携带的 thread_id
        # 表示接入既有 thread(resume 语义,run_turn 时按 resume 接入)。
        request = self._turn_projector.prepare(request)
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
            # Independently owned adapters close their transport per turn;
            # app-owned adapters may also cross a process boundary on restart.
            # An ephemeral thread has no rollout to resume across either
            # boundary, so retain the native durable thread by default.
            thread_config.setdefault("ephemeral", False)
            thread_id = await self._client.start_thread(thread_config)
        self._do_not_persist.discard(thread_id)
        self._known_threads.add(thread_id)
        self._canonical_roots.pop(thread_id, None)
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

    async def prepare_session_continuation(
        self, record, request: StartRequest, *, agent_instance_id: str, tenant_id: str,
    ) -> None:
        """Only Kernel-loaded completed history can authorize an expert follow-up."""
        if not self._a2a_experts:
            return
        from ksadk.a2a.codex_session_checkpoint import validated_session_checkpoint

        bridge = getattr(self._client, "_a2a_expert_bridge", None)
        try:
            handle = RunHandle.model_validate(record.metadata.get("handle"))
            checkpoint = validated_session_checkpoint(
                record, handle, request, agent_instance_id=agent_instance_id,
                tenant_id=tenant_id, experts=bridge.experts,
            )
        except (ValueError, TypeError, AttributeError):
            raise ValueError("A2A_CODEX_SESSION_CHECKPOINT_INVALID") from None
        await self._bootstrap_plugins_once()
        await self._client.restore_completed_a2a_thread(checkpoint)

    async def prepare_attach_record(self, handle: RunHandle, record) -> None:
        from ksadk.a2a.codex_recovery import validated_recovery_checkpoint

        if not self._a2a_experts:
            return
        if self._managed_a2a_bootstrap is None:
            raise ValueError("A2A_CODEX_RECOVERY_MANAGED_EXPERTS_REQUIRED")
        if handle.run_id in self._known_threads or handle.run_id in self._recovery_records:
            raise ValueError("A2A_CODEX_RECOVERY_THREAD_ALREADY_ATTACHED")
        bridge = self._client._a2a_expert_bridge
        if record.metadata.get("runtime_checkpoint", {}).get("schema") == "codex.a2a.session/v1":
            from ksadk.a2a.codex_session_checkpoint import validated_terminal_recovery_checkpoint

            self._recovery_records[handle.run_id] = validated_terminal_recovery_checkpoint(
                record, handle, experts=bridge.experts,
            )
            return
        recovery = validated_recovery_checkpoint(record, handle, experts=bridge.experts)
        if recovery.continuation_started:
            raise ValueError("A2A_CODEX_RECOVERY_CONTINUATION_UNCERTAIN")
        self._recovery_records[handle.run_id] = recovery

    async def attach(self, handle: RunHandle) -> RunHandle:
        if not self._a2a_experts:
            return await super().attach(handle)
        recovery = self._recovery_records.pop(handle.run_id, None)
        if recovery is None:
            raise ValueError("A2A_CODEX_RECOVERY_RECORD_REQUIRED")
        # No cached handle alone may authorize native access or a new turn.
        if (handle.runtime_type != "codex" or handle.session_id != recovery.session_id
                or handle.native_ref.get("thread_id") != recovery.thread_id
                or handle.native_ref.get("user_id") != recovery.start_request.user_id):
            raise ValueError("A2A_CODEX_RECOVERY_HANDLE_CONFLICT")
        from ksadk.a2a.codex_session_checkpoint import CodexSessionCheckpoint

        if isinstance(recovery, CodexSessionCheckpoint):
            thread = _CodexThread(thread_id=handle.run_id, continuation_preexisting=True)
            thread.__dict__["_terminal_recovery"] = recovery
            self._threads[handle.run_id] = thread
            self._requests[handle.run_id] = recovery.start_request
            self._known_threads.add(handle.run_id)
            return handle
        await self._bootstrap_plugins_once()
        restored = await self._client.restore_managed_a2a_thread(recovery)
        if restored != handle.run_id:
            raise ValueError("A2A_CODEX_RECOVERY_NATIVE_IDENTITY_INVALID")
        thread = _CodexThread(thread_id=restored, continuation_preexisting=True)
        thread.__dict__["_start_request"] = recovery.start_request
        thread.__dict__["_recovery"] = recovery
        self._threads[restored] = thread
        self._requests[restored] = recovery.start_request
        self._known_threads.add(restored)
        return handle

    async def durable_restore(self, handle: RunHandle) -> RunHandle:
        if not self._a2a_experts:
            return await super().durable_restore(handle)
        return await self.attach(handle)

    def requires_recovery_stream(self, handle: RunHandle) -> bool:
        thread = self._threads.get(handle.run_id)
        return thread is not None and any(
            thread.__dict__.get(key) is not None for key in ("_recovery", "_terminal_recovery")
        )

    async def _bootstrap_plugins_once(self) -> None:
        config = self._plugin_bootstrap
        if config is None or self._plugins_bootstrapped:
            return
        async with self._plugin_bootstrap_lock:
            if self._plugins_bootstrapped:
                return
            await self._client.bootstrap_plugins(config)
            self._plugins_bootstrapped = True

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
            # Cancel bound experts while the Kernel still owns this live run.
            # Native interruption can settle the parent before stream teardown.
            if thread.cancel_experts is not None:
                await thread.cancel_experts()
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
            if not interrupted and thread.__dict__.get("_recovery") is None:
                # 无活跃 handle 可 interrupt(竞态:turn 刚好结束)→ 视为未在运行。
                return CancelResult.NOT_RUNNING
            # 被中断的 session 不持久化(goal-09 契约 1)。
            self._do_not_persist.add(thread_id)
            thread.done = True
            self._threads.pop(thread_id, None)
            if thread.__dict__.get("_recovery") is None:
                self._requests.pop(thread_id, None)
            # Recovery has no native terminal notification yet. Its consumer
            # still needs the original caller identity to emit run.canceled.
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
                raw,
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
        self._canonical_roots.pop(handle.run_id, None)
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

    def adopt_app_lifetime(self) -> None:
        """A shared composition root owns the native transport and projector."""
        self._owned_by_app = True

    async def close_app_resources(self) -> None:
        if self._owned_by_app:
            await self.close_all()

    async def close(self, handle: RunHandle) -> None:
        self._do_not_persist.add(handle.run_id)
        if not self._owned_by_app:
            await self.close_all()
            return
        thread = self._threads.get(handle.run_id)
        if thread is not None and thread.streaming and not thread.done:
            thread.interrupt_event.set()
            await self._client.interrupt_active_turn(thread.thread_id)
        # Preserve the native client's completed-thread cache for the next
        # turn; release only this Kernel run's projections and request.
        self._threads.pop(handle.run_id, None)
        self._requests.pop(handle.run_id, None)
        self._canonical_roots.pop(handle.run_id, None)
        self._pending_cancels.discard(handle.run_id)

    async def compact_session(self, thread_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """Compact an existing native session using this build's client."""
        await self._bootstrap_plugins_once()
        await self._client.resume_thread(thread_id, config)
        return await self._client.compact_thread(thread_id)

    async def close_all(self) -> None:
        """Dispose every thread and the activation-owned App Server process.

        An independently owned adapter closes the transport with its handle.
        Shared app-owned adapters retain it until application shutdown. This
        helper also lets a draining AgentProvider release an unused runtime.
        """

        if self._closed:
            return
        self._closed = True
        threads = tuple(self._threads.values())
        active_threads = tuple(thread for thread in threads if thread.streaming and not thread.done)
        for thread in active_threads:
            thread.interrupt_event.set()
        try:
            for thread in active_threads:
                await self._client.interrupt_active_turn(thread.thread_id)
        finally:
            # AsyncCodex.close owns terminate/wait/kill for the app-server child.
            try:
                await self._client.close()
            finally:
                self._turn_projector.close()
            thread_ids = {
                *self._known_threads,
                *self._threads,
                *self._requests,
            }
            self._do_not_persist.update(thread_ids)
            self._threads.clear()
            self._requests.clear()
            self._canonical_roots.clear()
            self._recovery_records.clear()
            self._known_threads.clear()
            self._pending_cancels.clear()

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

        terminal = thread.__dict__.get("_terminal_recovery")
        if terminal is not None:
            from ksadk.a2a.codex_session_checkpoint import terminal_recovery_event

            try:
                yield await terminal_recovery_event(terminal)
            finally:
                thread.done = True
                self._threads.pop(handle.run_id, None)
            return

        recovery = thread.__dict__.get("_recovery")
        if recovery is not None:
            await self._restore_canonical_root(handle, recovery)

        if handle.run_id in self._pending_cancels:
            self._pending_cancels.discard(handle.run_id)
            try:
                yield self._make_run_canceled(
                    handle,
                    reason=f"pending_cancel:{CancelResult.PENDING_CANCEL_RECORDED.value}",
                )
            finally:
                self._canonical_roots.pop(handle.run_id, None)
            return

        request = thread.__dict__.get("_start_request")
        resume_state = thread.__dict__.get("_resume")
        if recovery is None:
            projected = self._turn_projector.project(
                request, resume_state.get("payload") if resume_state is not None else None,
            )
            run_input = projected.input
        else:
            # The original user input is already in the restored transcript.
            # Only verified expert outputs precede the next native turn.
            run_input = []
        thread.streaming = True
        thread.turn_id = thread.turn_id or f"turn_{thread.thread_id}"
        try:
            events = self._map_codex_stream(handle, thread, run_input)
            if self._a2a_experts:
                from ksadk.a2a.codex_runtime import managed_codex_events
                events = managed_codex_events(self._client, self._managed_a2a_bootstrap, events, request=request, owner=thread, recovery=recovery)
            async with aclosing(events):
                async for event in events:
                    if (isinstance(event, RunStarted) and event.parent_scope_id is None
                            and event.run_id == self._event_run_id(handle)):
                        # Runtime fallbacks must terminate the public/native
                        # root already observed by consumers, even if cancel
                        # removes the request/thread before stream teardown.
                        self._canonical_roots.setdefault(handle.run_id, event)
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
            from ksadk.a2a.codex_runtime import CodexRecoveryCancelled

            self._do_not_persist.add(handle.run_id)
            if isinstance(exc, CodexRecoveryCancelled):
                yield self._make_run_canceled(handle, reason="recovery_canceled")
            else:
                yield self._make_run_failed(handle, str(exc))
        finally:
            thread.streaming = False
            thread.done = True
            self._threads.pop(handle.run_id, None)
            self._canonical_roots.pop(handle.run_id, None)

    async def _restore_canonical_root(self, handle: RunHandle, recovery: Any) -> None:
        from ksadk.events.run_authority import current_run_event_authority

        authority = current_run_event_authority()
        if authority is None:
            raise RuntimeError("A2A_KERNEL_AUTHORITY_REQUIRED")
        # No tail limit: the root may precede hundreds of native observations.
        # The capability enforces the validated public session/run identity.
        for event in await authority.list(handle.session_id, run_id=recovery.run_id):
            if isinstance(event, RunStarted) and event.parent_scope_id is None:
                self._canonical_roots[handle.run_id] = event
                return

    async def _map_codex_stream(
        self,
        handle: RunHandle,
        thread: _CodexThread,
        prompt: Any,
    ) -> AsyncIterator[RuntimeEvent]:
        request = thread.__dict__.get("_start_request") or thread.__dict__.get("_request_config")
        adapter = CodexEventAdapter(
            known_thread_ids=(thread.thread_id,) if thread.continuation_preexisting else (),
        )
        context = CodexAdapterContext(run_id=self._event_run_id(handle))
        run_config: dict[str, Any] = {"sandbox_read_only": self._sandbox_read_only}
        if request is not None and request.config:
            for key in ("sandbox", "approval_mode", "summary", "collaboration_mode", "effort"):
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
                        asyncio.get_running_loop().time() + _INTERRUPT_DRAIN_TIMEOUT_SECONDS
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
                    str((chunk or {}).get("method") or "") if isinstance(chunk, dict) else ""
                )
                if chunk_method in {"ksadk/a2a/toolCall", "ksadk/a2a/toolCancel"}:
                    if not self._a2a_experts:
                        raise ValueError("A2A_CODEX_EXPERTS_NOT_CONFIGURED")
                    yield chunk
                    continue
                if chunk_method in {
                    "item/autoApprovalReview/started",
                    "item/autoApprovalReview/completed",
                }:
                    review_params = chunk.get("params") or {}
                    review_id = str(
                        review_params.get("reviewId") or review_params.get("review_id") or ""
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
                    if thread.__dict__.get("_recovery") is not None:
                        root = self._canonical_roots.get(handle.run_id)
                        if (root is not None and isinstance(event, RunStarted)
                                and event.run_id == root.run_id and event.parent_scope_id is None):
                            # The public run already started before recovery;
                            # a new native turn must not start it a second time.
                            continue
                        event = self._with_recovered_root(event, handle)
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
        """事件使用调用方 invocation_id 或 Kernel run_id，最后回退到 thread id。

        ``handle.run_id`` 是 codex 原生 thread id(resume/cancel 按 thread 寻址);
        但 canonical RuntimeEvent 的 run_id 必须与调用方
        ``StartRequest.metadata['invocation_id']`` 或 Kernel 的 ``run_id`` 一致，
        否则同一原生 thread 上的后续调用可能复用事件身份并被去重。
        """
        root = self._canonical_roots.get(handle.run_id)
        if root is not None:
            return root.run_id
        request = self._requests.get(handle.run_id)
        if request is not None:
            invocation_id = str(
                (getattr(request, "metadata", None) or {}).get("invocation_id")
                or (getattr(request, "metadata", None) or {}).get("run_id")
                or ""
            ).strip()
            if invocation_id:
                return invocation_id
        return handle.run_id

    def _make_source(self, handle: RunHandle) -> SourceRef:
        root = self._canonical_roots.get(handle.run_id)
        if root is not None:
            return root.source
        request = self._requests.get(handle.run_id)
        return SourceRef(
            framework="codex",
            native_run_id=handle.run_id,
            metadata={
                "agent_id": (str(request.agent_id or "codex") if request is not None else "codex"),
                "user_id": (
                    request.user_id
                    if request is not None
                    else str(handle.native_ref.get("user_id") or "user")
                ),
                "session_id": handle.session_id,
                "invocation_id": (
                    self._event_run_id(handle)
                    if request is not None
                    else handle.run_id
                ),
            },
        )

    def _with_recovered_root(self, event: RuntimeEvent, handle: RunHandle) -> RuntimeEvent:
        root = self._canonical_roots.get(handle.run_id)
        if (root is None or event.run_id != root.run_id
                or event.parent_scope_id is not None
                or not event.event_type.startswith("run.")):
            return event
        # Normalize before managed checkpointing, preserving native source and
        # item/child identities of the continuation's new native turn.
        updates = {"scope_id": root.scope_id}
        if isinstance(event, RunFailed):
            updates["error"] = event.error.model_copy(update={"scope_id": root.scope_id})
        return event.model_copy(update=updates)

    def _with_caller_scope(self, event: RuntimeEvent, request: Any) -> RuntimeEvent:
        """把调用方 scope(request 的 agent/user/session/invocation)并入事件 source。"""

        if request is None:
            return event
        caller_scope = {
            "agent_id": str(getattr(request, "agent_id", "") or "codex"),
            "user_id": str(getattr(request, "user_id", "") or "user"),
            "session_id": str(getattr(request, "session_id", "") or ""),
            "invocation_id": str(
                (getattr(request, "metadata", None) or {}).get("invocation_id") or ""
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

    def _lifecycle_scope_id(self, handle: RunHandle) -> str:
        root = self._canonical_roots.get(handle.run_id)
        return root.scope_id if root is not None else stable_scope_id("codex", self._event_run_id(handle))

    def _make_run_canceled(self, handle: RunHandle, *, reason: str | None = None) -> RunCanceled:
        framework = "codex"
        run_id = self._event_run_id(handle)
        scope_id = self._lifecycle_scope_id(handle)
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
        scope_id = self._lifecycle_scope_id(handle)
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

    def _make_run_failed(self, handle: RunHandle, error_message: str) -> RunFailed:
        framework = "codex"
        run_id = self._event_run_id(handle)
        scope_id = self._lifecycle_scope_id(handle)
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




__all__ = ["CodexRuntimeAdapter"]
