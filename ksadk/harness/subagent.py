"""子 Agent 执行（收口 6 / plan §14）：多 Agent 作为可选能力。

Engine 支持 ``sub_agents``：工具名 → 子 Agent 配置（instructions + 可选
tools）。父 Agent 的模型像调用普通工具一样发起子任务；引擎在 tool_calls
节点内联运行一个子 ManagedLangGraphEngine 到完成，子事件（含
agent.started/completed、自己的 turn/node/model/usage）以**子 agent_id**
写入父事件流——事件树（event_tree）据此把子 Agent 展示为独立树节点。

约束：
- 子 Agent 不再嵌套子 Agent（单层，避免失控递归）；
- 子 Agent 共享父 Reasoner 与工具注册表（数据面由宿主装配）；
- 每次委派使用独立 Checkpoint namespace、轮数预算和墙钟超时；
- Skill 默认继承，MCP 必须显式授权后才继承；
- 子 Agent 结果以其最终文本作为工具结果回流父对话。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from typing import Any

from jsonschema import ValidationError, validate
from pydantic import BaseModel, ConfigDict, Field

from ksadk.harness.events import RuntimeEvent
from ksadk.harness.spec import (
    CapabilityBindings,
    ExecutionStrategySpec,
    HarnessSpec,
    PromptSpec,
    SubAgentBinding,
)
from ksadk.runtime import ResumeTarget, RunHandle, StartRequest


@dataclass
class SubAgentSpec:
    """子 Agent 配置（宿主声明，引擎内联执行）。"""

    name: str
    instructions: str
    #: 允许子 Agent 使用的工具名（空 = 不用工具，只做文本推理）。
    tools: tuple[str, ...] = ()
    description: str = ""
    timeout_seconds: float = 120.0
    max_turns: int = 4
    max_total_tokens: int | None = None
    max_artifacts: int | None = None
    max_tool_calls: int | None = None
    output_schema: dict[str, Any] | None = None
    depends_on: tuple[str, ...] = ()
    failure_policy: str = "propagate"
    max_retries: int = 0
    inherit_skills: bool = True
    inherit_mcp: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_binding(cls, binding: SubAgentBinding) -> "SubAgentSpec":
        """把不可变 Revision 声明投影为本次 Run 的执行配置。"""
        return cls(**binding.model_dump())

    @property
    def openai_schema(self) -> dict[str, Any]:
        """模型可见的工具 schema（子 Agent 即工具，任务描述入参）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description or f"委托子 Agent {self.name} 完成任务并返回其结论",
                "parameters": {
                    "type": "object",
                    "properties": {"task": {"type": "string", "description": "要完成的子任务描述"}},
                    "required": ["task"],
                },
            },
        }


class SubAgentResult(BaseModel):
    """父 Agent 可消费、平台可审计的稳定子任务结果。"""

    model_config = ConfigDict(extra="allow")

    status: str
    output: Any = None
    artifact_refs: tuple[str, ...] = ()
    usage: dict[str, int] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    error_category: str | None = None
    error: str | None = None


class SubAgentOutputValidationError(ValueError):
    """子任务最终输出不满足 Revision 声明的 Schema。"""


def validate_subagent_output(text: str, schema: dict[str, Any] | None) -> Any:
    """按声明解析并校验最终输出；无 Schema 时保持原文本。"""
    if schema is None:
        return text
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SubAgentOutputValidationError(
            f"sub-agent output_schema requires JSON output: {exc.msg}"
        ) from exc
    try:
        validate(instance=value, schema=schema)
    except ValidationError as exc:
        raise SubAgentOutputValidationError(
            f"sub-agent output_schema validation failed: {exc.message}"
        ) from exc
    return value


def order_subagent_tool_calls(
    calls: list[dict[str, Any]], specs: dict[str, "SubAgentSpec"]
) -> list[dict[str, Any]]:
    """对同批子任务稳定拓扑排序；普通工具与无依赖子任务保持原顺序。"""
    positions = {str(call.get("name") or ""): index for index, call in enumerate(calls)}
    call_names = set(positions)
    dependencies = {
        name: {dependency for dependency in spec.depends_on if dependency in call_names}
        for name, spec in specs.items()
        if name in call_names
    }

    def rank(name: str, trail: frozenset[str] = frozenset()) -> int:
        if name in trail:
            raise SubAgentExecutionError("dependency_cycle", f"sub-agent dependency cycle: {name}")
        parents = dependencies.get(name, set())
        if not parents:
            return 0
        return 1 + max(rank(parent, trail | {name}) for parent in parents)

    return sorted(
        calls,
        key=lambda call: (
            rank(str(call.get("name") or "")),
            positions[str(call.get("name") or "")],
        ),
    )


