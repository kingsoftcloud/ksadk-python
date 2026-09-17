"""ManagedLangGraphEngine 的图构建模块（从 langgraph.py 拆出）。

按 ExecutionPlan 组装 LangGraph 图（节点实现 + 边 + 条件路由），
并随引擎注入 Checkpointer。langgraph 类型不越出 engine 包
（tests/architecture/test_harness_contract.py 守卫）。
"""

from __future__ import annotations

import os
import re
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ksadk.harness.engine import budgets
from ksadk.harness.engine.base import ExecutionEngineError
from ksadk.harness.engine.langgraph import _GraphState
from ksadk.harness.engine.mcp_disclosure import MCP_CALL_TOOL_TOOL, McpDisclosureCursors
from ksadk.harness.engine.policy_runtime import revalidate_policy
from ksadk.harness.engine.spans import wrap_node_span
from ksadk.harness.engine.subagents import ChildApprovalPending
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
from ksadk.harness.run_control import ControlAction, RunControlStop

_DELEGATION_PREFACE = re.compile(
    r"(?:\blet\s+me\s+(?:delegate|dispatch)|"
    r"\bi\s+(?:will|'ll|am\s+going\s+to)\s+(?:delegate|dispatch)|"
    r"(?:让我|我会|我将|接下来|准备|正在)(?:先)?(?:并行)?(?:派发|分派|调度|处理).{0,80}(?:子任务|子智能体|subtasks?))",
    re.IGNORECASE,
)


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
            budgets.spend(run, "tokens", int(event.payload.get("total_tokens") or
                                             input_tokens + output_tokens))
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
        run.events.append(
            engine._event(
                run,
                EventType.TURN_STARTED,
                {"turn_id": turn_id, "turn_number": state["turn_count"]},
            )
        )

        def _reason_input() -> ReasonInput:
            max_total_tokens = budgets.limit(run, "tokens")
            if (
                run.controller is not None
                and run.controller.spec.limits.max_total_tokens is not None
            ):
                controlled_limit = run.controller.spec.limits.max_total_tokens
                max_total_tokens = (
                    min(int(max_total_tokens), controlled_limit)
                    if max_total_tokens is not None
                    else controlled_limit
                )
            # Model Profile 的输出上限是每次调用的硬限制；Run 总预算则是
            # 随执行消耗递减的剩余额度。两者同时存在时必须取更小值。
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
                    min(max_output_tokens, remaining)
                    if max_output_tokens is not None
                    else remaining
                )
            streaming = getattr(engine._reasoner, "_streaming", None) is True or os.getenv(
                "KSADK_MODEL_STREAMING", ""
            ).strip().lower() in {"1", "true", "yes", "on"}

            def _live_event_sink(event):
                run.events.append(event)
                run.seq = max(run.seq, event.seq_id)
                _capture_control_event(event)

            delegation_synthesis = bool(state.get("delegation_synthesis_pending"))
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
            synthesis_tools = (
                list(run.tools.values())
                + engine._skill_disclosure.tools(run.skill_catalog)
                + engine._mcp_disclosure.tools(run.mcp_catalog)
            )
            available_tools = (
                synthesis_tools
                + list(run.sub_agents.values())
                + (
                    [engine._delegation_runtime]
                    if engine._delegation_runtime is not None
                    else []
                )
            )

            return ReasonInput(
                model_ref=spec.model.profile_ref,
                fallback_model_refs=spec.model.fallback_profile_refs,
                provider_policy=spec.model.provider_policy,
                instructions=instructions,
                messages=reason_messages,
                # A synthesis turn must not spawn more children, but it still
                # needs ordinary tools to produce the deliverable the user
                # requested (for example, save a Markdown report). Removing
                # every tool here made GLM emit a textual write call against an
                # empty schema, which could only fail after all children had
                # already completed.
                tools=(
                    []
                    if closing_turn
                    else synthesis_tools if delegation_synthesis else available_tools
                ),
                reasoner=engine._reasoner,
                agent_id=run.state.agent_id,
                user_id=run.state.user_id,
                session_id=run.state.session_id,
                run_id=run.handle.run_id,
                seq_start=run.seq,
                max_turns=engine._max_reasoning_turns,
                max_output_tokens=max_output_tokens,
                streaming=streaming,
                live_event_sink=_live_event_sink if streaming else None,
            )

        try:
            out = await reason_turn_async(state["turn_count"], _reason_input())
        except ModelFailoverExhausted as exc:
            # reason_turn 在最后一次失败时仍必须把每次 started/failed
            # 审计事件交还引擎，不能因异常路径丢失配对事实。
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
                    raise RuntimeError(str(retry_exc)) from retry_exc
            else:
                raise RuntimeError(str(exc)) from exc
        except ReasoningLimitError as exc:
            raise RuntimeError(str(exc)) from exc
        # 部分 Provider 在最后一个披露工具返回后会给出空 assistant 消息，
        # 随即把 Run 当成正常结束。只在本 Run 已执行过工具且尚未重试时，
        # 增加一次无工具收口轮；避免 Skill/MCP 已拿到 L3 证据却没有最终答案。
        assistant_text = "\n".join(
            str(message.get("content") or "").strip()
            for message in out.new_messages
            if message.get("role") == "assistant"
        ).strip()
        unfinished_delegation_answer = bool(
            state.get("delegation_synthesis_pending")
            and _DELEGATION_PREFACE.search(assistant_text)
        )
        if (
            out.route == "final"
            and (not assistant_text or unfinished_delegation_answer)
            and any(message.get("role") == "tool" for message in state["messages"])
            and int(state.get("finalization_retries") or 0) < 1
            and ("reason", "tool_calls") in set(run.compiled.plan.edges or ())
        ):
            state["finalization_retries"] = int(state.get("finalization_retries") or 0) + 1
            out.events = [
                event
                for event in out.events
                if not (
                    event.event_type == EventType.TEXT_COMPLETED and event.phase == "final_answer"
                )
            ]
            out.new_messages = []
            out.pending_tool_calls = []
            out.route = "tool_calls"
            state["messages"].append(
                {
                    "role": "user",
                    "content": (
                        "上一轮没有形成可交付答案。所有已派发任务都已经结束，"
                        "请立即基于现有工具和子任务结果给出完整答案；不得描述"
                        "将要派发、继续处理或稍后完成。"
                    ),
                }
            )
        if out.route == "final" and state.get("delegation_synthesis_pending"):
            state["delegation_synthesis_pending"] = False
        for ev in out.events:
            if ev.seq_id <= run.seq:
                continue
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
            _capture_control_event(ev)
            if ev.event_type == EventType.USAGE_REPORTED:
                state["usage_tokens"] = (
                    int(state.get("usage_tokens") or 0)
                    + int(ev.payload.get("input_tokens") or 0)
                    + int(ev.payload.get("output_tokens") or 0)
                )
        state["messages"].extend(out.new_messages)
        state["pending_tool_calls"] = out.pending_tool_calls
        state["tool_batch_succeeded"] = []
        state["tool_batch_failed"] = False
        state["route"] = out.route
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
        return state

    async def tool_calls(state: _GraphState) -> _GraphState:
        await revalidate_policy(engine, run)
        _restore_controller(state)
        run.child_approval_decision = state.get("child_approval_decision")
        state["child_approval_decision"] = {}
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
            return name == MCP_CALL_TOOL_TOOL and engine._mcp_disclosure.approval_decider(arguments)

        class _EngineToolExecutor:
            @staticmethod
            def validate_arguments(name, arguments):  # type: ignore[no-untyped-def]
                tool = run.tools.get(name)
                if tool is None:
                    return None
                raw_schema = getattr(tool, "parameters", None)
                # Host tests and backward-compatible integrations may still
                # register a plain callable.  With no published schema there
                # is nothing to validate here; execution retains its previous
                # behaviour.
                if not isinstance(raw_schema, dict):
                    return None
                schema = raw_schema
                required = schema.get("required") or ()
                missing = [
                    str(field)
                    for field in required
                    if str(field) not in arguments
                    or arguments.get(str(field)) is None
                ]
                if missing:
                    return "missing required fields: " + ", ".join(missing)
                try:
                    from jsonschema.validators import validator_for

                    validator = validator_for(schema)(schema)
                    error = next(validator.iter_errors(arguments), None)
                except Exception as exc:  # malformed host schema is not a model error
                    return f"tool schema validation failed: {exc}"
                return f"schema mismatch: {error.message}" if error is not None else None

            async def execute(self, name, arguments):  # type: ignore[no-untyped-def]
                return await engine._invoke_tool(name, arguments, run=run, mcp_cursors=cursors)

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
        def _parallel_safe(name, arguments):
            sub = run.sub_agents.get(name)
            if sub is not None:
                return not (sub.tools or (sub.inherit_skills and run.skill_catalog)
                            or (sub.inherit_mcp and run.mcp_catalog))
            return bool(
                engine._delegation_runtime is not None
                and engine._delegation_runtime.is_tool(name)
                and engine._capability_runtime is None
                and engine._delegation_runtime.parallel_safe(
                    arguments,
                    parent_run=run,
                )
            )

        # Commit sequential siblings at separate checkpoint boundaries. Only
        # children proven unable to request approval may share a parallel node.
        parallel = len(pending_tool_calls) > 1 and engine._capability_runtime is None and all(
            _parallel_safe(p["name"], p["arguments"])
            and p["name"] not in run.approval_required
            and not (run.sub_agents.get(p["name"]) and run.sub_agents[p["name"]].depends_on)
            for p in pending_tool_calls
        ) and spec.execution_strategy.config.get("subagent_failure_mode", "partial") != "fail_fast"
        remaining_calls = [] if parallel else pending_tool_calls[1:]
        pending_tool_calls = pending_tool_calls if parallel else pending_tool_calls[:1]
        # Chat only needs an aggregate lifecycle signal. Provider selection,
        # child labels and task bodies remain available in the execution trace
        # and tool receipts, but are deliberately not expanded into one visible
        # progress card per child.
        delegation_calls = (
            [
                pending
                for pending in pending_tool_calls
                if engine._delegation_runtime is not None
                and engine._delegation_runtime.is_tool(str(pending.get("name") or ""))
            ]
            if engine._delegation_runtime is not None
            else []
        )
        delegation_labels: list[str] = []
        ordinary_calls = [
            pending for pending in pending_tool_calls if pending not in delegation_calls
        ]

        def _emit_tool_progress(status: str) -> None:
            if not ordinary_calls:
                return
            tools = [str(pending.get("name") or "tool") for pending in ordinary_calls]
            lowered = " ".join(tools).lower()
            activity_kind = (
                "write"
                if any(token in lowered for token in ("write", "edit", "save"))
                else "search"
                if "search" in lowered
                else "fetch"
                if "fetch" in lowered
                else "read"
                if any(token in lowered for token in ("read", "list"))
                else "command"
                if any(token in lowered for token in ("command", "shell", "exec"))
                else "tool"
            )
            batch_id = ":".join(
                str(pending.get("call_id") or index)
                for index, pending in enumerate(ordinary_calls, start=1)
            )
            # Repeated search/fetch/read turns are useful trace evidence but
            # become dozens of duplicate chat rows during long research. Keep
            # one public lifecycle per human activity class.
            if status == "running":
                if activity_kind in run.public_tool_activity_kinds:
                    return
                run.public_tool_activity_kinds.add(activity_kind)
                run.public_tool_activity_batches.add(batch_id)
            elif batch_id not in run.public_tool_activity_batches:
                return
            progress_event = engine._event(
                run,
                EventType.RUN_PROGRESS,
                {
                    "kind": "tool.batch",
                    "status": status,
                    "count": len(ordinary_calls),
                    "batch_id": batch_id,
                    "activity_kind": activity_kind,
                    # Tool arguments and results stay in Trace/receipts.  The
                    # chat surface only receives stable capability names and
                    # turns them into human activity summaries.
                    "tools": tools,
                },
            )
            run.events.append(progress_event)
            _capture_control_event(progress_event)

        def _emit_delegation_progress(status: str) -> None:
            if not delegation_calls:
                return
            progress_event = engine._event(
                run,
                EventType.RUN_PROGRESS,
                {
                    "kind": "delegation.batch",
                    "status": status,
                    "count": len(delegation_calls),
                    "labels": delegation_labels,
                },
            )
            run.events.append(progress_event)
            _capture_control_event(progress_event)

        if delegation_calls:
            delegation_labels = [
                engine._delegation_runtime.public_label(
                    dict(pending.get("arguments") or {}),
                    call_id=str(pending.get("call_id") or ""),
                )
                for pending in delegation_calls
            ]
            _emit_delegation_progress("running")
        _emit_tool_progress("running")
        has_subagent_dependencies = any(
            run.sub_agents[name].depends_on
            for name in (str(call.get("name") or "") for call in pending_tool_calls)
            if name in run.sub_agents
        )
        subagent_failure_mode = str(
            run.compiled.spec.execution_strategy.config.get("subagent_failure_mode", "partial")
        )
        def _live_tool_event(event):
            if (event.event_type == EventType.TOOL_CALL_BEGIN
                    and str(event.payload.get("call_id") or "") in run.budget_tool_calls):
                return  # The same pending delegate resumes; it does not start twice.
            run.seq += 1
            event.seq_id = run.seq
            run.events.append(event)
            _capture_control_event(event)

        try:
            out = await execute_tool_calls(
                ToolCallInput(
                    pending_tool_calls=pending_tool_calls,
                    approval_required=frozenset(run.approval_required),
                    approval_decider=_mcp_approval_decider,
                    parallel_safe_decider=(
                        lambda name, _arguments: (
                            (
                                name in run.sub_agents and _parallel_safe(name, _arguments)
                                or (
                                    engine._delegation_runtime is not None
                                    and engine._delegation_runtime.is_tool(name)
                                )
                            )
                            and not has_subagent_dependencies
                            and subagent_failure_mode != "fail_fast"
                        )
                    ),
                    stop_on_error_decider=(
                        lambda name, _arguments: (
                            subagent_failure_mode == "fail_fast" and name in run.sub_agents
                        )
                    ),
                    cancel_pending_decider=lambda name, _arguments: (
                        name in run.sub_agents
                        or (
                            engine._delegation_runtime is not None
                            and engine._delegation_runtime.is_tool(name)
                        )
                    ),
                    dependencies={
                        name: tuple(spec.depends_on)
                        for name, spec in run.sub_agents.items()
                        if spec.depends_on
                    },
                    max_parallelism=int(
                        run.compiled.spec.execution_strategy.config.get("max_parallel_subagents", 4)
                    ),
                    capability_runtime=engine._capability_runtime,
                    tenant_id=engine._tenant_id,
                    authorization_agent_id=run.request.metadata.get("authorization_agent_id"),
                    succeeded_tools=frozenset(state.get("tool_batch_succeeded") or ()),
                    prior_failure=bool(state.get("tool_batch_failed")),
                    live_event_sink=_live_tool_event,
                    argument_validator=_EngineToolExecutor.validate_arguments,
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
        except ChildApprovalPending as exc:
            state["child_approval"] = exc.detail
            state["route"] = "child_approval"
            state["budget"] = budgets.snapshot(run)
            return state
        except BaseException:
            _emit_delegation_progress("failed")
            _emit_tool_progress("failed")
            raise
        for ev in out.events:
            _capture_control_event(ev)
            if ev.event_type == EventType.TOOL_CALL_END:
                if not ev.payload.get("error"):
                    state.setdefault("tool_batch_succeeded", []).append(ev.payload["name"])
                elif (
                    subagent_failure_mode == "fail_fast"
                    and ev.payload.get("name") in run.sub_agents
                ):
                    state["tool_batch_failed"] = True
        tool_errors = any(
            event.event_type == EventType.TOOL_CALL_END and event.payload.get("error")
            for event in out.events
            if str(event.payload.get("name") or "")
            not in {str(call.get("name") or "") for call in delegation_calls}
        )
        _emit_tool_progress("failed" if tool_errors else "completed")
        # 收口 6：子 Agent 事件统一重排并入（tool.call.begin/end 之后）。
        from ksadk.harness.subagent import resequence_child_events

        child_events = engine._pending_child_events.pop(run.handle.run_id, [])
        for event in child_events:
            _capture_control_event(event)
        resequence_child_events(run, child_events)
        subagent_events = engine._pending_subagent_events.pop(run.handle.run_id, {})
        for pending in pending_tool_calls:
            pending_events = subagent_events.get(str(pending.get("call_id") or ""), [])
            for event in pending_events:
                _capture_control_event(event)
            resequence_child_events(
                run,
                pending_events,
            )
        if delegation_calls:
            delegated_call_ids = {str(pending.get("call_id") or "") for pending in delegation_calls}
            failed = any(
                event.event_type == EventType.TOOL_CALL_END
                and str(event.payload.get("call_id") or "") in delegated_call_ids
                and bool(event.payload.get("error"))
                for event in out.events
            )
            _emit_delegation_progress("failed" if failed else "completed")
            state["delegation_synthesis_pending"] = True
        state["messages"].extend(out.new_messages)
        if out.working_context is not None:
            run.state.working_context = out.working_context
        state["pending_tool_calls"] = remaining_calls
        state["mcp_listed"] = sorted(cursors.listed)
        state["mcp_schema_read"] = sorted(cursors.schema_read)
        state["route"] = "tool_calls" if remaining_calls else out.route
        if run.controller is not None:
            if (
                run.controller.replan_reason
                and run.controller.guidance_emitted_revision != run.controller.guidance_revision
            ):
                run.events.append(
                    engine._event(
                        run,
                        EventType.RUN_PROGRESS,
                        {
                            "kind": "run_control",
                            "status": "in_progress",
                            "action": "replan",
                            "reason": run.controller.replan_reason,
                            "plan_revision": run.controller.guidance_revision,
                        },
                    )
                )
            state["run_control"] = run.controller.snapshot()
        return state

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
        turn: HarnessReasoningTurn = await engine._reasoner.complete(
            model=spec.model.profile_ref,
            prompt=(spec.prompt.instructions or "") + "\n请先给出分步执行计划，再开始执行。",
            messages=tuple(state["messages"]),
            tools=[],
        )
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
    reason_mapping["final"] = END if exit_target == "final" else exit_target
    if "reason" in node_impls and "reason" in plan.nodes and "reason" != exit_target:
        builder.add_conditional_edges("reason", route, reason_mapping)
    for src, dst in edges:
        if src == "reason":
            continue  # 已由条件边覆盖。
        if src == "tool_calls":
            builder.add_conditional_edges("tool_calls", route, {
                "tool_calls": "tool_calls", "child_approval": "child_approval", "reason": dst,
            })
            continue
        if dst == "final":
            if src in node_impls and src != "reason":
                builder.add_edge(src, END)
            continue
        builder.add_edge(src, dst)
    graph = builder.compile(checkpointer=engine._checkpointer)
    return graph
