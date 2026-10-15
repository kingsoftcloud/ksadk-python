"""tool_calls phase handler (Native Harness Phase 1, Task 5).

Extracted from the ``tool_calls(state)`` closure in
``ksadk/harness/engine/graph_builder.py``. Dependencies that were
closure-captured in the graph builder (``spec``/``run``/``engine``/``state``)
now arrive on the :class:`PhaseContext`; the wrapper in graph_builder keeps the
graph-only preamble (``revalidate_policy`` / ``_restore_controller`` /
child-approval decision intake).

Suspension: the closure called LangGraph ``interrupt()`` from two places — the
tool-approval resolver (``_GraphApprovalResolver``) and the child-agent
approval node. Both are now *explicit results*:

* tool approval → ``_SuspensionRequest`` raised by ``_HandlerApprovalResolver``,
  caught at the ``execute_tool_calls`` call site and returned as
  ``PhaseResult(interaction=NeedInteraction(kind="tool_approval", ...))``;
* child approval → the ``ChildApprovalPending`` branch returns
  ``PhaseResult(interaction=NeedInteraction(kind="child_approval", ...))``.

The caller (graph_builder for LangGraph, a Native engine in Phase 2) is the one
that translates ``NeedInteraction`` back into its suspension primitive. On
resume the caller re-enters this handler and the stored decisions are consumed
here (``tool_approval_decision`` / ``child_approval_decision`` on the graph
state).

This module must NOT import LangGraph; the architecture guard
(``tests/architecture/test_harness_contract.py``) enforces this from Task 6.

Behaviour preservation is the contract: event ordering, payloads, seq
accounting, progress lifecycle events and the fail-fast/parallel bookkeeping are
a mechanical copy of the closure. The direct ``run.events``/``run.seq`` writes
are kept for now (Phase 2 severs them); every event appended during the handler
is also returned on :class:`PhaseResult`.
"""

from __future__ import annotations

import logging
from typing import Any

from ksadk.harness.engine import budgets
from ksadk.harness.engine.hooked_reasoner import (
    buffer_hook_degradation,
    flush_hook_degradations,
)
from ksadk.harness.engine.mcp_disclosure import MCP_CALL_TOOL_TOOL, McpDisclosureCursors
from ksadk.harness.engine.phases.context import PhaseContext, PhaseResult
from ksadk.harness.engine.phases.interaction import NeedInteraction
from ksadk.harness.engine.subagents import ChildApprovalPending
from ksadk.harness.engine.tool_dispatch import RunToolResultGovernor
from ksadk.harness.events import EventType
from ksadk.harness.hooks import run_hooks
from ksadk.harness.loop import ToolCallInput, execute_tool_calls
from ksadk.harness.run_control import ControlAction

logger = logging.getLogger(__name__)

#: 图状态里"已恢复的工具审批决定"键（包装层在 interrupt() 返回后写入本 handler 消费）。
TOOL_APPROVAL_DECISION_KEY = "tool_approval_decision"

#: run 属性名：调用方（LangGraph 包装层）在节点入口写入"本次节点调用正在重放
#: 哪个已挂起审批的 call_id"，供挂起分支跳过重复补发的失败态进度（属性缺失 =
#: 首次挂起；Phase 2 Native 引擎以 NeedInteraction 表达挂起，不写该属性）。
TOOL_APPROVAL_REPLAY_ATTR = "tool_approval_replay_call_id"


class _SuspensionRequest(Exception):
    """Internal: handler's approval resolver signals a needed interaction."""

    def __init__(self, interaction: NeedInteraction) -> None:
        self.interaction = interaction


class _HandlerApprovalResolver:
    """ApprovalResolver that surfaces a NeedInteraction instead of interrupt().

    First pass raises ``_SuspensionRequest``; the caller suspends and (after the
    external decision arrives) re-enters the handler with the decision stored on
    the graph state under :data:`TOOL_APPROVAL_DECISION_KEY`, which this resolver
    consumes exactly once.
    """

    def __init__(self, state: dict[str, Any], expected_revision: int = 0) -> None:
        self._state = state
        self._expected_revision = expected_revision

    def request(self, *, call_id: str, name: str, arguments: dict[str, Any]) -> str:
        resolved = self._state.get(TOOL_APPROVAL_DECISION_KEY) or {}
        if resolved.get("call_id") == call_id:
            self._state.pop(TOOL_APPROVAL_DECISION_KEY, None)
            return str(resolved.get("decision") or "")
        raise _SuspensionRequest(
            NeedInteraction(
                interaction_id=f"ap-{call_id}",
                kind="tool_approval",
                request_payload={
                    "call_id": call_id,
                    "name": name,
                    "args": arguments,
                    "risk": "high",
                },
                resume_phase="tools",
                # Phase 1：LangGraph 通道不消费 revision；Phase 2 接 Native snapshot revision。
                expected_revision=self._expected_revision,
            )
        )