def child_spec(parent_spec: HarnessSpec, sub: SubAgentSpec) -> HarnessSpec:
    """由父 Spec 派生子 Spec（同模型绑定，独立指令）。"""
    # Skill 是只读知识能力，默认继承；MCP 可能触达外部系统，只有 Revision
    # 明确授权时才继承，防止委派扩大权限面。
    return HarnessSpec(
        agent_revision_ref=parent_spec.agent_revision_ref,
        model=parent_spec.model,
        prompt=PromptSpec(instructions=sub.instructions),
        capabilities=CapabilityBindings(
            skill_bindings=(
                parent_spec.capabilities.skill_bindings if sub.inherit_skills else ()
            ),
            mcp_bindings=(parent_spec.capabilities.mcp_bindings if sub.inherit_mcp else ()),
        ),
        context_policy=parent_spec.context_policy,
        memory_policy=parent_spec.memory_policy,
        approval_policy=parent_spec.approval_policy,
        sandbox_policy=parent_spec.sandbox_policy,
        observability_policy=parent_spec.observability_policy,
        execution_strategy=ExecutionStrategySpec(
            config={
                "max_tool_calls": sub.max_tool_calls,
                "max_artifacts": sub.max_artifacts,
                "max_total_tokens": sub.max_total_tokens,
            }
        ),
    )


