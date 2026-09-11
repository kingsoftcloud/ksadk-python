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
from typing import Any, AsyncIterator, Callable

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from ksadk.harness.engine.base import (
    CompiledHarness,
    EngineCapability,
    EngineCapabilityMatrix,
    ExecutionEngineError,
)
from ksadk.harness.engine.context_pipeline import EngineContextPipeline
from ksadk.harness.engine.mcp_disclosure import (
    McpDisclosureBridge,
    McpDisclosureCursors,
)
from ksadk.harness.engine.run_types import _EngineRun, _GraphState
from ksadk.harness.engine.skill_disclosure import SkillDisclosureBridge
from ksadk.harness.engine.thread_ids import encode_thread_id
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.mcp_runtime import McpCapabilityRuntime
from ksadk.harness.prompt_cache import PromptCacheTracker
from ksadk.harness.reasoner import HarnessReasoner, LiteLLMHarnessReasoner
from ksadk.harness.run_control import (
    RunController,
    RunControlStop,
    run_control_spec_from_config,
)
from ksadk.harness.skill_runtime import SkillRuntime
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
        skill_runtime: SkillRuntime | None = None,
        mcp_runtime: McpCapabilityRuntime | None = None,
        artifact_store: Any | None = None,
        mcp_offload_policy: Any | None = None,
        event_sink: Callable[[str, str, RuntimeEvent], None] | None = None,
        max_reasoning_turns: int = _MAX_REASONING_TURNS,
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
        if max_reasoning_turns < 1:
            raise ValueError("max_reasoning_turns 必须为正整数")
        self._max_reasoning_turns = max_reasoning_turns
        # 长任务方案 §6.4：压缩前受控 Memory Flush 用的 Memory Runtime（可选）。
        self._memory_runtime = memory_runtime
        self._prompt_cache_tracker = PromptCacheTracker()
        # SkillRuntime 只消费已绑定、已校验的 Skill 内容；L0 摘要常驻动态
        # Context，L1/L2/L3 由默认 Agent Loop 的受限工具渐进披露。
        self._skill_runtime = skill_runtime
        self._skill_disclosure = SkillDisclosureBridge(skill_runtime)
        # P0 MCP Deferred Tool Loading：McpCapabilityRuntime 复用健康缓存/
        # 熔断/tools 缓存，披露层级（L0 目录/L1 列表/L2 Schema/L3 调用）
        # 由 McpDisclosureBridge 接入默认 Loop。
        self._mcp_runtime = mcp_runtime
        # P1 大结果 Offload：L3 结果超阈值/命中敏感策略 → Artifact Store 外置。
        self._mcp_disclosure = McpDisclosureBridge(
            mcp_runtime,
            artifact_store=artifact_store,
            offload_policy=mcp_offload_policy,
        )
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
                prompt_cache_tracker=self._prompt_cache_tracker,
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
        # 并行子 Agent 按 call_id 单独缓冲，节点结束时再按模型原始调用顺序
        # 合并，避免墙钟完成顺序造成 Trace/重放结果不确定。
        self._pending_subagent_events: dict[str, dict[str, list[RuntimeEvent]]] = {}
        # parent_run_id -> call_id -> (child_engine, child_handle)。父取消必须先
        # 显式传播到所有活动子 Run，不能只依赖 asyncio 任务树的隐式取消。
        self._active_subagent_runs: dict[str, dict[str, tuple[Any, RunHandle]]] = {}
        self._runs: dict[str, _EngineRun] = {}

    # ------------------------------------------------------------- compile

    async def compile(
        self, spec: HarnessSpec, *, tools: dict[str, Any] | None = None
    ) -> CompiledHarness:
        # 集成项 4：拓扑由 Strategy Registry 按 spec.execution_strategy 编译，
        # 引擎不再硬编码 single_agent_plan。
        plan = self._strategy_registry.compile(spec, strategy=spec.execution_strategy.kind.value)
        if spec.execution_strategy.kind.value == self._strategy_registry.default():
            ExecutionStrategyRegistry.assert_single_agent_purity(plan)
        max_parallel = spec.execution_strategy.config.get("max_parallel_subagents", 4)
        if isinstance(max_parallel, bool) or not isinstance(max_parallel, int):
            raise ExecutionEngineError("max_parallel_subagents 必须是整数")
        if not 1 <= max_parallel <= 32:
            raise ExecutionEngineError("max_parallel_subagents 必须在 1..32 之间")
        failure_mode = spec.execution_strategy.config.get("subagent_failure_mode", "partial")
        if failure_mode not in {"partial", "fail_fast"}:
            raise ExecutionEngineError("subagent_failure_mode 必须是 partial 或 fail_fast")
        try:
            run_control_spec_from_config(spec.execution_strategy.config)
        except ValueError as exc:
            raise ExecutionEngineError(f"run_control 配置无效: {exc}") from exc
        declared_sub_agents = {binding.name for binding in spec.sub_agents}
        self._skill_disclosure.validate_bindings(
            spec,
            tool_names=set(self._tools if tools is None else tools),
            sub_agent_names=set(self._sub_agents) | declared_sub_agents,
        )
        self._mcp_disclosure.validate_bindings(
            spec,
            tool_names=set(self._tools if tools is None else tools),
            sub_agent_names=set(self._sub_agents) | declared_sub_agents,
        )
        self._current_spec = spec
        return CompiledHarness(
            spec=spec,
            plan=plan,
            engine_kind="managed-langgraph",
        )

    # --------------------------------------------------------------- start

    async def start(
        self,
        request: StartRequest,
        compiled: CompiledHarness,
        *,
        tools: dict[str, Any] | None = None,
        execution_policy: Any = None,
        execution_policy_resolver: Any = None,
        execution_policy_request: StartRequest | None = None,
    ) -> RunHandle:
        run_id = str(
            request.metadata.get("run_id")
            or request.metadata.get("invocation_id")
            or f"mle_{uuid.uuid4().hex[:16]}"
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
        checkpoint_session_id = str(
            request.metadata.get("checkpoint_session_id") or request.session_id
        )
        thread_id = encode_thread_id(
            tenant_id=self._tenant_id,
            user_id=request.user_id,
            agent_id=state.agent_id,
            session_id=checkpoint_session_id,
            run_id=run_id,
        )
        handle = RunHandle(
            run_id=run_id,
            session_id=request.session_id,
            runtime_type="managed-langgraph",
            native_ref={
                "thread_id": thread_id,
                "user_id": request.user_id,
                "agent_id": state.agent_id,
                "parent_run_id": request.metadata.get("parent_run_id"),
            },
        )
        from ksadk.harness.subagent import SubAgentSpec

        revision_sub_agents = {
            binding.name: SubAgentSpec.from_binding(binding) for binding in compiled.spec.sub_agents
        }
        # Revision 是事实源；宿主同名配置只作为未编译旧路径的兼容兜底。
        effective_sub_agents = {**self._sub_agents, **revision_sub_agents}
        self._runs[run_id] = _EngineRun(
            handle=handle,
            request=request,
            compiled=compiled,
            state=state,
            thread_id=thread_id,
            skill_catalog=self._skill_disclosure.catalog(
                compiled.spec, query=str(request.input or "")
            ),
            mcp_catalog=self._mcp_disclosure.catalog(compiled.spec),
            sub_agents=effective_sub_agents,
            tools=dict(self._tools if tools is None else tools),
            approval_required=set(self._approval_required)
            | set(getattr(execution_policy, "approval_required", ())),
            execution_policy=execution_policy,
            execution_policy_resolver=execution_policy_resolver,
            execution_policy_request=execution_policy_request or request,
            controller=self._new_run_controller(compiled),
        )
        return handle

    # -------------------------------------------------------------- attach

    def is_handle_attached(self, handle: RunHandle) -> bool:
        """Return whether this engine process already owns the live Run state."""
        return handle.run_id in self._runs

    async def attach(
        self,
        handle: RunHandle,
        compiled: CompiledHarness,
        *,
        allow_completed: bool = False,
        tools: dict[str, Any] | None = None,
        execution_policy: Any = None,
        execution_policy_resolver: Any = None,
        execution_policy_request: StartRequest | None = None,
    ) -> RunHandle:
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
            skill_catalog=self._skill_disclosure.catalog(compiled.spec),
            mcp_catalog=self._mcp_disclosure.catalog(compiled.spec),
            tools=dict(self._tools if tools is None else tools),
            approval_required=set(self._approval_required)
            | set(getattr(execution_policy, "approval_required", ())),
            execution_policy=execution_policy,
            execution_policy_resolver=execution_policy_resolver,
            execution_policy_request=execution_policy_request,
            controller=self._new_run_controller(compiled),
        )
        from ksadk.harness.subagent import SubAgentSpec

        run.sub_agents = {
            **self._sub_agents,
            **{
                binding.name: SubAgentSpec.from_binding(binding)
                for binding in compiled.spec.sub_agents
            },
        }
        run.started_emitted = True  # run.started 已在首个进程发出
        run.done = False
        # 从 Checkpoint snapshot 推断挂起状态。
        graph = self._build_graph(run)
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        values = getattr(snapshot, "values", None) or {}
        if snapshot is None or not values or (not snapshot.next and not allow_completed):
            raise ExecutionEngineError(f"attach 失败：thread {thread_id!r} 无未决 Checkpoint")
        tasks = getattr(snapshot, "tasks", None) or ()
        if isinstance(tasks, dict):
            tasks = tuple(tasks.values())
        has_interrupt = any(getattr(task, "interrupts", None) for task in tasks)
        for task in tasks:
            for pending in getattr(task, "interrupts", ()):
                value = getattr(pending, "value", {})
                if isinstance(value, dict) and value.get("call_id"):
                    run.pending_approval_call_id = str(value["call_id"])
                    run.pending_approval = dict(value)
                    break
        if run.controller is not None:
            values = getattr(snapshot, "values", None) or {}
            control_snapshot = values.get("run_control") if isinstance(values, dict) else None
            if control_snapshot:
                run.controller.restore(control_snapshot)
        from ksadk.harness.engine.budgets import restore

        restore(run, values.get("budget") or {})
        run.state.messages = [
            Message(role=MessageRole(m.get("role", "user")), content=str(m.get("content") or ""))
            for m in values.get("messages", [])
            if isinstance(m, dict)
        ]
        if values.get("execution_terminal"):
            run.state.status, run.done = RunStatus(values["execution_terminal"]), True
        elif not snapshot.next:
            run.state.status, run.done = RunStatus.COMPLETED, True
        else:
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
            await self.cancel(handle)
            if run.task is not None and not run.task.done():
                await asyncio.gather(run.task, return_exceptions=True)
            raise

    def _emit_insight(self, run: _EngineRun, event: RuntimeEvent) -> None:
        """事件出口统一回调（洞察登记处 / 审计侧消费；异常不阻断主流程）。"""
        if self._event_sink is None:
            return
        try:
            self._event_sink(run.state.session_id, run.handle.run_id, event)
        except Exception:  # noqa: BLE001 - 登记失败不阻断对话主流程
            pass

    def harness_capabilities(self):
        """返回运行时真实治理声明；该声明也随每个 Run 的首批事件保存。"""
        from ksadk.harness.runtime_capabilities import managed_langgraph_capabilities

        return managed_langgraph_capabilities()

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
            from ksadk.harness.engine.capability_declarations import (
                capability_declarations,
            )

            run.events.extend(capability_declarations(self, run))
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
                    skill_catalog_message = self._skill_disclosure.catalog_message(
                        run.skill_catalog
                    )
                    if skill_catalog_message:
                        conversation.append(skill_catalog_message)
                    mcp_catalog_message = self._mcp_disclosure.catalog_message(run.mcp_catalog)
                    if mcp_catalog_message:
                        conversation.append(mcp_catalog_message)
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
                    "mcp_listed": [],
                    "mcp_schema_read": [],
                    "finalization_retries": 0,
                    "run_control": (
                        run.controller.snapshot() if run.controller is not None else {}
                    ),
                }
            from ksadk.harness.engine.run_control_bridge import invoke_graph_with_control

            final_state = await invoke_graph_with_control(
                graph, invoke_input, config, run.controller
            )
            interrupts = final_state.get("__interrupt__") if isinstance(final_state, dict) else None
            if interrupts:
                # Approval 挂起：图状态已由 Checkpointer 持久化，等待 resume。
                run.state.status = RunStatus.AWAITING_APPROVAL
                run.done = False
                info = getattr(interrupts[0], "value", {}) or {}
                run.pending_approval_call_id = str(info.get("call_id") or "")
                run.pending_approval = dict(info)
                approval_id = self._approval_id(run)
                run.events.append(
                    self._event(
                        run,
                        EventType.APPROVAL_REQUESTED,
                        {
                            "approval_id": approval_id,
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
                        {
                            "status": "awaiting_approval",
                            "reason": "tool_approval",
                            "approval_id": approval_id,
                        },
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
            # 最终答案事件由 reason 节点统一产生。这里仅投影 Transcript；若再从
            # final_state 补发，stream 已消费 reason 事件时会造成同一答案重复展示。
            if run.cancel_requested:
                raise asyncio.CancelledError
            run.state.status = RunStatus.COMPLETED
            run.events.append(
                self._event(
                    run,
                    EventType.AGENT_COMPLETED,
                    {"agent_id": run.state.agent_id, "status": "completed"},
                )
            )
            from ksadk.harness.engine.run_control_bridge import completion_payload

            payload = completion_payload(run)
            run.events.append(self._event(run, EventType.RUN_COMPLETED, payload))
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
            run.events.append(
                self._event(
                    run,
                    EventType.AGENT_COMPLETED,
                    {"agent_id": run.state.agent_id, "status": "canceled"},
                )
            )
            run.events.append(self._event(run, EventType.RUN_CANCELED, {"status": "cancelled"}))
            await self._persist_terminal(run)
            return []
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
        except RunControlStop as exc:
            from ksadk.harness.engine.run_control_bridge import append_controlled_stop

            append_controlled_stop(self, run, exc.reason)
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
                self._event(
                    run,
                    EventType.RUN_FAILED,
                    {
                        "status": "failed",
                        "error": str(exc),
                        **(
                            {"error_category": str(exc.category)}
                            if getattr(exc, "category", None)
                            else {}
                        ),
                    },
                )
            )
            await self._persist_terminal(run)
            return []

    # ---------------------------------------------------------------- graph

    def _build_graph(self, run: _EngineRun):
        # 图组装（节点实现/边/条件路由）拆至 graph_builder 模块，保持本模块
        # <1000 行的仓库合同；节点闭包仍经 engine 访问运行时状态。
        from ksadk.harness.engine.graph_builder import build_graph

        return build_graph(self, run)

    async def _invoke_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        run: Any = None,
        mcp_cursors: McpDisclosureCursors | None = None,
        call_id: str = "",
    ) -> Any:
        from ksadk.harness.engine.tool_dispatch import invoke_tool
        from ksadk.harness.execution_policy import execution_policy_scope

        if run is not None:
            await self._check_execution_policy(run)
        with execution_policy_scope(run.execution_policy if run is not None else None):
            return await invoke_tool(
                self, name, arguments, run=run, mcp_cursors=mcp_cursors, call_id=call_id
            )

    async def _check_execution_policy(self, run: Any) -> None:
        if run.execution_policy_resolver is None:
            return
        from dataclasses import replace

        from ksadk.harness.execution_policy import ExecutionPolicy

        request = run.execution_policy_request
        policy = await run.execution_policy_resolver.resolve(
            request.metadata["execution_policy_ref"], request=request
        )
        if not isinstance(policy, ExecutionPolicy):
            raise ExecutionEngineError("execution policy resolver returned an invalid policy")
        previous = run.execution_policy
        if (
            set(policy.tools) != set(previous.tools)
            or policy.workspace_root != previous.workspace_root
            or policy.approval_required != previous.approval_required
        ):
            raise ExecutionEngineError(
                "execution policy changed; a new execution reference is required"
            )
        # Refresh authorized callables after authority is checked again.
        for name, tool in policy.tools.items():
            if name in run.tools:
                run.tools[name] = tool
        # Context may reflect fresh host facts. Keep the original prompt snapshot
        # for this invocation while accepting monotonically tighter budgets.
        limits = dict(previous.limits)
        for key, value in policy.limits.items():
            limits[key] = min(limits[key], value) if key in limits else value
        policy = replace(
            policy,
            limits=limits,
            system_context=previous.system_context,
            child_system_context=previous.child_system_context,
        )
        config = dict(run.compiled.spec.execution_strategy.config)
        for key, value in limits.items():
            prior = config.get(key)
            config[key] = min(int(prior), value) if prior is not None else value
        run.compiled = replace(
            run.compiled,
            spec=run.compiled.spec.model_copy(
                update={
                    "execution_strategy": run.compiled.spec.execution_strategy.model_copy(
                        update={"config": config}
                    )
                }
            ),
        )
        run.execution_policy = policy

    # --------------------------------------------------------------- cancel

    async def cancel(self, handle: RunHandle) -> CancelResult:
        run = self._runs.get(handle.run_id)
        if run is None or run.done:
            return CancelResult.NOT_RUNNING
        if run.cancel_requested:
            return CancelResult.PENDING_CANCEL_RECORDED
        from ksadk.harness.engine.subagents import restore_pending_child

        await restore_pending_child(self, run)
        run.cancel_requested = True
        active_children = list(self._active_subagent_runs.get(handle.run_id, {}).values())
        active_task = run.task if run.task is not None and not run.task.done() else None
        if active_task is not None:
            active_task.cancel()
        if active_children:
            await asyncio.gather(
                *(
                    child_engine.cancel(child_handle)
                    for child_engine, child_handle in active_children
                ),
                return_exceptions=True,
            )
        if active_task is None:
            run.state.status, run.done = RunStatus.CANCELED, True
            from ksadk.harness.engine.subagents import cancel_and_drain
            from ksadk.harness.subagent import resequence_child_events

            for child_engine, child_handle in active_children:
                await cancel_and_drain(
                    child_engine,
                    child_handle,
                    lambda event: resequence_child_events(run, [event]),
                )
            self._active_subagent_runs.pop(handle.run_id, None)
            run.events.append(
                self._event(
                    run,
                    EventType.AGENT_COMPLETED,
                    {"agent_id": run.state.agent_id, "status": "canceled"},
                )
            )
            run.events.append(self._event(run, EventType.RUN_CANCELED, {"status": "cancelled"}))
            await self._persist_terminal(run)
            return CancelResult.PENDING_CANCEL_RECORDED
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    async def _persist_terminal(self, run: _EngineRun) -> None:
        if self._checkpointer is None:
            return
        from ksadk.harness.engine.budgets import snapshot

        graph = self._build_graph(run)
        await graph.aupdate_state(
            {"configurable": {"thread_id": run.thread_id}},
            {"execution_terminal": run.state.status.value, "budget": snapshot(run)},
        )

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
        if (
            payload is None
            or payload.kind != "approval_decision"
            or not run.pending_approval_call_id
            or payload.call_id != run.pending_approval_call_id
            or payload.data not in ("approved", "denied", "rejected")
        ):
            raise ExecutionEngineError(
                "approval resume requires an explicit decision for the pending call_id"
            )
        decision = "denied"
        if payload is not None:
            decision = str(payload.data) if payload.data is not None else "approved"
            run.events.append(
                self._event(
                    run,
                    EventType.APPROVAL_RESOLVED,
                    {
                        "approval_id": self._approval_id(run),
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
        if run is not None:
            self._skill_disclosure.clear_run(run.handle.run_id)
            self._mcp_disclosure.clear_run(run.handle.run_id)
            self._pending_child_events.pop(run.handle.run_id, None)
            self._pending_subagent_events.pop(run.handle.run_id, None)
            self._active_subagent_runs.pop(run.handle.run_id, None)

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

    async def _invoke_mcp_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        run: _EngineRun | None,
        cursors: McpDisclosureCursors,
        call_id: str = "",
    ) -> dict[str, Any]:
        return await self._mcp_disclosure.invoke(
            name,
            arguments,
            run=run,
            cursors=cursors,
            pending_events=self._pending_child_events,
            call_id=call_id,
        )

    def _invoke_skill_tool(
        self, name: str, arguments: dict[str, Any], *, run: _EngineRun | None
    ) -> dict[str, Any]:
        return self._skill_disclosure.invoke(
            name,
            arguments,
            run=run,
            pending_events=self._pending_child_events,
        )

    def _event(
        self, run: _EngineRun, event_type: str, payload: dict[str, Any], *, phase: str | None = None
    ) -> RuntimeEvent:
        run.seq += 1
        event = RuntimeEvent.create(
            event_type,
            agent_id=run.state.agent_id,
            user_id=run.state.user_id,
            session_id=run.state.session_id,
            invocation_id=run.handle.run_id,
            seq_id=run.seq,
            payload=payload,
            phase=phase,
        )
        if run.controller is not None:
            run.control_events.append(event)
            run.controller.observe(event)
        return event

    @staticmethod
    def _approval_id(run: _EngineRun) -> str:
        base = f"ap-{run.handle.run_id}-{run.pending_approval_call_id or ''}"
        if run.pending_approval.get("kind") == "child_tool":
            identity = (
                str(run.pending_approval.get("child_run_id"))
                + ":"
                + str(run.pending_approval.get("child_call_id"))
            )
            return base + "-" + uuid.uuid5(uuid.NAMESPACE_URL, identity).hex[:16]
        return base

    @staticmethod
    def _new_run_controller(compiled: CompiledHarness) -> RunController | None:
        from ksadk.harness.engine.run_control_bridge import new_run_controller

        return new_run_controller(compiled)


def _is_durable(checkpointer: BaseCheckpointSaver) -> bool:
    """SQLite/Postgres Checkpointer 视为跨进程持久；MemorySaver 不是。"""
    from langgraph.checkpoint.memory import InMemorySaver, MemorySaver

    return not isinstance(checkpointer, (MemorySaver, InMemorySaver))


def memory_checkpointer() -> BaseCheckpointSaver:
    """进程内 Checkpointer（langgraph 类型不越出本模块的架构合同）。"""
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


def sqlite_checkpointer(db_path: str) -> Any:
    """SQLite 异步 Checkpointer 的上下文管理器（调用方负责 __aenter__）。

    经由本工厂获取，避免 langgraph import 越出引擎模块
    （tests/architecture/test_harness_contract.py 守卫）。
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    return AsyncSqliteSaver.from_conn_string(db_path)


__all__ = ["ManagedLangGraphEngine", "memory_checkpointer", "sqlite_checkpointer"]