def _make_tool_result_hook(engine: Any, run: Any) -> Any:
    """tool_calls 节点的 on_tool_result 钩子闭包（G5 §5.2）。

    内部经 ``run_hooks`` 按注册顺序执行钩子链。降级事件写入引擎级缓冲
    （``engine._pending_hook_degradations``），由 tool_calls 节点末尾统一
    flush——与 reason 节点同一 loop-safe 纪律：**不得**在钩子执行中途调
    ``engine._event``，否则 ``run.seq`` 会领先于 loop 以 ``seq_start`` 计的本地
    编号，导致本节点事件被丢弃或 seq 撞号。

    返回改写后的 result dict（``content`` 变化即视为改写生效，调用侧据此加
    ``modified_by_hook`` 留痕）；未改写或协议违约时返回 None（不加标记）。
    """
    hooks = engine._hooks

    def _degrade(run_ctx: Any, event_type: str, payload: dict[str, Any]) -> None:
        buffer_hook_degradation(engine, run_ctx.run_id, event_type, payload)

    async def _hook(
        run_ctx: Any, call: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any] | None:
        original = str(result.get("content") or "")
        rewritten = await run_hooks(
            hooks,
            "on_tool_result",
            run_ctx,
            _degrade,
            payload={"call": dict(call), "result": dict(result)},
        )
        if not isinstance(rewritten, dict):
            # run_hooks 链式守卫下不可达；纵深防御。
            # run_hooks 已对链式违约降级；这里保留末态守卫（与
            # hooked_reasoner._apply_request_hooks 同语义）双保险：末值仍非
            # dict 时丢弃改写、沿用原文，绝不把坏值当结果回填。
            buffer_hook_degradation(
                engine,
                run_ctx.run_id,
                EventType.POLICY_DECISION,
                {
                    "action": "hook_failed",
                    "reason": (
                        "on_tool_result: protocol violation (non-dict); original result kept"
                    ),
                    "outcome": "hook_failed",
                    "hook_outcome": "non_blocking_error",
                    "hook_type": "prompt",
                    "method": "on_tool_result",
                    "error": f"on_tool_result 返回 {type(rewritten).__name__}，期望 dict",
                },
            )
            return None
        if str(rewritten.get("content") or "") == original:
            return None
        return rewritten

    return _hook


