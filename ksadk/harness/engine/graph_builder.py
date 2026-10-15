"""ManagedLangGraphEngine 的图构建模块（从 langgraph.py 拆出）。

按 ExecutionPlan 组装 LangGraph 图（节点实现 + 边 + 条件路由），
并随引擎注入 Checkpointer。langgraph 类型不越出 engine 包
（tests/architecture/test_harness_contract.py 守卫）。
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ksadk.harness.engine import budgets
from ksadk.harness.engine.base import ExecutionEngineError
from ksadk.harness.engine.hooked_reasoner import (
    _HookedReasoner,
    flush_hook_degradations,
)
from ksadk.harness.engine.langgraph import _GraphState
from ksadk.harness.engine.policy_runtime import revalidate_policy
from ksadk.harness.engine.spans import wrap_node_span
from ksadk.harness.events import EventType
from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.harness.run_control import ControlAction, RunControlStop

logger = logging.getLogger(__name__)


def _model_failure_error(error: ModelFailoverExhausted) -> RuntimeError:
    """Preserve provider failure facts when the graph boundary wraps errors.

    The graph engine exposes a plain exception to its outer runner for
    compatibility, but Studio still needs the stable category and HTTP status
    to render an actionable terminal card (especially rate limit 429).
    """

    wrapped = RuntimeError(str(error))
    failure = error.failure
    # Unknown-kind failures keep the legacy payload shape (no error_category)
    # so sub-agent dispatch still falls back to "child_failed" and applies its
    # retry policy; only classified failures carry their category upstream.
    if failure.kind.value != "unknown":
        setattr(wrapped, "error_category", failure.kind.value)
    if failure.status_code is not None:
        setattr(wrapped, "status_code", failure.status_code)
    if failure.kind.value == "rate_limit":
        setattr(wrapped, "error_code", "MODEL_RATE_LIMITED")
    return wrapped


def build_graph(engine, run):
    spec = run.compiled.spec

    def _restore_controller(state: _GraphState) -> None:
        if run.controller is not None and state.get("run_control"):
            run.controller.restore(state["run_control"])
        budgets.restore(run, state.get("budget") or {})

    def _capture_control_event(event) -> ControlAction:
        if event.event_id in run.observed_event_ids:
            return ControlAction.CONTINUE
        run.observed_event_ids.add(event.event_id)
        if event.event_type == EventType.USAGE_REPORTED:
            input_tokens = int(event.payload.get("input_tokens") or 0)
            output_tokens = int(event.payload.get("output_tokens") or 0)
            budgets.spend(
                run,
                "tokens",
                int(event.payload.get("total_tokens") or input_tokens + output_tokens),
            )
            budgets.spend(run, "input_tokens", input_tokens)
            budgets.spend(run, "output_tokens", output_tokens)
        if event.event_type == EventType.ARTIFACT_CREATED:
            uri = str(event.payload.get("uri") or "")
            if uri and uri not in run.artifact_refs:
                run.artifact_refs.append(uri)
        if run.controller is None:
            return ControlAction.CONTINUE
        if not any(item.event_id == event.event_id for item in run.control_events):
            run.control_events.append(event)
        decision = run.controller.observe(event)
        return decision.action

    def _before_model(state: _GraphState) -> None:
        _restore_controller(state)
        if isinstance(engine._reasoner, _HookedReasoner):
            # G5 §5.2：包装后的 Reasoner 从活跃 Run 取钩子上下文（模型调用节点统一设置）。
            engine._active_hook_run = run
        if run.controller is None:
            budgets.check(run, "tokens", 0)
            budgets.check(run, "models")
            budgets.spend(run, "models")
            return
        decision = run.controller.before_model()
        if decision.action == ControlAction.STOP:
            state["run_control"] = run.controller.snapshot()
            raise RunControlStop(decision.reason)
        budgets.check(run, "tokens", 0)
        budgets.check(run, "models")
        budgets.spend(run, "models")
        guidance = run.controller.take_execution_instruction()
        if guidance:
            state["messages"].append({"role": "system", "content": guidance})
            run.events.append(
                engine._event(
                    run,
                    EventType.RUN_PROGRESS,
                    {
                        "kind": "run_control",
                        "status": "in_progress",
                        "action": "guidance",
                        "plan_revision": run.controller.guidance_revision,
                    },
                )
            )
        if decision.action == ControlAction.CLOSE:
            instruction = run.controller.take_closing_instruction(decision.reason)
            if instruction:
                state["messages"].append({"role": "system", "content": instruction})
                run.events.append(
                    engine._event(
                        run,
                        EventType.RUN_PROGRESS,
                        {
                            "kind": "run_control",
                            "status": "in_progress",
                            "action": "close",
                            "reason": decision.reason,
                        },
                    )
                )
        state["run_control"] = run.controller.snapshot()

    async def reason(state: _GraphState) -> _GraphState:
        await revalidate_policy(engine, run)
        _before_model(state)
        state["turn_count"] += 1
        # 收口 5：Turn 区间事件（模型/工具/usage 事件按 seq 落在区间内）。
        turn_id = f"{run.handle.run_id}:t{state['turn_count']}"
        # Native Harness Phase 1：reason 主体已抽出为引擎无关的纯函数 handler
        # （engine/phases/reason.py）。本包装负责图内前置（策略重校验 / _before_model
        # / turn_count / turn_id 构造），handler 负责 turn 区间内全部事件与状态推进。
        from ksadk.harness.engine.phases.context import PhaseContext
        from ksadk.harness.engine.phases.reason import reason_phase

        ctx = PhaseContext(spec=spec, run=run, engine=engine, turn_id=turn_id, state=state)
        result = await reason_phase(ctx)
        state.update(result.state)
        # Phase 1 过渡期：handler 仍直接写 run.events/run.seq，故此处仅作纵深防御
        # （seq_id 已推进时不再重复 append；Phase 2 切断直写后此循环成为唯一出口）。
        for ev in result.events:
            if ev.seq_id > run.seq:
                run.events.append(ev)
                run.seq = max(run.seq, ev.seq_id)
                _capture_control_event(ev)
        return state

    async def tool_calls(state: _GraphState) -> _GraphState:
        await revalidate_policy(engine, run)
        _restore_controller(state)
        run.child_approval_decision = state.get("child_approval_decision")
        state["child_approval_decision"] = {}
        # Native Harness Phase 1：tool_calls 主体已抽出为引擎无关的纯函数 handler
        # （engine/phases/tools.py）。本包装负责图内前置（策略重校验 / 控制器恢复 /
        # child-approval 决定接收）与**挂起原语翻译**：handler 返回 NeedInteraction
        # 表示需要外部交互，LangGraph 侧在此调 interrupt() 挂起，resume 后把决定写回
        # 状态并重新进入 handler（同一节点调用内循环，故前置只执行一次——与旧
        # interrupt() 语义下"节点整体重放"的次数一致）。
        from ksadk.harness.engine.phases.context import PhaseContext
        from ksadk.harness.engine.phases.tools import (
            TOOL_APPROVAL_DECISION_KEY,
            TOOL_APPROVAL_REPLAY_ATTR,
            tools_phase,
        )

        # 重放检测：引擎在挂起时把审批 call_id 记到 run.pending_approval_call_id，
        # attach 亦会从 Checkpoint 重建它（跨进程恢复同样成立）。resume 后 LangGraph
        # 整体重放本节点，而重放期间 handler 仍拿不到决定（决定要等下面 interrupt()
        # 返回）会再走一次挂起分支；旧实现里那一次是 interrupt() 内联返回决定、不走
        # 异常分支，故 handler 据此跳过重复补发的失败态进度。Phase 2 Native 引擎以
        # NeedInteraction 表达挂起，不需该标记。
        setattr(
            run,
            TOOL_APPROVAL_REPLAY_ATTR,
            str(getattr(run, "pending_approval_call_id", "") or ""),
        )

        ctx = PhaseContext(spec=spec, run=run, engine=engine, turn_id="", state=state)
        while True:
            result = await tools_phase(ctx)
            state.update(result.state)
            # Phase 1 过渡期：handler 仍直接写 run.events/run.seq，故此处仅作纵深防御
            # （seq_id 已推进时不再重复 append；Phase 2 切断直写后此循环成为唯一出口）。
            for ev in result.events:
                if ev.seq_id > run.seq:
                    run.events.append(ev)
                    run.seq = max(run.seq, ev.seq_id)
                    _capture_control_event(ev)
            if result.interaction is None:
                return state
            if result.interaction.kind == "child_approval":
                # Phase 1：子 Agent 审批仍由专用 ``child_approval`` 节点挂起
                # （route="child_approval" 由 handler 写入）。不在本节点内 interrupt 的
                # 实测原因：在**已 resume 过**的 tool_calls 任务里再次 interrupt，
                # LangGraph 会把 Checkpoint 的 ``next`` 清空，attach 无法从 snapshot
                # 推断挂起（test_second_child_approval_never_reuses_first_decision）。
                # handler 仍把 child_approval NeedInteraction 显式交还，供 Phase 2
                # Native 引擎直接挂起（届时不再需要图节点）。
                return state
            payload = result.interaction.request_payload
            # LangGraph suspension: interrupt() resumes with the approval decision.
            decision = interrupt(payload)
            state[TOOL_APPROVAL_DECISION_KEY] = {
                "call_id": str(payload.get("call_id") or ""),
                "decision": decision,
            }
            # Re-enter tools to execute the now-decided call.


    def route(state: _GraphState) -> str:
        return state["route"]

    def child_approval(state: _GraphState) -> _GraphState:
        info = state["child_approval"]
        decision = interrupt(info)
        state["child_approval_decision"] = {**info, "decision": decision}
        state["child_approval"] = {}
        state["route"] = "tool_calls"
        return state

    async def plan_node(state: _GraphState) -> _GraphState:
        """plan-execute 拓扑的规划节点：一次无工具模型调用产出执行计划。"""
        _before_model(state)
        reasoning_effort = str(run.request.config.get("effort") or "").strip().lower() or None
        turn: HarnessReasoningTurn = await engine._reasoner.complete(
            model=spec.model.profile_ref,
            prompt=(spec.prompt.instructions or "") + "\n请先给出分步执行计划，再开始执行。",
            messages=tuple(state["messages"]),
            tools=[],
            **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
        )
        # 直接调 Reasoner 的节点也要先 flush 降级再发自己的后续事件，保持 seq 单调。
        flush_hook_degradations(engine, run)
        state["messages"].append(
            {"role": "assistant", "content": "【执行计划】\n" + (turn.final_text or "")}
        )
        run.events.append(
            engine._event(
                run,
                EventType.RUN_PROGRESS,
                {
                    "kind": "plan",
                    "status": "in_progress",
                    "message": turn.final_text or "计划已生成",
                },
            )
        )
        return state

    async def review_node(state: _GraphState) -> _GraphState:
        """plan-execute-review 拓扑的独立审查节点：对最终回答做一次批判。"""
        _before_model(state)
        reasoning_effort = str(run.request.config.get("effort") or "").strip().lower() or None
        messages = list(state["messages"]) + [
            {"role": "user", "content": "请审查以上回答的准确性与遗漏，指出问题（如有）。"}
        ]
        turn: HarnessReasoningTurn = await engine._reasoner.complete(
            model=spec.model.profile_ref,
            prompt=spec.prompt.instructions or "",
            messages=tuple(messages),
            tools=[],
            **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
        )
        flush_hook_degradations(engine, run)
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
    builder.add_node("child_approval", child_approval)
    builder.add_edge("child_approval", "tool_calls")
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
    # A live user correction may arrive while the final model response is in
    # flight. The reason handler rejects that stale candidate and re-enters
    # itself with the accepted correction, even for text-only plans.
    reason_mapping["reason"] = "reason"
    reason_mapping["final"] = END if exit_target == "final" else exit_target
    if "reason" in node_impls and "reason" in plan.nodes and "reason" != exit_target:
        builder.add_conditional_edges("reason", route, reason_mapping)
    for src, dst in edges:
        if src == "reason":
            continue  # 已由条件边覆盖。
        if src == "tool_calls":
            builder.add_conditional_edges(
                "tool_calls",
                route,
                {
                    "tool_calls": "tool_calls",
                    "child_approval": "child_approval",
                    "reason": dst,
                },
            )
            continue
        if dst == "final":
            if src in node_impls and src != "reason":
                builder.add_edge(src, END)
            continue
        builder.add_edge(src, dst)
    graph = builder.compile(checkpointer=engine._checkpointer)
    return graph
