"""Governed tool dispatch shared by root and child Managed executions."""

from __future__ import annotations

import inspect
from dataclasses import replace
from typing import Any

from ksadk.harness.engine.mcp_disclosure import McpDisclosureCursors
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.run_control import ControlAction, RunControlReplan, RunControlStop
from ksadk.harness.tool_result_policy import govern_tool_result, policy_for_tool


class ToolNotDisclosedError(RuntimeError):
    """G24 发现门：工具未在当前披露面中。

    不阻断 Run（被 loop/tools.py 的 except Exception 捕获后回填模型，模型可经
    发现工具自愈重试）；``category`` 记入 TOOL_CALL_END 的 error_category。
    """

    category = "undisclosed_tool"

    def __init__(self, name: str) -> None:
        self.tool_name = name
        super().__init__(self._message(name))

    @staticmethod
    def _message(name: str) -> str:
        return (
            f"工具 {name!r} 未在当前披露面中。它可能需要先经发现工具加载：\n"
            "- MCP 工具：用 mcp_list_tools 查看，mcp_read_tool_schema 读 schema，"
            "再 mcp_call_tool 调用\n"
            "- Skill 工具：用 execute_skills 调用已绑定 Skill\n"
            "若确认该工具应无条件可见，检查 Agent 的 capability binding 是否包含它。"
        )


class RunToolResultGovernor:
    """Run 级中央结果治理器（G10 §13.2 / G16）。

    - 治理元数据取自 ``run.tools`` 中的工具声明（缺省中央默认值）；
    - 引擎内部工具（MCP 披露三件套 / Skill 披露）按外部来源定界（G16），
      Schema/list 类披露结果体积可控，不做外置；
    - 超限结果外置到 ArtifactStore 并发 artifact.created 事件（与 MCP
      Offload 同一事件形态，Studio 展示与审计一致）。
    """

    #: 引擎内部披露工具：结果视为外部内容（G16 定界），但不参与外置。
    _DISCLOSURE_EXTERNAL = frozenset(
        {"mcp_list_tools", "mcp_read_tool_schema", "mcp_call_tool"}
    )

    def __init__(self, *, engine: Any, run: Any) -> None:
        self._engine = engine
        self._run = run

    def govern(self, name: str, result_text: str) -> tuple[str, dict[str, Any]]:
        run = self._run
        tool = run.tools.get(name)
        policy = policy_for_tool(tool)
        if tool is None:
            # 披露/委托工具不在 run.tools：MCP 披露结果按外部来源定界。
            if name in self._DISCLOSURE_EXTERNAL:
                policy = replace(policy, external=True, offload_to_artifact=False)
            else:
                policy = replace(policy, offload_to_artifact=False)
        store = getattr(self._engine, "_artifact_store", None)
        text, meta = govern_tool_result(
            name=name,
            result_text=result_text,
            policy=policy,
            artifact_store=store,
            run_id=run.handle.run_id,
        )
        if meta.get("offloaded") and store is not None:
            self._emit_artifact_created(name, meta)
        return text, meta

    def _emit_artifact_created(self, name: str, meta: dict[str, Any]) -> None:
        run = self._run
        self._engine._pending_child_events.setdefault(run.handle.run_id, []).append(
            RuntimeEvent.create(
                EventType.ARTIFACT_CREATED,
                agent_id=run.state.agent_id,
                user_id=run.state.user_id,
                session_id=run.state.session_id,
                invocation_id=run.handle.run_id,
                seq_id=0,
                payload={
                    "name": f"tool_result_{name}",
                    "uri": meta.get("artifact_uri") or "",
                    "mime": meta.get("mime") or "text/plain",
                    "content_hash": meta.get("content_hash") or "",
                    "size_bytes": meta.get("size_bytes") or 0,
                    "source": "tool_result",
                },
            )
        )


async def invoke_tool(
    self,
    name: str,
    arguments: dict[str, Any],
    *,
    run: Any = None,
    mcp_cursors: McpDisclosureCursors | None = None,
    call_id: str = "",
) -> Any:
    exclusive = bool(run is not None and run.exclusive_tools)
    if exclusive:
        from ksadk.harness.engine.policy_runtime import revalidate_policy

        # Check before all dispatch branches, including built-in, MCP and
        # dynamically disclosed tools. A model calling a hidden name gets no
        # authority from that name, even if it shadows a trusted host tool.
        await revalidate_policy(self, run)
        if name not in run.tools:
            raise PermissionError("tool is outside the trusted execution policy")
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
    if not exclusive and self._skill_disclosure.is_tool(name):
        return self._invoke_skill_tool(name, arguments, run=run)
    if not exclusive and self._mcp_disclosure.is_tool(name):
        if mcp_cursors is None:
            mcp_cursors = McpDisclosureCursors()
        return await self._invoke_mcp_tool(
            name,
            arguments,
            run=run,
            cursors=mcp_cursors,
            call_id=call_id,
        )
    if (
        not exclusive
        and self._delegation_runtime is not None
        and self._delegation_runtime.is_tool(name)
    ):
        if run is None:
            raise RuntimeError("dynamic delegation requires an active parent run")
        result, child_events = await self._delegation_runtime.invoke(
            engine=self,
            parent_run=run,
            arguments=arguments,
            call_id=call_id or name,
        )
        self._pending_subagent_events.setdefault(run.handle.run_id, {})[call_id or name] = (
            child_events
        )
        return result
    tool = (run.tools if run is not None else self._tools).get(name)
    if tool is None:
        # G24 发现门：未披露即拒绝（不阻断，回填自愈提示给模型）
        raise ToolNotDisclosedError(name)
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
