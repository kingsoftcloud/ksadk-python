"""Governed tool dispatch shared by root and child Managed executions."""

from __future__ import annotations

import inspect
from typing import Any

from ksadk.harness.engine.mcp_disclosure import McpDisclosureCursors
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.run_control import ControlAction, RunControlReplan, RunControlStop


async def invoke_tool(
    self,
    name: str,
    arguments: dict[str, Any],
    *,
    run: Any = None,
    mcp_cursors: McpDisclosureCursors | None = None,
    call_id: str = "",
) -> Any:
    if run is not None:
        from ksadk.harness.engine import budgets

        # A model that reports over-budget usage cannot spend a separate tool
        # allowance to perform a side effect after exhausting the tree budget.
        budgets.check(run, "tokens", 0)
        if run.controller is not None and (not call_id or call_id not in run.budget_tool_calls):
            exempt_tools = set(
                run.compiled.spec.execution_strategy.config.get("stagnation_exempt_tools", ())
            )
            decision = run.controller.before_tool(
                name,
                arguments,
                stagnation_exempt=name in exempt_tools,
            )
            if decision.action == ControlAction.STOP:
                raise RunControlStop(decision.reason)
            if decision.action == ControlAction.REPLAN:
                raise RunControlReplan(decision.reason)
        if not call_id or call_id not in run.budget_tool_calls:
            budgets.check(run, "tools")
            budgets.spend(run, "tools")
            if call_id:
                run.budget_tool_calls.add(call_id)
            run.tool_calls_started += 1
    # 收口 6：子 Agent 即工具——内联运行到完成，子事件并入父流。
    sub = (run.sub_agents if run is not None else self._sub_agents).get(name)
    if sub is not None and run is not None:
        from ksadk.harness.subagent import run_subagent

        text, _ = await run_subagent(
            engine=self,
            parent_run=run,
            sub=sub,
            task=str((arguments or {}).get("task") or ""),
            call_id=call_id,
        )
        return text
    if self._skill_disclosure.is_tool(name):
        return self._invoke_skill_tool(name, arguments, run=run)
    if self._mcp_disclosure.is_tool(name):
        if mcp_cursors is None:
            mcp_cursors = McpDisclosureCursors()
        return await self._invoke_mcp_tool(
            name,
            arguments,
            run=run,
            cursors=mcp_cursors,
            call_id=call_id,
        )
    tool = (run.tools if run is not None else self._tools).get(name)
    if tool is None:
        raise RuntimeError(
            f"engine tool {name!r} is not available; it may be filtered or unpublished"
        )
    if run is not None and callable(getattr(tool, "drain_artifacts", None)):
        artifact_budget = run.compiled.spec.execution_strategy.config.get("max_artifacts")
        declared_cost = getattr(tool, "artifact_budget_cost", None)
        if artifact_budget is not None and declared_cost is None:
            from ksadk.harness.subagent import SubAgentExecutionError

            raise SubAgentExecutionError(
                "budget_exhausted",
                "artifact-producing tool lacks artifact_budget_cost; "
                "finite budget cannot be proven before execution",
            )
        if artifact_budget is not None:
            artifact_cost = int(declared_cost)
            if artifact_cost < 0 or (run.artifacts_created + artifact_cost > int(artifact_budget)):
                from ksadk.harness.subagent import SubAgentExecutionError

                raise SubAgentExecutionError(
                    "budget_exhausted",
                    f"execution artifact budget {artifact_budget} exhausted before tool execution",
                )
            run.artifacts_created += artifact_cost
            budgets.check(run, "artifacts", artifact_cost)
            budgets.spend(run, "artifacts", artifact_cost)
    call = getattr(tool, "call", None)
    from ksadk.runtime_context import tool_execution_scope

    with tool_execution_scope(
        run.state.session_id if run is not None else "",
        run.handle.run_id if run is not None else "",
    ):
        target = call if callable(call) else tool
        parameters = inspect.signature(target).parameters
        accepts_call_id = "call_id" in parameters or any(
            value.kind is inspect.Parameter.VAR_KEYWORD for value in parameters.values()
        )
        result = await target(arguments, **({"call_id": call_id} if accepts_call_id else {}))
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