async def run_subagent(
    *,
    engine: Any,
    parent_run: Any,
    sub: SubAgentSpec,
    task: str,
    call_id: str = "",
) -> tuple[dict[str, Any], list[RuntimeEvent]]:
    """运行子 Agent 到完成，返回 (最终文本, 以父 seq 重排的子事件)。

    子事件信封：invocation_id 同父 Run（单一审计流），agent_id 为
    ``{父agent_id}:{子名}``（事件树按 agent 分组展示子 Agent）。
    """
    from ksadk.harness.engine.base import ExecutionEngineError
    from ksadk.harness.engine.langgraph import ManagedLangGraphEngine

    parent_spec = getattr(engine, "_current_spec", None)
    if parent_spec is None:
        raise RuntimeError("子 Agent 需要父引擎先 compile（无当前 Spec）")
    # 零预算必须在启动子 Run、调用模型或工具之前拒绝，避免“先产生副作用再
    # 报超额”。正预算由子引擎执行期守卫继续逐次扣减。
    if sub.max_total_tokens == 0:
        raise SubAgentExecutionError("budget_exhausted", "sub-agent token budget is 0")
    if sub.max_artifacts == 0 and sub.meta.get("requires_artifacts"):
        raise SubAgentExecutionError("budget_exhausted", "sub-agent artifact budget is 0")
    artifact_store = getattr(engine._mcp_disclosure, "_artifact_store", None)
    child_run_id = f"{parent_run.handle.run_id}:sub:{sub.name}:{call_id or 'delegation'}"
    if artifact_store is not None and sub.max_artifacts is not None:
        from ksadk.harness.artifact_store import BudgetedArtifactStore

        artifact_store = BudgetedArtifactStore(
            artifact_store, run_id=child_run_id, max_artifacts=sub.max_artifacts
        )
    child_engine = ManagedLangGraphEngine(
        reasoner=engine._reasoner,
        # 复用持久化后端、使用独立 thread_id；因此可独立恢复但不会污染父图。
        checkpointer=engine._checkpointer,
        tools={name: engine._tools[name] for name in sub.tools if name in engine._tools},
        skill_runtime=getattr(engine, "_skill_runtime", None),
        mcp_runtime=(getattr(engine, "_mcp_runtime", None) if sub.inherit_mcp else None),
        artifact_store=artifact_store,
        max_reasoning_turns=sub.max_turns,
    )
    compiled = await child_engine.compile(child_spec(parent_spec, sub))
    child_agent_id = f"{parent_run.state.agent_id}:{sub.name}"
    checkpoint_session_id = (
        f"{parent_run.state.session_id}:sub:{sub.name}:{call_id or 'delegation'}"
    )
    child_request = StartRequest(
            input=task,
            user_id=parent_run.state.user_id,
            # 事件信封沿用父 session，保持单一 invocation 的 Conformance；
            # Checkpoint 另用私有 namespace，避免同名子 Agent 并发覆盖。
            session_id=parent_run.state.session_id,
            agent_id=child_agent_id,
            runtime_type="managed-langgraph",
            metadata={
                "invocation_id": child_run_id,
                "parent_run_id": parent_run.handle.run_id,
                "checkpoint_session_id": checkpoint_session_id,
            },
    )
    handle = None
    recovered_from_checkpoint = False
    if engine._checkpointer is not None:
        from ksadk.harness.engine.thread_ids import encode_thread_id

        candidate = RunHandle(
            run_id=child_run_id,
            session_id=parent_run.state.session_id,
            runtime_type="managed-langgraph",
            native_ref={
                "thread_id": encode_thread_id(
                    tenant_id=engine._tenant_id,
                    user_id=parent_run.state.user_id,
                    agent_id=child_agent_id,
                    session_id=checkpoint_session_id,
                    run_id=child_run_id,
                ),
                "user_id": parent_run.state.user_id,
                "agent_id": child_agent_id,
            },
        )
        try:
            handle = await child_engine.attach(candidate, compiled)
            recovered_from_checkpoint = True
            child_run = child_engine._runs[handle.run_id]
            if child_run.state.status.value == "paused":
                await child_engine.resume(
                    handle,
                    ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
                    None,
                )
            elif child_run.state.status.value == "awaiting_approval":
                raise SubAgentExecutionError(
                    "awaiting_approval",
                    f"sub-agent {sub.name!r} recovery requires approval decision",
                )
        except SubAgentExecutionError:
            raise
        except ExecutionEngineError as exc:
            if "无未决 Checkpoint" not in str(exc):
                raise
            # 没有未决 checkpoint 才创建新子 Run；确定性 child_run_id 使进程
            # 重启后能命中同一 Checkpoint，而不是重复启动副作用。
            handle = None
    if handle is None:
        handle = await child_engine.start(child_request, compiled)
    active_key = call_id or f"{sub.name}:{handle.run_id}"
    engine._active_subagent_runs.setdefault(parent_run.handle.run_id, {})[active_key] = (
        child_engine,
        handle,
    )
    final_text = ""
    collected: list[RuntimeEvent] = []
    failure = ""
    failure_category = ""
    completed = False
    total_tokens = 0
    artifact_count = 0
    tool_call_count = 0
    artifact_refs: list[str] = []
    try:
        async with asyncio.timeout(sub.timeout_seconds):
            async for event in child_engine.stream(handle):
                if event.event_type == "run.failed":
                    failure = str(event.payload.get("error") or "child run failed")
                    failure_category = str(
                        event.payload.get("error_category") or failure_category
                    )
                # 子 Run 生命周期不并入父流；agent 区间表达子任务边界。
                if event.event_type.startswith("run."):
                    continue
                collected.append(event)
                if event.event_type == "usage.reported":
                    total_tokens += int(event.payload.get("input_tokens") or 0)
                    total_tokens += int(event.payload.get("output_tokens") or 0)
                    if (
                        sub.max_total_tokens is not None
                        and total_tokens > sub.max_total_tokens
                    ):
                        await child_engine.cancel(handle)
                        failure_category = "budget_exhausted"
                        failure = (
                            f"sub-agent {sub.name!r} exceeded token budget "
                            f"{sub.max_total_tokens}"
                        )
                        break
                if event.event_type == "artifact.created":
                    artifact_count += 1
                    artifact_ref = str(
                        event.payload.get("artifact_uri")
                        or event.payload.get("artifact_ref")
                        or ""
                    )
                    if artifact_ref:
                        artifact_refs.append(artifact_ref)
                    if sub.max_artifacts is not None and artifact_count > sub.max_artifacts:
                        await child_engine.cancel(handle)
                        failure_category = "budget_exhausted"
                        failure = (
                            f"sub-agent {sub.name!r} exceeded artifact budget "
                            f"{sub.max_artifacts}"
                        )
                        break
                if event.event_type == "tool.call.end":
                    tool_call_count += 1
                    if event.payload.get("error_category") == "budget_exhausted":
                        await child_engine.cancel(handle)
                        failure_category = "budget_exhausted"
                        failure = str(
                            event.payload.get("error")
                            or f"sub-agent {sub.name!r} budget exhausted"
                        )
                        break
                    if (
                        sub.max_tool_calls is not None
                        and tool_call_count > sub.max_tool_calls
                    ):
                        await child_engine.cancel(handle)
                        failure_category = "budget_exhausted"
                        failure = (
                            f"sub-agent {sub.name!r} exceeded tool-call budget "
                            f"{sub.max_tool_calls}"
                        )
                        break
                if event.event_type == "agent.completed":
                    completed = str(event.payload.get("status") or "") == "completed"
                if event.event_type == "text.completed" and event.phase == "final_answer":
                    final_text = str(event.payload.get("text") or "")
    except TimeoutError:
        failure_category = "timeout"
        failure = f"sub-agent {sub.name!r} timed out after {sub.timeout_seconds:g}s"
    finally:
        active = engine._active_subagent_runs.get(parent_run.handle.run_id, {})
        active.pop(active_key, None)
        if not active:
            engine._active_subagent_runs.pop(parent_run.handle.run_id, None)
    # Engine 会把 CancelledError 归一化为取消状态；因此 asyncio.timeout 的
    # 取消可能被子引擎消费。缺失 agent.completed 仍必须按超时失败处理。
    if not completed and not failure:
        failure_category = "timeout"
        failure = f"sub-agent {sub.name!r} timed out after {sub.timeout_seconds:g}s"
    if (
        failure
        and sub.failure_policy == "retry"
        and sub.max_retries > 0
        and (failure_category or "child_failed") in {"child_failed", "timeout"}
    ):
        retry_result, retry_events = await run_subagent(
            engine=engine,
            parent_run=parent_run,
            sub=replace(sub, max_retries=sub.max_retries - 1),
            task=task,
            call_id=f"{call_id or sub.name}:retry-{sub.max_retries}",
        )
        return retry_result, [*collected, *retry_events]
    if failure:
        if sub.failure_policy == "return_error":
            return SubAgentResult(
                status="failed",
                error_category=failure_category or "child_failed",
                error=failure,
                artifact_refs=tuple(artifact_refs),
                usage={
                    "input_tokens": sum(
                        int(e.payload.get("input_tokens") or 0)
                        for e in collected
                        if e.event_type == "usage.reported"
                    ),
                    "output_tokens": sum(
                        int(e.payload.get("output_tokens") or 0)
                        for e in collected
                        if e.event_type == "usage.reported"
                    ),
                    "total_tokens": total_tokens,
                },
                evidence={
                    "child_agent_id": child_agent_id,
                    "child_run_id": handle.run_id,
                    "recovered_from_checkpoint": recovered_from_checkpoint,
                },
            ).model_dump(mode="json"), collected
        raise SubAgentExecutionError(failure_category or "child_failed", failure)
    try:
        output = validate_subagent_output(
            final_text or "（子 Agent 未产出结果）", sub.output_schema
        )
    except SubAgentOutputValidationError as exc:
        raise SubAgentExecutionError("output_validation_failed", str(exc)) from exc
    input_tokens = sum(
        int(e.payload.get("input_tokens") or 0)
        for e in collected
        if e.event_type == "usage.reported"
    )
    output_tokens = sum(
        int(e.payload.get("output_tokens") or 0)
        for e in collected
        if e.event_type == "usage.reported"
    )
    return SubAgentResult(
        status="completed",
        output=output,
        artifact_refs=tuple(artifact_refs),
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        evidence={
            "child_agent_id": child_agent_id,
            "child_run_id": handle.run_id,
            "recovered_from_checkpoint": recovered_from_checkpoint,
        },
    ).model_dump(mode="json"), collected


