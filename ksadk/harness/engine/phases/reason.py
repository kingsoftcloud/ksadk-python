"""reason phase handler (Native Harness Phase 1, Task 4).

Extracted from the ``reason(state)`` closure in
``ksadk/harness/engine/graph_builder.py``. The body is a mechanical move:
dependencies that were closure-captured in the graph builder
(``spec``/``run``/``engine``/``state``/``turn_id``) now arrive on the
:class:`PhaseContext`. The reason node has no ``interrupt()`` call, so this
handler never suspends — it returns a :class:`PhaseResult`.

This module must NOT import LangGraph; the architecture guard
(``tests/architecture/test_harness_contract.py``) enforces this from Task 6.

Behaviour preservation is the contract: event ordering, payloads and the
Phase 0 ``turn_id`` injection into final-answer ``TEXT_COMPLETED`` are
unchanged. The direct ``run.events``/``run.seq`` writes are kept for now
(Phase 2 severs them); every event appended during the handler is also
returned on :class:`PhaseResult` so a future caller can drain without touching
``run.events``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import replace

from ksadk.harness.engine import budgets
from ksadk.harness.engine.completion_guard import looks_like_unfinished_answer
from ksadk.harness.engine.hooked_reasoner import flush_hook_degradations
from ksadk.harness.engine.phases.context import PhaseContext, PhaseResult
from ksadk.harness.events import EventType
from ksadk.harness.loop import (
    ModelFailoverExhausted,
    ReasonInput,
    reason_turn_async,
)
from ksadk.harness.loop.reason import ReasoningLimitError
from ksadk.harness.run_control import ControlAction, RunControlStop

logger = logging.getLogger(__name__)

_DELEGATION_DECISION_MAX_OUTPUT_TOKENS = 2048


def _delegation_decision_budget() -> int:
    """委派决策回合的输出（内容）预算；可用环境变量覆盖。

    见调用点的说明：该预算必须与"关思考"配对，paired 逻辑由
    ``ksadk/model_proxy/compat.should_disable_thinking`` 统一裁决。
    """

    raw = os.getenv("KSADK_DELEGATION_DECISION_MAX_OUTPUT_TOKENS", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return _DELEGATION_DECISION_MAX_OUTPUT_TOKENS
        if value > 0:
            return value
    return _DELEGATION_DECISION_MAX_OUTPUT_TOKENS


_DELEGATION_PREFACE = re.compile(
    r"(?:\blet\s+me\s+(?:delegate|dispatch)|"
    r"\bi\s+(?:will|'ll|am\s+going\s+to)\s+(?:delegate|dispatch)|"
    r"(?:让我|我会|我将|接下来|准备|正在)(?:先)?(?:并行)?(?:派发|分派|调度|处理).{0,80}(?:子任务|子智能体|subtasks?))",
    re.IGNORECASE,
)


async def reason_phase(ctx: PhaseContext) -> PhaseResult:
    """reason 一轮判定 + 本节点事件合并（GraphBuilder ``reason`` 节点主体）。

    调用方（graph_builder 的 ``reason`` 包装）已完成 ``revalidate_policy`` /
    ``_before_model`` / ``turn_count += 1`` / ``turn_id`` 构造——handler 收到
    的是已递增的 ``ctx.state`` 与预构造的 ``ctx.turn_id``。
    """

    # 依赖注入：闭包捕获 → ctx 读取。以下局部绑定即 ctx.* 的取值；嵌套 helper
    # （_reason_input / _live_event_sink / _capture_control_event）闭包引用同一
    # 对象，与 graph_builder 中的捕获语义等价（值来自 ctx，非 build_graph 闭包）。
    spec = ctx.spec
    run = ctx.run
    engine = ctx.engine
    state = ctx.state
    turn_id = ctx.turn_id

    # Phase 1 过渡期：本节点仍直接写 run.events/run.seq（Phase 2 切断）。以节点
    # 入口长度切片收集"本节点新增事件"，随 PhaseResult 一并交还调用方。
    events_start = len(run.events)

    def _capture_control_event(event) -> ControlAction:  # noqa: ANN001
        # 与 graph_builder 节点内同名 helper 逐字等价（Phase 2 收口去重）。以
        # run.observed_event_ids 去重，故与包装层的事件捕获可安全并存。
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

    # G1 steering 消费点：reason 边界取走全部待投递纠偏消息。消息以带前缀的
    # user message 进 state（随 Checkpointer 持久化、进 Transcript），不静默
    # 注入——投递事实由 STEER_DELIVERED 事件留痕。
    steered = engine._steering.drain_steering(run.handle.run_id)
    if steered:
        for steer_text in steered:
            state["messages"].append({"role": "user", "content": f"【运行中指令】{steer_text}"})
        run.events.append(
            engine._event(
                run,
                EventType.STEER_DELIVERED,
                {
                    "message_count": len(steered),
                    "delivered_at_turn": state["turn_count"],
                },
            )
        )
    run.events.append(
        engine._event(
            run,
            EventType.TURN_STARTED,
            {"turn_id": turn_id, "turn_number": state["turn_count"]},
        )
    )

    def _reason_input() -> ReasonInput:
        max_total_tokens = budgets.limit(run, "tokens")
        if run.controller is not None and run.controller.spec.limits.max_total_tokens is not None:
            controlled_limit = run.controller.spec.limits.max_total_tokens
            max_total_tokens = (
                min(int(max_total_tokens), controlled_limit)
                if max_total_tokens is not None
                else controlled_limit
            )
        # Model Profile 的输出上限是每次调用的硬限制；Run 总预算则是随执行消耗
        # 递减的剩余额度。两者同时存在时必须取更小值。
        max_output_tokens = spec.model.max_output_tokens
        if max_total_tokens is not None:
            from ksadk.harness.engine.context_pipeline import count_tokens
            from ksadk.harness.subagent import SubAgentExecutionError

            used = max(
                int(state.get("usage_tokens") or 0),
                run.budget_usage.get("tokens", 0),
                run.controller.total_tokens if run.controller is not None else 0,
            )
            estimated_input = sum(
                count_tokens(str(message.get("content") or "")) for message in state["messages"]
            ) + count_tokens(spec.prompt.instructions or "")
            remaining = int(max_total_tokens) - used - estimated_input
            if remaining < 1:
                if run.controller is not None:
                    run.controller.stop_reason = "total_tokens_hard_limit"
                    run.controller.budget_exhausted = True
                    state["run_control"] = run.controller.snapshot()
                    raise RunControlStop("total_tokens_hard_limit")
                raise SubAgentExecutionError(
                    "budget_exhausted",
                    "sub-agent token budget exhausted before model invocation "
                    f"(limit={max_total_tokens}, used={used}, "
                    f"estimated_input={estimated_input})",
                )
            max_output_tokens = (
                min(max_output_tokens, remaining) if max_output_tokens is not None else remaining
            )
        delegation_synthesis = bool(state.get("delegation_synthesis_pending"))
        delegation_decision = not delegation_synthesis and engine._delegation_runtime is not None
        streaming = getattr(engine._reasoner, "_streaming", None) is True or os.getenv(
            "KSADK_MODEL_STREAMING", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        # 不再按 delegation_decision 关闭增量（2026-09-18 实测判定）。
        #
        # 该护栏（c67b0a79）本来是防"某些 GLM 网关截断/损坏大块原生工具调用
        # JSON"。但对我们**实际使用的** kspmas 网关实测是干净的：流式工具调用
        # 153 个内容块、5.3KB 参数、JSON 完整解析，且内容按 28 字符单元精确
        # 对齐（丢块会出现除不尽的余数）——即该前提在本端点上复现不了。
        # 而它的代价很大：只要 agent 配了委派，`delegation_decision` 每个回合都
        # 为真，流式被整体关掉（连 KSADK_MODEL_STREAMING 也覆盖不了），一次慢
        # 请求期间前端零可显示内容，最长可到整轮预算（实测 689s）。
        #
        # 风险由既有兜底覆盖：流式参数非 JSON → 修复一次；修复仍坏 →
        # invalid_arguments 回灌（不致命）；有工具调用的回合，其 TEXT_DELTA
        # 会被循环层整段丢弃，不会污染答案。
        logger.info(
            "harness 流式决策：streaming=%s reasoner_streaming=%s delegation_decision=%s turn=%s",
            streaming,
            getattr(engine._reasoner, "_streaming", None),
            delegation_decision,
            state.get("turn_count"),
        )

        def _live_event_sink(event):  # noqa: ANN001
            run.events.append(event)
            run.seq = max(run.seq, event.seq_id)
            _capture_control_event(event)

        closing_turn = state["turn_count"] >= engine._max_reasoning_turns and any(
            message.get("role") == "tool" for message in state["messages"]
        )
        instructions = spec.prompt.instructions or ""
        if delegation_synthesis:
            instructions += (
                "\n\n已调度的子任务均已结束。现在必须直接综合已有子任务结果回答"
                "用户；不得再次派发、描述将要执行的计划或把处理中话术当成答案。"
                "若部分子任务失败，应明确指出失败项，并基于其余结果给出当前最佳结论。"
            )
        if closing_turn:
            instructions += (
                "\n\n这是本次执行的最后一轮。禁止再调用或模拟调用工具，禁止描述"
                "后续计划或说将继续查找。必须现在基于已有证据输出完整、可交付的"
                "当前最佳答案；证据不足的部分直接标为未验证。"
            )
        reason_messages = list(state["messages"])
        if closing_turn:
            # Several OpenAI-compatible models follow the newest user
            # message more reliably than a system-prompt suffix.  Without
            # this explicit hand-off GLM can keep emitting a textual call
            # to a tool used in the previous turn even though the closing
            # turn deliberately has no tool schema.  The provider then
            # (correctly) rejects that call as undeclared and turns an
            # otherwise useful research run into a failure.  Keep the
            # safety boundary -- no tools are disclosed -- but make the
            # required transition from evidence collection to synthesis
            # unambiguous in the conversational context as well.
            reason_messages.append(
                {
                    "role": "user",
                    "content": (
                        "资料收集阶段已经结束。不要再搜索、查看网页或调用任何工具；"
                        "请只使用上文已有证据，现在直接给出最终结论。若证据不足，"
                        "明确标为未验证。"
                    ),
                }
            )
        skill_tools = engine._skill_disclosure.tools(
            run.skill_catalog,
            include_empty_catalog=(
                run.execution_policy is None
                and not run.handle.native_ref.get("parent_run_id")
                and not delegation_synthesis
            ),
        )
        synthesis_tools = (
            list(run.tools.values()) + skill_tools + engine._mcp_disclosure.tools(run.mcp_catalog)
        )
        available_tools = (
            synthesis_tools
            + list(run.sub_agents.values())
            + ([engine._delegation_runtime] if engine._delegation_runtime is not None else [])
        )
        # Tool-selection turns should be short and decisive.  Some
        # reasoning-heavy OpenAI-compatible models can otherwise spend an
        # entire large output budget reconsidering an already obvious
        # delegation after reading Skill/KB results.  Keep the full model
        # budget for the later synthesis/file-writing turn; only cap the
        # parent while ``delegate_task`` is still available.
        #
        # 这个上限是**内容预算**，必须与"关思考"配对：始终思考的端点若在此预算
        # 里开着思考，预算会被 reasoning 吃光、正文与工具调用为 0（2026-09-18
        # 实测三次调用 output 2048 / reasoning 2048，各耗 100-160 秒零产出）。
        # 配对逻辑在 ksadk/model_proxy/compat.should_disable_thinking。
        if not delegation_synthesis and engine._delegation_runtime is not None:
            decision_budget = _delegation_decision_budget()
            max_output_tokens = min(
                max_output_tokens or decision_budget,
                decision_budget,
            )

        # 候选5 工具集装配可见性：本轮最终工具面记账（binding 声明 ↔ 工具面 ↔
        # 记账三方一致）。available_tools 在此处按 closing_turn/delegation_synthesis
        # 动态裁剪，故 per-turn 记账，作 G24 发现门的前置防线。源归属按装配分段。
        final_tools = (
            [] if closing_turn else synthesis_tools if delegation_synthesis else available_tools
        )
        run.events.append(
            engine._event(
                run,
                EventType.TOOL_SURFACE,
                {
                    "turn_id": turn_id,
                    "closing_turn": closing_turn,
                    "delegation_synthesis": delegation_synthesis,
                    "tool_names": [getattr(t, "name", str(t)) for t in final_tools],
                    "sources": {
                        "host": [getattr(t, "name", str(t)) for t in run.tools.values()],
                        "skill": [getattr(t, "name", str(t)) for t in skill_tools],
                        "mcp": [
                            getattr(t, "name", str(t))
                            for t in engine._mcp_disclosure.tools(run.mcp_catalog)
                        ],
                        "subagent": [getattr(t, "name", str(t)) for t in run.sub_agents.values()],
                        "delegation": (
                            [getattr(engine._delegation_runtime, "name", "delegate_task")]
                            if engine._delegation_runtime is not None
                            else []
                        ),
                    },
                },
            )
        )

        model_chain = (spec.model.profile_ref, *spec.model.fallback_profile_refs)
        active_model_ref = str(state.get("active_model_ref") or spec.model.profile_ref)
        if active_model_ref not in model_chain:
            active_model_ref = spec.model.profile_ref
        # Use a fallback for three additional semantic turns, then probe the
        # configured primary again.  This avoids immediate timeout thrashing
        # while preventing an entire long task from remaining on a fallback that
        # later becomes rate-limited.
        active_model_turns = max(0, int(state.get("active_model_turns") or 0))
        if active_model_ref != spec.model.profile_ref and active_model_turns >= 3:
            active_model_ref = spec.model.profile_ref
        active_index = model_chain.index(active_model_ref)

        return ReasonInput(
            model_ref=active_model_ref,
            fallback_model_refs=tuple(model_chain[active_index + 1 :]),
            provider_policy=spec.model.provider_policy,
            instructions=instructions,
            messages=reason_messages,
            # A synthesis turn must not spawn more children, but it still needs
            # ordinary tools to produce the deliverable the user requested (for
            # example, save a Markdown report). Removing every tool here made GLM
            # emit a textual write call against an empty schema, which could only
            # fail after all children had already completed.
            tools=(
                [] if closing_turn else synthesis_tools if delegation_synthesis else available_tools
            ),
            reasoner=engine._reasoner,
            agent_id=run.state.agent_id,
            user_id=run.state.user_id,
            session_id=run.state.session_id,
            run_id=run.handle.run_id,
            seq_start=run.seq,
            max_turns=engine._max_reasoning_turns,
            max_output_tokens=max_output_tokens,
            reasoning_effort=(str(run.request.config.get("effort") or "").strip().lower() or None),
            streaming=streaming,
            live_event_sink=_live_event_sink if streaming else None,
        )

    try:
        reason_input = _reason_input()
        if engine._context_engine is not None and state["turn_count"] > 1:
            prepared_messages = await engine._context_pipeline.prepare_turn_context(
                run,
                list(reason_input.messages),
                reason_input.instructions,
                list(reason_input.tools),
            )
            if prepared_messages != list(reason_input.messages):
                state["messages"] = list(prepared_messages)
                reason_input = replace(
                    reason_input,
                    messages=prepared_messages,
                    # 压缩摘要本身会发模型/usage 事件并推进 seq；主调用必须从
                    # 最新序号继续，不能复用压缩前的 seq_start。
                    seq_start=run.seq,
                )
        out = await reason_turn_async(state["turn_count"], reason_input)
    except ModelFailoverExhausted as exc:
        # reason_turn 在最后一次失败时仍必须把每次 started/failed 审计事件交还
        # 引擎，不能因异常路径丢失配对事实。
        for event in exc.events:
            if event.seq_id <= run.seq:
                continue
            run.events.append(event)
            run.seq = max(run.seq, event.seq_id)
        if exc.stop_reason.value == "recover_context" and engine._context_engine is not None:
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
                    if event.seq_id <= run.seq:
                        continue
                    run.events.append(event)
                    run.seq = max(run.seq, event.seq_id)
                # 模型调用彻底失败：钩子降级事实同样不能丢，此刻 seq 已推进到位。
                flush_hook_degradations(engine, run)
                raise retry_exc.runtime_error() from retry_exc
        else:
            flush_hook_degradations(engine, run)
            raise exc.runtime_error() from exc
    except ReasoningLimitError as exc:
        flush_hook_degradations(engine, run)
        raise RuntimeError(str(exc)) from exc
    # 部分 Provider 在最后一个披露工具返回后会给出空 assistant 消息，随即把
    # Run 当成正常结束。只在本 Run 已执行过工具且尚未重试时，增加一次无工具
    # 收口轮；避免 Skill/MCP 已拿到 L3 证据却没有最终答案。
    assistant_text = "\n".join(
        str(message.get("content") or "").strip()
        for message in out.new_messages
        if message.get("role") == "assistant"
    ).strip()
    unfinished_delegation_answer = bool(
        state.get("delegation_synthesis_pending") and _DELEGATION_PREFACE.search(assistant_text)
    )
    unfinished_answer = unfinished_delegation_answer or looks_like_unfinished_answer(assistant_text)
    if (
        out.route == "final"
        and (not assistant_text or unfinished_answer)
        and any(message.get("role") == "tool" for message in state["messages"])
        and int(state.get("finalization_retries") or 0) < 2
        and ("reason", "tool_calls") in set(run.compiled.plan.edges or ())
    ):
        state["finalization_retries"] = int(state.get("finalization_retries") or 0) + 1
        out.events = [
            event
            for event in out.events
            if not (event.event_type == EventType.TEXT_COMPLETED and event.phase == "final_answer")
        ]
        out.new_messages = []
        out.pending_tool_calls = []
        out.route = "tool_calls"
        state["messages"].append(
            {
                "role": "user",
                "content": (
                    "上一轮只是描述了将要继续的工作，尚未形成可交付结果。"
                    "不要停在计划或意图：如果任务尚未完成，立即调用必要工具"
                    "继续执行；只有在用户要求的产物和验证已经完成时才输出"
                    "最终答案。"
                ),
            }
        )
    # A correction can arrive while the model request is in flight, after
    # this phase's entry drain. The response was generated against the old
    # goal: do not publish it as final or execute its now-stale tool batch.
    # Re-enter reason within this Run; retain the model/usage audit facts.
    post_model_steers = engine._steering.drain_steering(run.handle.run_id)
    if post_model_steers:
        out.events = [
            event
            for event in out.events
            if not (event.event_type == EventType.TEXT_COMPLETED and event.phase == "final_answer")
        ]
        out.new_messages = []
        out.pending_tool_calls = []
        out.route = "reason"
        state["messages"].extend(
            {"role": "user", "content": f"【运行中指令】{text}"}
            for text in post_model_steers
        )
    if out.route == "final" and state.get("delegation_synthesis_pending"):
        state["delegation_synthesis_pending"] = False
    for ev in out.events:
        if ev.seq_id <= run.seq:
            continue
        # 会话串轮修复（交接记录 2026-09-22 §7）：把当前 turn_id 写进
        # final-answer 的 TEXT_COMPLETED payload，让无状态的
        # managed_runtime._project_event 能据此派生 turn 级 final item_id，
        # 避免同 run 多轮 final 撞到同一 item_id 而互相遮蔽。
        if ev.event_type == EventType.TEXT_COMPLETED and ev.phase == "final_answer":
            ev.payload["turn_id"] = turn_id
        # 长任务方案 §6.3：Actual Token 回填到最近一次 ContextManifest
        # （actual_usage_ref 指向 usage.reported 事件）。同时在 usage 事件 payload
        # 落 manifest_id —— 事件流层面可直接查询「某次模型调用实际对应哪个
        # Manifest」（§6.2 闭环）。
        if ev.event_type == EventType.USAGE_REPORTED and run.context_manifest:
            ev.payload["manifest_id"] = run.context_manifest.manifest_id
            run.context_manifest = run.context_manifest.with_actual(
                input_tokens=int(ev.payload.get("input_tokens") or 0),
                output_tokens=int(ev.payload.get("output_tokens") or 0),
                usage_ref=ev.event_id,
            )
        run.events.append(ev)
        run.seq = max(run.seq, ev.seq_id)
        _capture_control_event(ev)
        if ev.event_type == EventType.USAGE_REPORTED:
            state["usage_tokens"] = (
                int(state.get("usage_tokens") or 0)
                + int(ev.payload.get("input_tokens") or 0)
                + int(ev.payload.get("output_tokens") or 0)
            )
    state["messages"].extend(out.new_messages)
    if out.selected_model_ref:
        previous_model_ref = str(state.get("active_model_ref") or spec.model.profile_ref)
        primary_failed_this_turn = any(
            event.event_type == EventType.MODEL_CALL_FAILED
            and str(event.payload.get("model") or "") == spec.model.profile_ref
            for event in out.events
        )
        state["active_model_ref"] = out.selected_model_ref
        if out.selected_model_ref == spec.model.profile_ref:
            state["active_model_turns"] = 0
        elif primary_failed_this_turn or out.selected_model_ref != previous_model_ref:
            state["active_model_turns"] = 0
        else:
            state["active_model_turns"] = max(0, int(state.get("active_model_turns") or 0)) + 1
    state["pending_tool_calls"] = out.pending_tool_calls
    state["tool_batch_succeeded"] = []
    state["tool_batch_failed"] = False
    state["route"] = out.route
    # G5 §5.2：钩子降级事件在 loop 事件合并完毕（run.seq 已正确推进）后统一
    # flush——早于此刻 engine._event 会让 run.seq 领先 loop 的 seq_start 本地
    # 编号，破坏配对与 seq 单调（流式 live sink 亦同）。
    flush_hook_degradations(engine, run)
    if post_model_steers:
        run.events.append(
            engine._event(
                run,
                EventType.STEER_DELIVERED,
                {
                    "message_count": len(post_model_steers),
                    "delivered_at_turn": state["turn_count"] + 1,
                },
            )
        )
    run.events.append(
        engine._event(
            run,
            EventType.TURN_COMPLETED,
            {"turn_id": turn_id, "turn_number": state["turn_count"]},
        )
    )
    if run.controller is not None:
        state["run_control"] = run.controller.snapshot()
        if run.controller.stop_reason:
            raise RunControlStop(run.controller.stop_reason)
    return PhaseResult(
        state=state,
        events=list(run.events[events_start:]),
        route=state.get("route"),
    )


__all__ = ["reason_phase"]
