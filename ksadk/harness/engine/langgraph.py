"""ManagedLangGraphEngine —— 默认执行引擎（plan §7 / Phase 1）。

实现 §7.1 默认 Agent Loop 的 Phase 1 子集：prepare_context -> reason ->
tool_calls -> final，reason 自环。LangGraph 类型不越出本模块
（tests/architecture/test_harness_contract.py 守卫）。

Phase 1 能力范围（诚实声明）：
- Cancel：支持（终止语义，不伪装 Pause）；
- Interrupt/Resume：支持（approval 通道，resume 携带 payload 重放）；
- Checkpoint：LangGraph Checkpointer 注入（内存/SQLite），thread_id 走
  租户复合编码（plan §6.2.2）；
- Context 压缩：Phase 2 交付，本引擎留 policy 读取口。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ksadk.events import EventType, RuntimeEvent
from ksadk.harness.engine.base import (
    CompiledHarness,
    EngineCapability,
    EngineCapabilityMatrix,
    ExecutionEngineError,
    single_agent_plan,
)
from ksadk.harness.engine.thread_ids import encode_thread_id
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn, LiteLLMHarnessReasoner
from ksadk.harness.spec import HarnessSpec
from ksadk.harness.state import HarnessState, Message, MessageRole, RunStatus
from ksadk.runtime import (
    CancelResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    StartRequest,
)

_MAX_REASONING_TURNS = 8


class _GraphState(TypedDict):
    """图 State——只存最小路由信息（plan §6.2.1），正文活在 HarnessState。"""

    messages: list[dict[str, Any]]  # OpenAI 形态消息（含 tool_calls）
    pending_tool_calls: list[dict[str, Any]]
    turn_count: int
    route: str  # "reason" | "final"


@dataclass
class _EngineRun:
    handle: RunHandle
    request: StartRequest
    compiled: CompiledHarness
    state: HarnessState
    thread_id: str
    task: asyncio.Task[list[RuntimeEvent]] | None = None
    events: list[RuntimeEvent] = field(default_factory=list)
    seq: int = 0
    cancel_requested: bool = False
    done: bool = False
    started_emitted: bool = False


class ManagedLangGraphEngine:
    """默认执行引擎：Revision 编译产物在 LangGraph 上运行。"""

    def __init__(
        self,
        *,
        reasoner: HarnessReasoner | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        tenant_id: str = "default",
        tools: dict[str, Any] | None = None,
        approval_required: set[str] | None = None,
        context_engine: Any | None = None,
    ) -> None:
        self._reasoner = reasoner or LiteLLMHarnessReasoner()
        self._checkpointer = checkpointer
        self._tenant_id = tenant_id
        self._tools = tools or {}
        self._approval_required = approval_required or set()
        self._context_engine = context_engine
        self._runs: dict[str, _EngineRun] = {}

    # ------------------------------------------------------------- compile

    async def compile(self, spec: HarnessSpec) -> CompiledHarness:
        return CompiledHarness(
            spec=spec,
            plan=single_agent_plan(),
            engine_kind="managed-langgraph",
        )

    # --------------------------------------------------------------- start

    async def start(self, request: StartRequest, compiled: CompiledHarness) -> RunHandle:
        run_id = str(
            request.metadata.get("invocation_id") or f"mle_{uuid.uuid4().hex[:16]}"
        )
        if run_id in self._runs:
            raise ValueError(f"duplicate engine run: {run_id}")
        state = HarnessState(
            tenant_id=self._tenant_id,
            user_id=request.user_id,
            agent_id=str(request.agent_id or compiled.spec.agent_revision_ref),
            session_id=request.session_id,
            run_id=run_id,
            status=RunStatus.PENDING,
        )
        thread_id = encode_thread_id(
            tenant_id=self._tenant_id,
            user_id=request.user_id,
            agent_id=state.agent_id,
            session_id=request.session_id,
            run_id=run_id,
        )
        handle = RunHandle(
            run_id=run_id,
            session_id=request.session_id,
            runtime_type="managed-langgraph",
            native_ref={"thread_id": thread_id},
        )
        self._runs[run_id] = _EngineRun(
            handle=handle, request=request, compiled=compiled, state=state, thread_id=thread_id
        )
        return handle

    # -------------------------------------------------------------- stream

    def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        return self._stream(handle)

    async def _stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        run = self._require_run(handle)
        task_finished = run.task is None or run.task.done()
        if run.state.status is RunStatus.AWAITING_APPROVAL and task_finished:
            # 挂起中：只回放未消费事件，不重启图（恢复必须走 resume）。
            while run.events:
                yield run.events.pop(0)
            return
        if run.done and task_finished:
            while run.events:
                yield run.events.pop(0)
            return
        if run.task is None or task_finished:
            run.task = asyncio.create_task(self._execute(run))
        try:
            while True:
                while run.events:
                    yield run.events.pop(0)
                if run.task.done():
                    break
                await asyncio.sleep(0)
            remaining = await run.task
            for event in remaining:
                yield event
        except asyncio.CancelledError:
            run.done = True
            yield self._event(run, EventType.RUN_CANCELED, {"status": "cancelled"})

    async def _execute(
        self, run: _EngineRun, resume_command: Command | None = None
    ) -> list[RuntimeEvent]:
        spec = run.compiled.spec
        run.state.status = RunStatus.RUNNING
        if not run.started_emitted:
            run.started_emitted = True
            run.events.append(self._event(run, EventType.RUN_STARTED, {"status": "in_progress"}))
        try:
            self._emit_context_plan(run)
            graph = self._build_graph(run)
            instructions = spec.prompt.instructions or ""
            config = {"configurable": {"thread_id": run.thread_id}}
            invoke_input: _GraphState | Command
            if resume_command is not None:
                invoke_input = resume_command
            else:
                invoke_input = {
                    "messages": [
                        {"role": "system", "content": instructions},
                        {"role": "user", "content": str(run.request.input or "")},
                    ],
                    "pending_tool_calls": [],
                    "turn_count": 0,
                    "route": "reason",
                }
            final_state = await graph.ainvoke(invoke_input, config=config)
            interrupts = final_state.get("__interrupt__") if isinstance(final_state, dict) else None
            if interrupts:
                # Approval 挂起：图状态已由 Checkpointer 持久化，等待 resume。
                run.state.status = RunStatus.AWAITING_APPROVAL
                run.done = False
                info = getattr(interrupts[0], "value", {}) or {}
                run.events.append(
                    self._event(
                        run,
                        EventType.APPROVAL_REQUESTED,
                        {
                            "approval_id": f"ap-{run.handle.run_id}",
                            "call_id": str(info.get("call_id", "")),
                            "kind": "tool",
                            "detail": info,
                        },
                    )
                )
                run.events.append(
                    self._event(
                        run, EventType.RUN_INTERRUPTED,
                        {"status": "awaiting_approval", "reason": "tool_approval"},
                    )
                )
                return []
            run.state.messages = [
                Message(role=MessageRole.USER, content=str(run.request.input or ""))
            ]
            final_messages = final_state.get("messages", [])
            if final_messages:
                last = final_messages[-1]
                text = str(last.get("content") or "")
                run.events.append(self._event(run, EventType.TEXT_COMPLETED, {"text": text}))
            run.state.status = RunStatus.COMPLETED
            run.events.append(self._event(run, EventType.RUN_COMPLETED, {"status": "completed"}))
            run.done = True
            return []
        except asyncio.CancelledError:
            run.state.status = RunStatus.CANCELED
            run.done = True
            return [self._event(run, EventType.RUN_CANCELED, {"status": "cancelled"})]
        except GraphInterrupt as exc:
            # checkpoint=False 路径（无 Checkpointer 时 interrupt 直接抛出）。
            run.state.status = RunStatus.AWAITING_APPROVAL
            run.done = False
            run.events.append(
                self._event(
                    run, EventType.RUN_INTERRUPTED,
                    {"status": "awaiting_approval", "reason": f"graph_interrupt: {exc}"},
                )
            )
            return []
        except Exception as exc:  # noqa: BLE001
            run.state.status = RunStatus.FAILED
            run.done = True
            run.events.append(
    self._event(run, EventType.RUN_FAILED, {"status": "failed", "error": str(exc)})
)
            return []

    # ---------------------------------------------------------------- graph

    def _build_graph(self, run: _EngineRun):
        spec = run.compiled.spec

        async def reason(state: _GraphState) -> _GraphState:
            state["turn_count"] += 1
            if state["turn_count"] > _MAX_REASONING_TURNS:
                raise RuntimeError(f"reasoning exceeded {_MAX_REASONING_TURNS} turns")
            run.events.append(
                self._event(run, EventType.MODEL_CALL_STARTED, {"model": spec.model.profile_ref})
            )
            turn: HarnessReasoningTurn = await self._reasoner.complete(
                model=spec.model.profile_ref,
                prompt=spec.prompt.instructions or "",
                messages=tuple(state["messages"]),
                tools=list(self._tools.values()),
            )
            run.events.append(
                self._event(run, EventType.MODEL_CALL_COMPLETED, {"model": spec.model.profile_ref})
            )
            if turn.tool_calls:
                state["messages"].append(
                    {
                        "role": "assistant",
                        "content": turn.final_text,
                        "tool_calls": [
                            {
                                "id": call.call_id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                                },
                            }
                            for call in turn.tool_calls
                        ],
                    }
                )
                state["pending_tool_calls"] = [
                    {"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
                    for c in turn.tool_calls
                ]
                state["route"] = "tool_calls"
                return state
            state["messages"].append({"role": "assistant", "content": turn.final_text or ""})
            state["route"] = "final"
            return state

        async def tool_calls(state: _GraphState) -> _GraphState:
            for pending in state["pending_tool_calls"]:
                call_id, name = pending["call_id"], pending["name"]
                decision = "approved"
                if name in self._approval_required:
                    # Approval interrupt（plan §11.2）：interrupt() 首次抛
                    # GraphInterrupt 暂停；resume 后此处返回审批决定。
                    decision = interrupt(
                        {
                            "call_id": call_id,
                            "name": name,
                            "args": pending["arguments"],
                            "risk": "high",
                        }
                    )
                if decision != "approved":
                    run.events.append(
                        self._event(
                            run, EventType.TOOL_CALL_BEGIN,
                            {"call_id": call_id, "name": name, "args": pending["arguments"]},
                        )
                    )
                    run.events.append(
                        self._event(
                            run, EventType.TOOL_CALL_END,
                            {"call_id": call_id, "name": name,
                             "error": f"approval {decision}"},
                        )
                    )
                    state["messages"].append(
                        {"role": "tool", "tool_call_id": call_id, "name": name,
                         "content": f"[denied] approval decision: {decision}"}
                    )
                    continue
                run.events.append(
                    self._event(
                        run, EventType.TOOL_CALL_BEGIN,
                        {"call_id": call_id, "name": name, "args": pending["arguments"]},
                    )
                )
                result = await self._invoke_tool(name, pending["arguments"])
                run.events.append(
                    self._event(
                        run, EventType.TOOL_CALL_END,
                        {"call_id": call_id, "name": name, "result": result},
                    )
                )
                state["messages"].append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": (
    result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
),
                    }
                )
            state["pending_tool_calls"] = []
            state["route"] = "reason"
            return state

        def route(state: _GraphState) -> str:
            return state["route"]

        builder = StateGraph(_GraphState)
        builder.add_node("reason", reason)
        builder.add_node("tool_calls", tool_calls)
        builder.add_edge(START, "reason")
        builder.add_conditional_edges("reason", route, {"tool_calls": "tool_calls", "final": END})
        builder.add_edge("tool_calls", "reason")
        graph = builder.compile(checkpointer=self._checkpointer)
        return graph

    async def _invoke_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise RuntimeError(
                f"engine tool {name!r} is not available; it may be filtered or unpublished"
            )
        return await tool(arguments)

    # --------------------------------------------------------------- cancel

    async def cancel(self, handle: RunHandle) -> CancelResult:
        run = self._runs.get(handle.run_id)
        if run is None or run.done:
            return CancelResult.NOT_RUNNING
        if run.task is None:
            run.cancel_requested = True
            return CancelResult.PENDING_CANCEL_RECORDED
        if run.task.done():
            return CancelResult.NOT_RUNNING
        run.task.cancel()
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    # --------------------------------------------------------------- resume

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        run = self._require_run(handle)
        if run.state.status is not RunStatus.AWAITING_APPROVAL:
            raise ExecutionEngineError(
                f"resume 仅支持 awaiting_approval 状态，当前 {run.state.status.value}"
            )
        if self._checkpointer is None:
            raise ExecutionEngineError(
                "resume 需要 Checkpointer：interrupt 状态由 Checkpoint 持久化"
            )
        decision = "approved"
        if payload is not None:
            decision = str(payload.data) if payload.data is not None else "approved"
            run.events.append(
                self._event(
                    run,
                    EventType.APPROVAL_RESOLVED,
                    {
                        "approval_id": f"ap-{run.handle.run_id}",
                        "call_id": payload.call_id or "",
                        "decision": decision,
                    },
                )
            )
        run.state.status = RunStatus.RUNNING
        run.events.append(
            self._event(
                run, EventType.RUN_RESUMED,
                {"target": target.id, "resume_kind": "approval_decision"},
            )
        )
        run.task = asyncio.create_task(self._execute(run, resume_command=Command(resume=decision)))
        return handle

    # ------------------------------------------------------------- snapshot

    async def snapshot_state(self, handle: RunHandle) -> HarnessState | None:
        run = self._runs.get(handle.run_id)
        return run.state if run else None

    def capabilities(self) -> EngineCapabilityMatrix:
        return EngineCapabilityMatrix(
            cancel=EngineCapability(supported=True),
            resume=EngineCapability(supported=True, reason="approval channel only (Phase 3: full)"),
            checkpoint=EngineCapability(
                supported=self._checkpointer is not None,
                reason=None if self._checkpointer else "no checkpointer injected",
            ),
            interrupt=EngineCapability(supported=True, reason="approval channel only"),
            durable_across_process=EngineCapability(
                supported=self._checkpointer is not None and _is_durable(self._checkpointer),
                reason=None,
            ),
            streaming=EngineCapability(supported=True),
        )

    async def close(self, handle: RunHandle) -> None:
        run = self._runs.pop(handle.run_id, None)
        if run and run.task and not run.task.done():
            run.task.cancel()
            await asyncio.gather(run.task, return_exceptions=True)

    # ------------------------------------------------------------- helpers

    def _emit_context_plan(self, run: _EngineRun) -> None:
        """Phase 2：注入 ContextEngine 时发出 CONTEXT_PLANNED（预算随窗口动态计算）。"""
        if self._context_engine is None:
            return
        from ksadk.harness.context_engine import ContextRequest, resolve_context_window

        raw_window = run.request.metadata.get("context_window_tokens")
        window, source = resolve_context_window(
            model_profile_window=int(raw_window) if isinstance(raw_window, (int, float)) else None
        )
        plan = self._context_engine.plan(
            ContextRequest(
                spec=run.compiled.spec,
                state=run.state,
                user_input=str(run.request.input or ""),
                context_window_tokens=window,
            )
        )
        run.events.append(
            self._event(
                run,
                EventType.CONTEXT_PLANNED,
                {
                    "budget_tokens": plan.budget.max_input_tokens,
                    "sections": dict(plan.tokens_by_kind),
                    "window_source": source,
                    "context_window_tokens": window,
                    "planned_input_tokens": plan.planned_input_tokens,
                },
            )
        )

    def _require_run(self, handle: RunHandle) -> _EngineRun:
        try:
            return self._runs[handle.run_id]
        except KeyError:
            raise KeyError(f"unknown engine run: {handle.run_id}") from None

    def _event(self, run: _EngineRun, event_type: str, payload: dict[str, Any]) -> RuntimeEvent:
        run.seq += 1
        return RuntimeEvent.create(
            event_type,
            agent_id=run.state.agent_id,
            user_id=run.state.user_id,
            session_id=run.state.session_id,
            invocation_id=run.handle.run_id,
            seq_id=run.seq,
            payload=payload,
        )


def _is_durable(checkpointer: BaseCheckpointSaver) -> bool:
    """SQLite/Postgres Checkpointer 视为跨进程持久；MemorySaver 不是。"""
    from langgraph.checkpoint.memory import InMemorySaver, MemorySaver

    return not isinstance(checkpointer, (MemorySaver, InMemorySaver))


__all__ = ["ManagedLangGraphEngine"]