class SubAgentExecutionError(RuntimeError):
    """Classified child failure safe to project into Tool events."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def resequence_child_events(parent_run: Any, events: list[RuntimeEvent]) -> None:
    """把缓冲的子事件按父 seq 重排后并入父流（tool.call 事件之后统一落盘）。"""
    for ev in events:
        parent_run.seq += 1
        payload = dict(ev.payload)
        if ev.event_type in {"agent.started", "agent.completed"}:
            payload.setdefault("parent_agent_id", parent_run.state.agent_id)
        parent_run.events.append(
            RuntimeEvent.create(
                ev.event_type,
                agent_id=ev.agent_id,
                user_id=ev.user_id,
                session_id=ev.session_id,
                # v1 invocation_id 保持父审计流兼容；v2 run_id 保留独立子 Run。
                invocation_id=parent_run.handle.run_id,
                seq_id=parent_run.seq,
                payload=payload,
                phase=ev.phase,
                run_id=ev.run_id or ev.invocation_id,
                scope_id=ev.scope_id or f"agent:{ev.agent_id}",
                parent_scope_id=ev.parent_scope_id or f"agent:{parent_run.state.agent_id}",
                parent_run_id=ev.parent_run_id or parent_run.handle.run_id,
            )
        )


__all__ = [
    "SubAgentOutputValidationError",
    "SubAgentResult",
    "SubAgentSpec",
    "child_spec",
    "order_subagent_tool_calls",
    "resequence_child_events",
    "run_subagent",
    "validate_subagent_output",
]
