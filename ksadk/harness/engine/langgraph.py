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
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ksadk.harness.engine.base import (
    CompiledHarness,
    EngineCapability,
    EngineCapabilityMatrix,
    ExecutionEngineError,
)
from ksadk.harness.engine.context_pipeline import EngineContextPipeline
from ksadk.harness.engine.spans import wrap_node_span
from ksadk.harness.engine.thread_ids import encode_thread_id
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.loop import (
    ReasonInput,
    ToolCallInput,
    execute_tool_calls,
    reason_turn_async,
)
from ksadk.harness.loop.reason import ReasoningLimitError
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn, LiteLLMHarnessReasoner
from ksadk.harness.spec import HarnessSpec
from ksadk.harness.state import HarnessState, Message, MessageRole, RunStatus
from ksadk.harness.strategies import ExecutionStrategyRegistry
from ksadk.runtime import (
    CancelResult,
    PauseResult,
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
    pause_requested: bool = False
    done: bool = False
    started_emitted: bool = False
    #: 最近一次 ContextManifest（Actual Token 由 usage 回填，长任务方案 §6.2）。
    context_manifest: Any | None = None
    #: 本 Run 的 CompactionRecord 列表（长任务方案 §6.4）。
    compaction_records: list[Any] = field(default_factory=list)


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
        strategy_registry: ExecutionStrategyRegistry | None = None,
        capability_runtime: Any | None = None,
        sub_agents: dict[str, Any] | None = None,
        memory_runtime: Any | None = None,
        event_sink: Callable[[str, str, RuntimeEvent], None] | None = None,
    ) -> None:
        self._reasoner = reasoner or LiteLLMHarnessReasoner()
        self._checkpointer = checkpointer
        # 集成项 4：Strategy Registry 真正参与 compile()——拓扑来自注册表。
        self._strategy_registry = strategy_registry or ExecutionStrategyRegistry()
        self._tenant_id = tenant_id
        self._tools = tools or {}
        self._approval_required = approval_required or set()
        self._context_engine = context_engine
        # 收口 2：统一 CapabilityRuntime（Policy 决策 + Receipt 幂等）。
        # 为 None 时 tool_calls 节点回退静态 approval_required 集合。
        self._capability_runtime = capability_runtime
        # 收口 6：子 Agent（名称 → SubAgentSpec）；多 Agent 作为可选能力。
        self._sub_agents = sub_agents or {}
        # 长任务方案 §6.4：压缩前受控 Memory Flush 用的 Memory Runtime（可选）。
        self._memory_runtime = memory_runtime
        # P3 补强：事件出口回调（session_id, run_id, event）——洞察登记处
        # （ksadk.harness.insights）由此拿到完整事件流，供 Studio API 消费。
        self._event_sink = event_sink
        # Context 构建管线（规划/压缩/组装/Manifest 投影，见 context_pipeline）。
        self._context_pipeline = (
            EngineContextPipeline(
                context_engine=self._context_engine,
                reasoner=self._reasoner,
                event_fn=self._event,
                memory_runtime=self._memory_runtime,
            )
            if self._context_engine is not None
            else None
        )
        # 最近 compile 的 Spec（子 Agent 派生 child spec 用）。
        self._current_spec: HarnessSpec | None = None
        # 收口 6：子 Agent 事件缓冲（run_id → 待并入父流的子事件）。
        # 子 Agent 在 tool_calls 节点内联执行，但事件必须等本节点自身的
        # tool.call.begin/end 落定后统一重排并入，保证 seq 单调。
        self._pending_child_events: dict[str, list[RuntimeEvent]] = {}
        self._runs: dict[str, _EngineRun] = {}

    # ------------------------------------------------------------- compile

    async def compile(self, spec: HarnessSpec) -> CompiledHarness:
        # 集成项 4：拓扑由 Strategy Registry 按 spec.execution_strategy 编译，
        # 引擎不再硬编码 single_agent_plan。
        plan = self._strategy_registry.compile(spec, strategy=spec.execution_strategy.kind.value)
        if spec.execution_strategy.kind.value == self._strategy_registry.default():
            ExecutionStrategyRegistry.assert_single_agent_purity(plan)
        self._current_spec = spec
        return CompiledHarness(
            spec=spec,
            plan=plan,
            engine_kind="managed-langgraph",
        )

    # --------------------------------------------------------------- start

    async def start(self, request: StartRequest, compiled: CompiledHarness) -> RunHandle:
        run_id = str(request.metadata.get("invocation_id") or f"mle_{uuid.uuid4().hex[:16]}")
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

    # -------------------------------------------------------------- attach

    async def attach(self, handle: RunHandle, compiled: CompiledHarness) -> RunHandle:
        """跨进程恢复（收口 3）：从持久 Checkpoint 重建 _EngineRun。

        状态从 durable Checkpointer 的 graph snapshot 推断：存在未决 interrupt
        → awaiting_approval；存在 pending 节点 → paused；无 Checkpoint → 诚实报错。
        事件队列从空开始（历史事件已由平台 EventStore 持久化）。
        """
        if handle.run_id in self._runs:
            return handle
        if self._checkpointer is None:
            raise ExecutionEngineError(
                "attach 需要 durable Checkpointer：跨进程恢复依赖持久 Checkpoint"
            )
        thread_id = str(handle.native_ref.get("thread_id") or "")
        if not thread_id:
            raise ExecutionEngineError(
                f"attach 失败：handle.native_ref 缺少 thread_id（run {handle.run_id!r}）"
            )
        # 持久 handle 可能不带 user_id（旧版本句柄）：用占位值，事件锚点以
        # Checkpoint 内的图状态为准，跨进程身份由 thread_id 保证。
        user_id = str(handle.native_ref.get("user_id") or "unknown")
        agent_id = str(handle.native_ref.get("agent_id") or compiled.spec.agent_revision_ref)
        state = HarnessState(
            tenant_id=self._tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            session_id=handle.session_id,
            run_id=handle.run_id,
            status=RunStatus.PAUSED,
        )
        run = _EngineRun(
            handle=handle,
            request=StartRequest(
                agent_id=agent_id,
                user_id=user_id,
                session_id=handle.session_id,
                input="",
                runtime_type="managed-langgraph",
                metadata={"invocation_id": handle.run_id},
            ),
            compiled=compiled,
            state=state,
            thread_id=thread_id,
        )
        run.started_emitted = True  # run.started 已在首个进程发出
        run.done = False
        # 从 Checkpoint snapshot 推断挂起状态。
        graph = self._build_graph(run)
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        if snapshot is None or not snapshot.next:
            raise ExecutionEngineError(f"attach 失败：thread {thread_id!r} 无未决 Checkpoint")
        tasks = getattr(snapshot, "tasks", None) or ()
        if isinstance(tasks, dict):
            tasks = tuple(tasks.values())
        has_interrupt = any(getattr(task, "interrupts", None) for task in tasks)
        run.state.status = RunStatus.AWAITING_APPROVAL if has_interrupt else RunStatus.PAUSED
        self._runs[handle.run_id] = run
        return handle

    # -------------------------------------------------------------- stream

    def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        return self._stream(handle)

    async def _stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        run = self._require_run(handle)
        task_finished = run.task is None or run.task.done()
        if run.state.status in (RunStatus.AWAITING_APPROVAL, RunStatus.PAUSED) and task_finished:
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
                    event = run.events.pop(0)
                    self._emit_insight(run, event)
                    yield event
                if run.task.done():
                    break
                await asyncio.sleep(0)
            remaining = await run.task
            for event in remaining:
                self._emit_insight(run, event)
                yield event
        except asyncio.CancelledError:
            run.done = True
            yield self._event(run, EventType.RUN_CANCELED, {"status": "cancelled"})

    def _emit_insight(self, run: _EngineRun, event: RuntimeEvent) -> None:
        """事件出口统一回调（洞察登记处 / 审计侧消费；异常不阻断主流程）。"""
        if self._event_sink is None:
            return
        try:
            self._event_sink(run.state.session_id, run.handle.run_id, event)
        except Exception:  # noqa: BLE001 - 登记失败不阻断对话主流程
            pass

    async def _execute(
        self,
        run: _EngineRun,
        resume_command: Command | None = None,
        *,
        resume_from_checkpoint: bool = False,
    ) -> list[RuntimeEvent]:
        spec = run.compiled.spec
        run.state.status = RunStatus.RUNNING
        if not run.started_emitted:
            run.started_emitted = True
            run.events.append(self._event(run, EventType.RUN_STARTED, {"status": "in_progress"}))
            # 收口 5：事件树根——Agent 生命周期（主 Agent 与未来子 Agent 同构）。
            run.events.append(
                self._event(run, EventType.AGENT_STARTED, {"agent_id": run.state.agent_id})
            )
        try:
            graph = self._build_graph(run)
            instructions = spec.prompt.instructions or ""
            config = {"configurable": {"thread_id": run.thread_id}}
            invoke_input: _GraphState | Command | None
            if resume_command is not None:
                invoke_input = resume_command
            elif resume_from_checkpoint:
                # 通用 pause 恢复：None → LangGraph 从最近 Checkpoint 续跑。
                invoke_input = None
            else:
                # 收口 1：ContextEngine 真正控制首次模型输入（规划 + 主动/紧急压缩
                # + 组装）；未注入时回退旧的 history 拼接路径。
                planned = await self._prepare_context(run, instructions)
                if planned is not None:
                    conversation = planned
                else:
                    history = run.request.metadata.get("conversation_history") or []
                    conversation = [{"role": "system", "content": instructions}]
                    if isinstance(history, list) and history:
                        # 宿主（如 Studio Playground）注入的会话历史已含当前输入。
                        conversation.extend(dict(m) for m in history if isinstance(m, dict))
                    else:
                        conversation.append(
                            {"role": "user", "content": str(run.request.input or "")}
                        )
                invoke_input = {
                    "messages": conversation,
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
                        run,
                        EventType.RUN_INTERRUPTED,
                        {"status": "awaiting_approval", "reason": "tool_approval"},
                    )
                )
                return []
            # 影子状态消息（§6.2.1）：完整对话消息投影，供 Transcript 持久化。
            final_messages = final_state.get("messages", [])
            run.state.messages = [
                Message(
                    role=MessageRole(m.get("role", "user")),
                    content=str(m.get("content") or ""),
                    tool_call_id=(str(m.get("tool_call_id")) if m.get("tool_call_id") else None),
                    name=str(m["name"]) if m.get("name") else None,
                )
                for m in final_messages
                if isinstance(m, dict)
            ]
            if final_messages:
                last = final_messages[-1]
                text = str(last.get("content") or "")
                run.events.append(
                    self._event(run, EventType.TEXT_COMPLETED, {"text": text}, phase="final_answer")
                )
            run.state.status = RunStatus.COMPLETED
            run.events.append(
                self._event(
                    run,
                    EventType.AGENT_COMPLETED,
                    {"agent_id": run.state.agent_id, "status": "completed"},
                )
            )
            run.events.append(self._event(run, EventType.RUN_COMPLETED, {"status": "completed"}))
            run.done = True
            return []
        except asyncio.CancelledError:
            if run.pause_requested:
                # 通用 pause：非终止性挂起，Checkpoint 持久化后可 resume。
                run.pause_requested = False
                run.state.status = RunStatus.PAUSED
                run.done = False
                run.events.append(
                    self._event(
                        run,
                        EventType.RUN_INTERRUPTED,
                        {"status": "paused", "reason": "pause_requested"},
                    )
                )
                return []
            run.state.status = RunStatus.CANCELED
            run.done = True
            return [self._event(run, EventType.RUN_CANCELED, {"status": "cancelled"})]
        except GraphInterrupt as exc:
            # checkpoint=False 路径（无 Checkpointer 时 interrupt 直接抛出）。
            run.state.status = RunStatus.AWAITING_APPROVAL
            run.done = False
            run.events.append(
                self._event(
                    run,
                    EventType.RUN_INTERRUPTED,
                    {"status": "awaiting_approval", "reason": f"graph_interrupt: {exc}"},
                )
            )
            return []
        except Exception as exc:  # noqa: BLE001
            run.state.status = RunStatus.FAILED
            run.done = True
            run.events.append(
                self._event(
                    run,
                    EventType.AGENT_COMPLETED,
                    {"agent_id": run.state.agent_id, "status": "failed"},
                )
            )
            run.events.append(
                self._event(run, EventType.RUN_FAILED, {"status": "failed", "error": str(exc)})
            )
            return []

    # ---------------------------------------------------------------- graph

    def _build_graph(self, run: _EngineRun):
        spec = run.compiled.spec

        async def reason(state: _GraphState) -> _GraphState:
            state["turn_count"] += 1
            # 收口 5：Turn 区间事件（模型/工具/usage 事件按 seq 落在区间内）。
            turn_id = f"{run.handle.run_id}:t{state['turn_count']}"
            run.events.append(
                self._event(
                    run,
                    EventType.TURN_STARTED,
                    {"turn_id": turn_id, "turn_number": state["turn_count"]},
                )
            )
            try:
                out = await reason_turn_async(
                    state["turn_count"],
                    ReasonInput(
                        model_ref=spec.model.profile_ref,
                        instructions=spec.prompt.instructions or "",
                        messages=state["messages"],
                        tools=list(self._tools.values()) + list(self._sub_agents.values()),
                        reasoner=self._reasoner,
                        agent_id=run.state.agent_id,
                        user_id=run.state.user_id,
                        session_id=run.state.session_id,
                        run_id=run.handle.run_id,
                        seq_start=run.seq,
                        max_turns=_MAX_REASONING_TURNS,
                    ),
                )
            except ReasoningLimitError as exc:
                raise RuntimeError(str(exc)) from exc
            for ev in out.events:
                # 长任务方案 §6.3：Actual Token 回填到最近一次 ContextManifest
                # （actual_usage_ref 指向 usage.reported 事件）。同时在 usage
                # 事件 payload 落 manifest_id —— 事件流层面可直接查询
                # 「某次模型调用实际对应哪个 Manifest」（§6.2 闭环）。
                if ev.event_type == EventType.USAGE_REPORTED and run.context_manifest:
                    ev.payload["manifest_id"] = run.context_manifest.manifest_id
                    run.context_manifest = run.context_manifest.with_actual(
                        input_tokens=int(ev.payload.get("input_tokens") or 0),
                        output_tokens=int(ev.payload.get("output_tokens") or 0),
                        usage_ref=ev.event_id,
                    )
                run.events.append(ev)
                run.seq = max(run.seq, ev.seq_id)
            state["messages"].extend(out.new_messages)
            state["pending_tool_calls"] = out.pending_tool_calls
            state["route"] = out.route
            run.events.append(
                self._event(
                    run,
                    EventType.TURN_COMPLETED,
                    {"turn_id": turn_id, "turn_number": state["turn_count"]},
                )
            )
            return state

        async def tool_calls(state: _GraphState) -> _GraphState:
            class _GraphApprovalResolver:
                # LangGraph interrupt 同步语义：首次抛 GraphInterrupt，resume 后返回审批决定。
                def request(self, *, call_id, name, arguments):  # type: ignore[no-untyped-def]
                    return interrupt(
                        {"call_id": call_id, "name": name, "args": arguments, "risk": "high"}
                    )

            engine = self

            class _EngineToolExecutor:
                async def execute(self, name, arguments):  # type: ignore[no-untyped-def]
                    return await engine._invoke_tool(name, arguments, run=run)

            out = await execute_tool_calls(
                ToolCallInput(
                    pending_tool_calls=state["pending_tool_calls"],
                    approval_required=frozenset(engine._approval_required),
                    capability_runtime=engine._capability_runtime,
                    tenant_id=engine._tenant_id,
                    approval_resolver=_GraphApprovalResolver(),
                    tool_executor=_EngineToolExecutor(),
                    agent_id=run.state.agent_id,
                    user_id=run.state.user_id,
                    session_id=run.state.session_id,
                    run_id=run.handle.run_id,
                    seq_start=run.seq,
                    working_context=run.state.working_context,
                )
            )
            for ev in out.events:
                run.events.append(ev)
                run.seq = max(run.seq, ev.seq_id)
            # 收口 6：子 Agent 事件统一重排并入（tool.call.begin/end 之后）。
            from ksadk.harness.subagent import resequence_child_events

            resequence_child_events(run, self._pending_child_events.pop(run.handle.run_id, []))
            state["messages"].extend(out.new_messages)
            if out.working_context is not None:
                run.state.working_context = out.working_context
            state["pending_tool_calls"] = out.pending_tool_calls
            state["route"] = out.route
            return state

        def route(state: _GraphState) -> str:
            return state["route"]

        async def plan_node(state: _GraphState) -> _GraphState:
            """plan-execute 拓扑的规划节点：一次无工具模型调用产出执行计划。"""
            turn: HarnessReasoningTurn = await self._reasoner.complete(
                model=spec.model.profile_ref,
                prompt=(spec.prompt.instructions or "") + "\n请先给出分步执行计划，再开始执行。",
                messages=tuple(state["messages"]),
                tools=[],
            )
            state["messages"].append(
                {"role": "assistant", "content": "【执行计划】\n" + (turn.final_text or "")}
            )
            return state

        async def review_node(state: _GraphState) -> _GraphState:
            """plan-execute-review 拓扑的独立审查节点：对最终回答做一次批判。"""
            messages = list(state["messages"]) + [
                {"role": "user", "content": "请审查以上回答的准确性与遗漏，指出问题（如有）。"}
            ]
            turn: HarnessReasoningTurn = await self._reasoner.complete(
                model=spec.model.profile_ref,
                prompt=spec.prompt.instructions or "",
                messages=tuple(messages),
                tools=[],
            )
            state["messages"].append(
                {"role": "assistant", "content": "【审查】\n" + (turn.final_text or "")}
            )
            return state

        def passthrough(state: _GraphState) -> _GraphState:
            # prepare_context / execute：上下文规划已在 _emit_context_plan 完成，
            # execute 由 reason 循环承担——节点存在是为了拓扑可观测。
            return state

        # 集成项 4：按 ExecutionPlan 组图（不再硬编码 single-agent 拓扑）。
        node_impls = {
            "reason": reason,
            "tool_calls": tool_calls,
            "plan": plan_node,
            "review": review_node,
            "prepare_context": passthrough,
            "execute": passthrough,
        }

        node_impls = {
            name: wrap_node_span(self, run, name, impl) for name, impl in node_impls.items()
        }
        plan = run.compiled.plan
        builder = StateGraph(_GraphState)
        for node in plan.nodes:
            if node == "final":
                continue  # final 是出口，不是图节点。
            impl = node_impls.get(node)
            if impl is None:
                raise ExecutionEngineError(f"引擎不支持拓扑节点: {node!r}")
            builder.add_node(node, impl)
        edges = plan.edges or ()
        successors_of = {src: [dst for s, dst in edges if s == src] for src, _ in edges}
        # 入口：无入边的节点接 START（保持 plan.nodes 顺序稳定）。
        incoming = {dst for _, dst in edges}
        for node in plan.nodes:
            if node != "final" and node not in incoming:
                builder.add_edge(START, node)
        # reason 的出边是条件路由：有工具调用走 tool_calls，否则走出口/审查。
        reason_successors = successors_of.get("reason", [])
        exit_targets = [dst for dst in reason_successors if dst != "tool_calls"]
        exit_target = exit_targets[0] if exit_targets else "final"
        reason_mapping = {}
        if "tool_calls" in reason_successors:
            reason_mapping["tool_calls"] = "tool_calls"
        reason_mapping["final"] = END if exit_target == "final" else exit_target
        if "reason" in node_impls and "reason" in plan.nodes and "reason" != exit_target:
            builder.add_conditional_edges("reason", route, reason_mapping)
        for src, dst in edges:
            if src == "reason":
                continue  # 已由条件边覆盖。
            if dst == "final":
                if src in node_impls and src != "reason":
                    builder.add_edge(src, END)
                continue
            builder.add_edge(src, dst)
        graph = builder.compile(checkpointer=self._checkpointer)
        return graph

    async def _invoke_tool(self, name: str, arguments: dict[str, Any], *, run: Any = None) -> Any:
        # 收口 6：子 Agent 即工具——内联运行到完成，子事件并入父流。
        sub = self._sub_agents.get(name)
        if sub is not None and run is not None:
            from ksadk.harness.subagent import run_subagent

            text, child_events = await run_subagent(
                engine=self,
                parent_run=run,
                sub=sub,
                task=str((arguments or {}).get("task") or ""),
            )
            self._pending_child_events.setdefault(run.handle.run_id, []).extend(child_events)
            return text
        tool = self._tools.get(name)
        if tool is None:
            raise RuntimeError(
                f"engine tool {name!r} is not available; it may be filtered or unpublished"
            )
        result = await tool(arguments)
        # 缺口 5：工具产出的 Artifact → artifact.created 事件（与子事件同
        # 缓冲，tool.call.end 之后统一重排并入，seq 单调）。
        drain = getattr(tool, "drain_artifacts", None)
        if callable(drain) and run is not None:
            for artifact in drain():
                self._pending_child_events.setdefault(run.handle.run_id, []).append(
                    RuntimeEvent.create(
                        EventType.ARTIFACT_CREATED,
                        agent_id=run.state.agent_id,
                        user_id=run.state.user_id,
                        session_id=run.state.session_id,
                        invocation_id=run.handle.run_id,
                        seq_id=0,
                        payload={
                            "name": str(artifact.get("name") or ""),
                            "version": int(artifact.get("version") or 1),
                            "uri": str(artifact.get("uri") or ""),
                            "mime": str(artifact.get("mime") or "text/plain"),
                        },
                    )
                )
        return result

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

    # --------------------------------------------------------------- pause

    async def pause(self, handle: RunHandle) -> PauseResult:
        """通用 pause：取消当前回合但保留状态，Checkpointer 持久化后可 resume。"""
        run = self._runs.get(handle.run_id)
        if run is None or run.done:
            return PauseResult.NOT_RUNNING
        if run.state.status not in (RunStatus.RUNNING, RunStatus.PENDING):
            return PauseResult.NOT_RUNNING
        if self._checkpointer is None:
            return PauseResult.NOT_SUPPORTED
        if run.task is None or run.task.done():
            # 尚未启动流式执行：直接置 PAUSED，start 后由 stream/resume 驱动。
            run.state.status = RunStatus.PAUSED
            run.events.append(
                self._event(
                    run,
                    EventType.RUN_INTERRUPTED,
                    {"status": "paused", "reason": "pause_requested"},
                )
            )
            return PauseResult.PAUSED_ACTIVE_TURN
        run.pause_requested = True
        run.task.cancel()
        return PauseResult.PAUSED_ACTIVE_TURN

    # --------------------------------------------------------------- resume

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        run = self._require_run(handle)
        if run.state.status is RunStatus.PAUSED:
            if self._checkpointer is None:
                raise ExecutionEngineError(
                    "resume 需要 Checkpointer：pause 状态由 Checkpoint 持久化"
                )
            # 通用恢复：从最近 Checkpoint 续跑（非审批通道）。
            run.state.status = RunStatus.RUNNING
            run.events.append(
                self._event(
                    run,
                    EventType.RUN_RESUMED,
                    {"target": target.id, "resume_kind": "checkpoint"},
                )
            )
            run.task = asyncio.create_task(self._execute(run, resume_from_checkpoint=True))
            return handle
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
                run,
                EventType.RUN_RESUMED,
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
            resume=EngineCapability(
                supported=True, reason="approval + checkpoint (paused) channels"
            ),
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

    async def _prepare_context(
        self, run: _EngineRun, instructions: str
    ) -> list[dict[str, Any]] | None:
        """Context 构建管线委托（长任务方案 §6/§8，实现见 context_pipeline）。"""
        if self._context_engine is None:
            return None
        return await self._context_pipeline.prepare_context(run, instructions)

    def _require_run(self, handle: RunHandle) -> _EngineRun:
        try:
            return self._runs[handle.run_id]
        except KeyError:
            raise KeyError(f"unknown engine run: {handle.run_id}") from None

    def _event(
        self, run: _EngineRun, event_type: str, payload: dict[str, Any], *, phase: str | None = None
    ) -> RuntimeEvent:
        run.seq += 1
        return RuntimeEvent.create(
            event_type,
            agent_id=run.state.agent_id,
            user_id=run.state.user_id,
            session_id=run.state.session_id,
            invocation_id=run.handle.run_id,
            seq_id=run.seq,
            payload=payload,
            phase=phase,
        )


def _is_durable(checkpointer: BaseCheckpointSaver) -> bool:
    """SQLite/Postgres Checkpointer 视为跨进程持久；MemorySaver 不是。"""
    from langgraph.checkpoint.memory import InMemorySaver, MemorySaver

    return not isinstance(checkpointer, (MemorySaver, InMemorySaver))


__all__ = ["ManagedLangGraphEngine"]