async def tools_phase(ctx: PhaseContext) -> PhaseResult:
    """tool_calls 一节点执行（GraphBuilder ``tool_calls`` 节点主体）。

    调用方（graph_builder 的 ``tool_calls`` 包装）已完成 ``revalidate_policy`` /
    ``_restore_controller`` / child-approval 决定接收；handler 负责本节点全部
    事件与状态推进。需要审批时返回 ``PhaseResult(interaction=NeedInteraction)``
    ——由调用方决定挂起原语（LangGraph：``interrupt()``）。
    """

    # 依赖注入：闭包捕获 → ctx 读取。以下局部绑定即 ctx.* 的取值；嵌套 helper
    # （_capture_control_event / _HandlerApprovalResolver）闭包引用同一对象，与
    # graph_builder 中的捕获语义等价（值来自 ctx，非 build_graph 闭包）。
    spec = ctx.spec
    run = ctx.run
    engine = ctx.engine
    state = ctx.state

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
        def public_display(name, arguments, result):
            from ksadk.harness.tool_display import builtin_tool_display

            tool = run.tools.get(name)
            if getattr(tool, "source", None) == "sdk-builtin":
                return builtin_tool_display(name, arguments, result)
            return None

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
                if str(field) not in arguments or arguments.get(str(field)) is None
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
            return not (
                sub.tools
                or (sub.inherit_skills and run.skill_catalog)
                or (sub.inherit_mcp and run.mcp_catalog)
            )
        return bool(
            engine._delegation_runtime is not None
            and engine._delegation_runtime.is_tool(name)
            and engine._delegation_runtime.parallel_safe(
                arguments,
                parent_run=run,
            )
        )

    # Commit sequential siblings at separate checkpoint boundaries. Only
    # children proven unable to request approval may share a parallel node.
    parallel = (
        len(pending_tool_calls) > 1
        and all(
            _parallel_safe(p["name"], p["arguments"])
            and p["name"] not in run.approval_required
            and not (run.sub_agents.get(p["name"]) and run.sub_agents[p["name"]].depends_on)
            for p in pending_tool_calls
        )
        and spec.execution_strategy.config.get("subagent_failure_mode", "partial")
        != "fail_fast"
    )
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
        if (
            event.event_type == EventType.TOOL_CALL_BEGIN
            and str(event.payload.get("call_id") or "") in run.budget_tool_calls
        ):
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
                            name in run.sub_agents
                            and _parallel_safe(name, _arguments)
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
                approval_resolver=_HandlerApprovalResolver(state),
                tool_executor=_EngineToolExecutor(),
                result_governor=RunToolResultGovernor(engine=engine, run=run),
                # G5 §5.2：on_tool_result 钩子闭包（降级经引擎缓冲，节点末尾
                # flush；无钩子时为 None，execute_tool_calls 行为逐字节不变）。
                tool_result_hook=(
                    _make_tool_result_hook(engine, run) if engine._hooks else None
                ),
                agent_id=run.state.agent_id,
                user_id=run.state.user_id,
                session_id=run.state.session_id,
                run_id=run.handle.run_id,
                seq_start=run.seq,
                working_context=run.state.working_context,
            )
        )
    except _SuspensionRequest as sus:
        # 审批挂起（旧行为：resolver 直接 interrupt()，异常穿出 execute_tool_calls
        # 落到 except BaseException 分支）。事件面逐字对齐该分支：先补发失败态
        # 进度，再把本节点已发生的 on_tool_result 降级落到事件流，不让降级事实
        # 滞留缓冲跨挂起边界；随后以 NeedInteraction 交还调用方。
        # 重放守卫：resume 后 LangGraph 会整体重放本节点，而重放期间决定尚未到达
        # （要等调用方 interrupt() 返回）会再走一次本分支；旧行为下那一次是
        # interrupt() 内联返回决定、从不进异常分支，故不得重复补发 failed。调用方
        # 在节点入口把"正在重放的审批 call_id"写为 run 属性（引擎挂起时记录、attach
        # 从 Checkpoint 重建，跨进程恢复亦成立）；属性缺失/不等 = 首次挂起。
        if str(getattr(run, TOOL_APPROVAL_REPLAY_ATTR, "") or "") != str(
            sus.interaction.request_payload.get("call_id") or ""
        ):
            _emit_delegation_progress("failed")
            _emit_tool_progress("failed")
        flush_hook_degradations(engine, run)
        return PhaseResult(
            state=state,
            events=list(run.events[events_start:]),
            interaction=sus.interaction,
        )
    except ChildApprovalPending as exc:
        state["child_approval"] = exc.detail
        state["route"] = "child_approval"
        state["budget"] = budgets.snapshot(run)
        # 审批挂起前把本节点已发生的 on_tool_result 降级落到事件流，
        # 不让降级事实滞留缓冲跨 interrupt 边界。
        flush_hook_degradations(engine, run)
        call_id = str(exc.detail.get("call_id") or "")
        return PhaseResult(
            state=state,
            events=list(run.events[events_start:]),
            route="child_approval",
            interaction=NeedInteraction(
                interaction_id=f"ap-{call_id}",
                kind="child_approval",
                request_payload=dict(exc.detail),
                resume_phase="tools",
                # Phase 1：LangGraph 通道不消费 revision；Phase 2 接 Native snapshot revision。
                expected_revision=0,
            ),
        )
    except BaseException:
        _emit_delegation_progress("failed")
        _emit_tool_progress("failed")
        # 异常路径：降级事实同样不能丢（本节点事件已由 live sink 推进
        # run.seq，此刻 flush 保持 seq 单调）。
        flush_hook_degradations(engine, run)
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
    # G5 §5.2：on_tool_result 降级事件在节点末尾统一 flush（live sink 已使
    # run.seq 与 loop 本地计数对齐；早于本点 flush 抢占不了任何事件，晚于
    # 本点则把事实留到下个节点）。语义与 reason 节点的 flush 完全一致。
    flush_hook_degradations(engine, run)
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
    return PhaseResult(
        state=state,
        events=list(run.events[events_start:]),
        route=state.get("route"),
    )


__all__ = ["tools_phase"]
