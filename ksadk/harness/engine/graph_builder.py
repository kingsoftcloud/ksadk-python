"""ManagedLangGraphEngine 的图构建模块（从 langgraph.py 拆出）。

按 ExecutionPlan 组装 LangGraph 图（节点实现 + 边 + 条件路由），
并随引擎注入 Checkpointer。langgraph 类型不越出 engine 包
（tests/architecture/test_harness_contract.py 守卫）。
"""

from __future__ import annotations

import os
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ksadk.harness.engine.base import ExecutionEngineError
from ksadk.harness.engine.langgraph import _GraphState
from ksadk.harness.engine.mcp_disclosure import MCP_CALL_TOOL_TOOL, McpDisclosureCursors
from ksadk.harness.engine.spans import wrap_node_span
from ksadk.harness.events import EventType
from ksadk.harness.loop import (
    ModelFailoverExhausted,
    ReasonInput,
    ToolCallInput,
    execute_tool_calls,
    reason_turn_async,
)
from ksadk.harness.loop.reason import ReasoningLimitError
from ksadk.harness.reasoner import HarnessReasoningTurn


def build_graph(engine, run):
    spec = run.compiled.spec

    async def reason(state: _GraphState) -> _GraphState:
        state["turn_count"] += 1
        # 收口 5：Turn 区间事件（模型/工具/usage 事件按 seq 落在区间内）。
        turn_id = f"{run.handle.run_id}:t{state['turn_count']}"
        run.events.append(
            engine._event(
                run,
                EventType.TURN_STARTED,
                {"turn_id": turn_id, "turn_number": state["turn_count"]},
            )
        )

        def _reason_input() -> ReasonInput:
            max_total_tokens = spec.execution_strategy.config.get("max_total_tokens")
            max_output_tokens = None
            if max_total_tokens is not None:
                from ksadk.harness.engine.context_pipeline import count_tokens
                from ksadk.harness.subagent import SubAgentExecutionError

                used = int(state.get("usage_tokens") or 0)
                estimated_input = sum(
                    count_tokens(str(message.get("content") or ""))
                    for message in state["messages"]
                ) + count_tokens(spec.prompt.instructions or "")
                remaining = int(max_total_tokens) - used - estimated_input
                if remaining < 1:
                    raise SubAgentExecutionError(
                        "budget_exhausted",
                        "sub-agent token budget exhausted before model invocation "
                        f"(limit={max_total_tokens}, used={used}, "
                        f"estimated_input={estimated_input})",
                    )
                max_output_tokens = remaining
            return ReasonInput(
                model_ref=spec.model.profile_ref,
                fallback_model_refs=spec.model.fallback_profile_refs,
                provider_policy=spec.model.provider_policy,
                instructions=spec.prompt.instructions or "",
                messages=state["messages"],
                tools=(
                    list(engine._tools.values())
                    + engine._skill_disclosure.tools(run.skill_catalog)
                    + engine._mcp_disclosure.tools(run.mcp_catalog)
                    + list(run.sub_agents.values())
                ),
                reasoner=engine._reasoner,
                agent_id=run.state.agent_id,
                user_id=run.state.user_id,
                session_id=run.state.session_id,
                run_id=run.handle.run_id,
                seq_start=run.seq,
                max_turns=engine._max_reasoning_turns,
                max_output_tokens=max_output_tokens,
                streaming=(
                    getattr(engine._reasoner, "_streaming", None) is True
                    or os.getenv("KSADK_MODEL_STREAMING", "").strip().lower()
                    in {"1", "true", "yes", "on"}
                ),
            )

        try:
            out = await reason_turn_async(state["turn_count"], _reason_input())
        except ModelFailoverExhausted as exc:
            # reason_turn 在最后一次失败时仍必须把每次 started/failed
            # 审计事件交还引擎，不能因异常路径丢失配对事实。
            for event in exc.events:
                run.events.append(event)
                run.seq = max(run.seq, event.seq_id)
            if (
                exc.stop_reason.value == "recover_context"
                and engine._context_engine is not None
            ):
                state["messages"] = await engine._context_pipeline.recover_model_overflow(
                    run,
                    state["messages"],
                    spec.prompt.instructions or "",
                )
                # 恢复调用仍属于同一个 Turn，但使用压缩后输入及新的 seq 起点。
                try:
                    out = await reason_turn_async(state["turn_count"], _reason_input())
                except ModelFailoverExhausted as retry_exc:
                    for event in retry_exc.events:
                        run.events.append(event)
                        run.seq = max(run.seq, event.seq_id)
                    raise RuntimeError(str(retry_exc)) from retry_exc
            else:
                raise RuntimeError(str(exc)) from exc
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
            if ev.event_type == EventType.USAGE_REPORTED:
                state["usage_tokens"] = int(state.get("usage_tokens") or 0) + int(
                    ev.payload.get("input_tokens") or 0
                ) + int(ev.payload.get("output_tokens") or 0)
        state["messages"].extend(out.new_messages)
        state["pending_tool_calls"] = out.pending_tool_calls
        state["route"] = out.route
        run.events.append(
            engine._event(
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

        # 披露游标活在图状态（随 Checkpoint 持久化）；节点结束写回。
        cursors = McpDisclosureCursors(
            # Checkpoint 反序列化会把 tuple 变 list（不可哈希），统一还原。
            listed={tuple(item) for item in (state.get("mcp_listed") or ())},
            schema_read={tuple(item) for item in (state.get("mcp_schema_read") or ())},
        )

        def _mcp_approval_decider(name: str, arguments: dict[str, Any]) -> bool:
            # P0.1：按实际目标 Server 动态决策——只有 mcp_call_tool 指向
            # high/critical 风险 Server 时才要求审批（高低风险混用不误伤）。
            return name == MCP_CALL_TOOL_TOOL and engine._mcp_disclosure.approval_decider(
                arguments
            )

        class _EngineToolExecutor:
            async def execute(self, name, arguments):  # type: ignore[no-untyped-def]
                return await engine._invoke_tool(
                    name, arguments, run=run, mcp_cursors=cursors
                )

            async def execute_with_context(  # type: ignore[no-untyped-def]
                self, name, arguments, context
            ):
                return await engine._invoke_tool(
                    name,
                    arguments,
                    run=run,
                    mcp_cursors=cursors,
                    call_id=context.call_id,
                )

            def reliability(self, name, arguments, *, receipt_enabled):  # type: ignore[no-untyped-def]
                if engine._mcp_disclosure.is_tool(name):
                    return engine._mcp_disclosure.reliability(
                        name,
                        arguments,
                        receipt_enabled=receipt_enabled,
                    )
                if engine._capability_runtime is not None:
                    return engine._capability_runtime.reliability(name)
                from ksadk.harness.tool_reliability import classify_tool_reliability

                return classify_tool_reliability(side_effect="unknown")

        from ksadk.harness.subagent import order_subagent_tool_calls

        pending_tool_calls = order_subagent_tool_calls(
            list(state["pending_tool_calls"]), run.sub_agents
        )
        has_subagent_dependencies = any(
            run.sub_agents[name].depends_on
            for name in (str(call.get("name") or "") for call in pending_tool_calls)
            if name in run.sub_agents
        )
        subagent_failure_mode = str(
            run.compiled.spec.execution_strategy.config.get(
                "subagent_failure_mode", "partial"
            )
        )
        out = await execute_tool_calls(
            ToolCallInput(
                pending_tool_calls=pending_tool_calls,
                approval_required=frozenset(engine._approval_required),
                approval_decider=_mcp_approval_decider,
                parallel_safe_decider=(
                    lambda name, _arguments: (
                        name in run.sub_agents
                        and not has_subagent_dependencies
                        and subagent_failure_mode != "fail_fast"
                    )
                ),
                stop_on_error_decider=(
                    lambda name, _arguments: (
                        subagent_failure_mode == "fail_fast" and name in run.sub_agents
                    )
                ),
                cancel_pending_decider=lambda name, _arguments: name in run.sub_agents,
                dependencies={
                    name: tuple(spec.depends_on)
                    for name, spec in run.sub_agents.items()
                    if spec.depends_on
                },
                max_parallelism=int(
                    run.compiled.spec.execution_strategy.config.get(
                        "max_parallel_subagents", 4
                    )
                ),
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

        resequence_child_events(run, engine._pending_child_events.pop(run.handle.run_id, []))
        subagent_events = engine._pending_subagent_events.pop(run.handle.run_id, {})
        for pending in pending_tool_calls:
            resequence_child_events(
                run,
                subagent_events.get(str(pending.get("call_id") or ""), []),
            )
        state["messages"].extend(out.new_messages)
        if out.working_context is not None:
            run.state.working_context = out.working_context
        state["pending_tool_calls"] = out.pending_tool_calls
        state["mcp_listed"] = sorted(cursors.listed)
        state["mcp_schema_read"] = sorted(cursors.schema_read)
        state["route"] = out.route
        return state

    def route(state: _GraphState) -> str:
        return state["route"]

    async def plan_node(state: _GraphState) -> _GraphState:
        """plan-execute 拓扑的规划节点：一次无工具模型调用产出执行计划。"""
        turn: HarnessReasoningTurn = await engine._reasoner.complete(
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
        turn: HarnessReasoningTurn = await engine._reasoner.complete(
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
        name: wrap_node_span(engine, run, name, impl) for name, impl in node_impls.items()
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
    graph = builder.compile(checkpointer=engine._checkpointer)
    return graph
