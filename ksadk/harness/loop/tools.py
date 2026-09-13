"""tool 执行节点（plan §7.1 tool_calls）——引擎无关纯逻辑。

职责：遍历待执行工具调用，对每个：
1. 若在 approval_required 集合，经 :class:`ApprovalResolver` 请求审批（引擎
   负责"如何挂起"——LangGraph 走 interrupt()，单测走同步返回）；
2. 决策非 approved → 记 tool.call.begin/end（拒绝）+ 失败消息 + working_context；
3. approved → 调 :class:`ToolExecutor` 执行，记 begin/end + 结果消息 +
   working_context（成功记 verified_facts，失败记 recent_tool_failures）。

不 import LangGraph。interrupt 抽象为 ``ApprovalResolver``：``request`` 返回
``"approved" | "denied" | ...``，引擎注入"调用 LangGraph interrupt"的实现。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Protocol, Sequence

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.state import WorkingContext
from ksadk.harness.tool_reliability import ToolReliability, classify_tool_reliability
from ksadk.harness.working_context import record_tool_failure, record_tool_result

#: 审批决策值（§11.2）：approved 放行，其余视为拒绝并记录原因。
APPROVED = "approved"


class ApprovalResolver(Protocol):
    """approval 决策来源（引擎负责实际挂起/恢复语义）。

    引擎实现示例（LangGraph）：

        class _GraphApprovalResolver:
            def request(self, call_id, name, arguments) -> str:
                return interrupt(
            {"call_id": call_id, "name": name, "args": arguments, "risk": "high"}
        )

    单测实现：直接返回 ``"approved"`` 或模拟拒绝。
    """

    def request(self, *, call_id: str, name: str, arguments: dict[str, Any]) -> str: ...


#: 按调用参数动态判定是否需审批（P0.1：MCP 按实际目标 Server 风险决策）。
ApprovalDecider = Callable[[str, dict[str, Any]], bool]
ParallelSafeDecider = Callable[[str, dict[str, Any]], bool]
StopOnErrorDecider = Callable[[str, dict[str, Any]], bool]
CancelPendingDecider = Callable[[str, dict[str, Any]], bool]


#: 同步审批解析器（适合 LangGraph interrupt 同步语义）。
SyncApprovalResolver = ApprovalResolver


class ToolExecutor(Protocol):
    """工具执行器（引擎注入：``await tool(args)``）。"""

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class ToolExecutionContext:
    """一次工具调用的稳定运行时身份。

    ``run_id + call_id`` 在审批恢复、Graph 重放和进程重启后保持不变，供
    显式支持幂等的外部 Transport 派生去重键。该上下文不注入模型生成的
    ``arguments``，避免破坏工具 Schema。
    """

    run_id: str
    call_id: str


class ContextualToolExecutor(Protocol):
    """可接收稳定调用身份的工具执行器（旧 ToolExecutor 保持兼容）。"""

    async def execute_with_context(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> Any: ...


#: 工具执行函数类型（可替代 ToolExecutor）。
ToolExecuteFn = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class ToolCallInput:
    """tool_calls 节点的输入。"""

    pending_tool_calls: Sequence[dict[str, Any]]
    approval_required: frozenset[str]
    approval_resolver: ApprovalResolver | None
    tool_executor: ToolExecutor | ToolExecuteFn | None
    #: RuntimeEvent 锚点。
    agent_id: str = ""
    user_id: str = ""
    session_id: str = ""
    run_id: str = ""
    seq_start: int = 0
    #: 进入节点的 working_context（成功/失败都会更新后返回）。
    working_context: WorkingContext | None = None
    #: 统一 CapabilityRuntime（收口 2）：Policy 决策 + Receipt 幂等。
    #: 为 None 时回退 approval_required 静态集合（旧语义）。
    capability_runtime: Any = None
    #: 按调用参数的动态审批决策（P0.1）：对静态集合与 Policy 决策都是
    #: 追加约束——decider 返回 True 则必须审批，即使 Policy 判 allow。
    approval_decider: ApprovalDecider | None = None
    #: 仅当整批调用都被宿主判定为无副作用、无需审批时并行执行。
    #: 默认关闭，避免改变普通 Tool 的既有顺序与 Receipt 语义。
    parallel_safe_decider: ParallelSafeDecider | None = None
    #: 当前调用失败后是否取消同批尚未启动的调用（多 Agent fail-fast）。
    stop_on_error_decider: StopOnErrorDecider | None = None
    #: fail-fast 触发后，仅取消匹配的尚未启动调用；普通工具不应被误取消。
    cancel_pending_decider: CancelPendingDecider | None = None
    #: 调用名 -> 必须先成功的调用名。未满足时该调用不会启动。
    dependencies: dict[str, tuple[str, ...]] = field(default_factory=dict)
    max_parallelism: int = 1
    tenant_id: str = "default"
    live_event_sink: Callable[[RuntimeEvent], RuntimeEvent] | None = None
    succeeded: frozenset[str] = frozenset()
    cancelled_call_ids: frozenset[str] = frozenset()
    policy_agent_id: str | None = None
    control_exceptions: tuple[type[BaseException], ...] = ()


@dataclass
class ToolCallOutput:
    """tool_calls 节点的输出。"""

    events: list[RuntimeEvent] = field(default_factory=list)
    #: 追加到对话的 tool 结果消息（含拒绝/失败的占位消息）。
    new_messages: list[dict[str, Any]] = field(default_factory=list)
    #: 更新后的 working_context（成功记 verified_facts，失败记 failures）。
    working_context: WorkingContext | None = None
    #: 清空后的 pending_tool_calls（节点结束时置空）。
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: 路由：tool_calls 节点结束恒回 reason。
    route: str = "reason"


async def execute_tool_calls(inp: ToolCallInput) -> ToolCallOutput:
    """执行一批工具调用（§7.1 tool_calls 节点）。

    - 统一 CapabilityRuntime（收口 2）：注入时每个调用先做 Receipt 幂等回放，
      再经 ToolPolicy 决策（deny / require_approval / allow）；审批仍经
      ``approval_resolver``（引擎挂起语义）。
    - approval 通道（旧语义）：capability_runtime 为 None 时，approval_required
      集合中的工具经 ``approval_resolver`` 请求；resolver 为 None 时按已批准
      处理（调试/无审批场景）。
    - 失败韧性（§7.2）：单工具抛错不终止 Run，记 error 事件 + 失败消息后继续。
    - asyncio.CancelledError 原样抛出（cancel 语义，由引擎处理）。
    """
    if _parallel_batch_allowed(inp):
        return await _execute_parallel_batch(inp)

    out = ToolCallOutput(working_context=inp.working_context)
    seq = inp.seq_start
    runtime = inp.capability_runtime
    succeeded: set[str] = set(inp.succeeded)
    cancelled_call_ids: set[str] = set(inp.cancelled_call_ids)

    for pending_index, pending in enumerate(inp.pending_tool_calls):
        call_id, name = pending["call_id"], pending["name"]
        arguments = pending["arguments"]
        reliability = _resolve_reliability(inp, name, arguments)
        reliability_payload = reliability.to_event_payload()
        if str(call_id) in cancelled_call_ids:
            seq += 1
            out.events.append(
                _event(
                    EventType.TOOL_CALL_BEGIN,
                    inp,
                    seq,
                    {
                        "call_id": call_id,
                        "name": name,
                        "args": arguments,
                        "skipped": True,
                        "reliability": reliability_payload,
                    },
                )
            )
            seq += 1
            out.events.append(
                _event(
                    EventType.TOOL_CALL_END,
                    inp,
                    seq,
                    {
                        "call_id": call_id,
                        "name": name,
                        "error": "cancelled by fail-fast sibling failure",
                        "error_category": "cancelled_by_fail_fast",
                        "receipt_committed": False,
                        "skipped": True,
                        "reliability": reliability_payload,
                    },
                )
            )
            out.new_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": "[error] cancelled by fail-fast sibling failure",
                }
            )
            continue
        missing_dependencies = [
            dependency
            for dependency in inp.dependencies.get(name, ())
            if dependency not in succeeded
        ]
        if missing_dependencies:
            seq += 1
            out.events.append(
                _event(
                    EventType.TOOL_CALL_BEGIN,
                    inp,
                    seq,
                    {
                        "call_id": call_id,
                        "name": name,
                        "args": arguments,
                        "skipped": True,
                        "reliability": reliability_payload,
                    },
                )
            )
            seq += 1
            error = f"unsatisfied dependencies: {', '.join(missing_dependencies)}"
            out.events.append(
                _event(
                    EventType.TOOL_CALL_END,
                    inp,
                    seq,
                    {
                        "call_id": call_id,
                        "name": name,
                        "error": error,
                        "error_category": "dependency_unsatisfied",
                        "receipt_committed": False,
                        "skipped": True,
                        "reliability": reliability_payload,
                    },
                )
            )
            out.new_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": f"[error] {error}",
                }
            )
            continue

        # 1. Receipt 幂等回放：审批恢复重放节点时不再重复触发副作用。
        prior = runtime.check_receipt(inp.run_id, call_id) if runtime is not None else None
        if prior is not None and prior.status in {"executed", "skipped"}:
            executed = prior.status == "executed"
            result_text = (
                prior.result_digest or "[idempotent replay: no result recorded]"
                if executed
                else "[denied] prior approval decision replayed"
            )
            seq += 1
            out.events.append(
                _event(
                    EventType.TOOL_CALL_BEGIN,
                    inp,
                    seq,
                    {
                        "call_id": call_id,
                        "name": name,
                        "args": arguments,
                        "reliability": reliability_payload,
                    },
                )
            )
            seq += 1
            out.events.append(
                _event(
                    EventType.TOOL_CALL_END,
                    inp,
                    seq,
                    {
                        "call_id": call_id,
                        "name": name,
                        **({"result": result_text} if executed else {"error": "approval denied"}),
                        "replayed": True,
                        "receipt_committed": True,
                        "reliability": reliability_payload,
                    },
                )
            )
            out.new_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": result_text,
                }
            )
            if executed:
                succeeded.add(name)
            continue

        # 2. 决策：统一 Policy 优先；未注入时回退静态 approval_required 集合。
        decision = APPROVED
        dynamic_requires = inp.approval_decider is not None and inp.approval_decider(
            name, arguments
        )
        if runtime is not None:
            policy_decision = runtime.decide(
                tenant_id=inp.tenant_id,
                user_id=inp.user_id,
                agent_id=inp.policy_agent_id or inp.agent_id,
                tool_name=name,
                arguments=arguments,
            )
            if policy_decision.action == "deny":
                decision = f"policy-denied: {policy_decision.reason}"
            elif (
                policy_decision.action == "require_approval"
                or dynamic_requires
                or name in inp.approval_required
            ):
                if inp.approval_resolver is not None:
                    decision = inp.approval_resolver.request(
                        call_id=call_id, name=name, arguments=arguments
                    )
                else:
                    decision = f"policy-denied: approval required ({policy_decision.reason})"
        elif name in inp.approval_required or dynamic_requires:
            decision = (
                inp.approval_resolver.request(call_id=call_id, name=name, arguments=arguments)
                if inp.approval_resolver is not None
                else "approval resolver unavailable"
            )

        if decision != APPROVED:
            receipt_committed = runtime is not None and runtime.receipt_enabled
            seq += 1
            out.events.append(
                _event(
                    EventType.TOOL_CALL_BEGIN,
                    inp,
                    seq,
                    {
                        "call_id": call_id,
                        "name": name,
                        "args": arguments,
                        "reliability": reliability_payload,
                    },
                )
            )
            seq += 1
            out.events.append(
                _event(
                    EventType.TOOL_CALL_END,
                    inp,
                    seq,
                    {
                        "call_id": call_id,
                        "name": name,
                        "error": f"approval {decision}",
                        "receipt_committed": receipt_committed,
                        "reliability": reliability_payload,
                    },
                )
            )
            out.new_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": f"[denied] approval decision: {decision}",
                }
            )
            if runtime is not None:
                runtime.record_receipt(
                    invocation_id=inp.run_id,
                    call_id=call_id,
                    tool_name=name,
                    arguments=arguments,
                    decision="denied",
                    status="skipped",
                )
            if out.working_context is not None:
                out.working_context = record_tool_failure(
                    out.working_context, name=name, error=f"approval {decision}"
                )
            continue

        seq += 1
        out.events.append(
            _event(
                EventType.TOOL_CALL_BEGIN,
                inp,
                seq,
                {
                    "call_id": call_id,
                    "name": name,
                    "args": arguments,
                    "reliability": reliability_payload,
                },
            )
        )
        try:
            result = await _invoke(
                inp.tool_executor,
                name,
                arguments,
                context=ToolExecutionContext(run_id=inp.run_id, call_id=call_id),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单工具失败不终止 Run
            if isinstance(exc, inp.control_exceptions):
                raise
            category = str(getattr(exc, "category", "") or "")
            seq += 1
            out.events.append(
                _event(
                    EventType.TOOL_CALL_END,
                    inp,
                    seq,
                    {
                        "call_id": call_id,
                        "name": name,
                        "error": f"{type(exc).__name__}: {exc}",
                        **({"error_category": category} if category else {}),
                        "receipt_committed": False,
                        "reliability": reliability_payload,
                    },
                )
            )
            out.new_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": f"[error] {type(exc).__name__}: {exc}",
                }
            )
            if out.working_context is not None:
                out.working_context = record_tool_failure(
                    out.working_context, name=name, error=f"{type(exc).__name__}: {exc}"
                )
            if inp.stop_on_error_decider is not None and inp.stop_on_error_decider(name, arguments):
                for skipped in inp.pending_tool_calls[pending_index + 1 :]:
                    skipped_call_id = str(skipped["call_id"])
                    skipped_name = str(skipped["name"])
                    skipped_arguments = skipped["arguments"]
                    if inp.cancel_pending_decider is not None and not inp.cancel_pending_decider(
                        skipped_name, skipped_arguments
                    ):
                        continue
                    cancelled_call_ids.add(skipped_call_id)
            continue

        result_text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        if runtime is not None:
            runtime.record_receipt(
                invocation_id=inp.run_id,
                call_id=call_id,
                tool_name=name,
                arguments=arguments,
                decision="approved",
                status="executed",
                result_digest=result_text[:65_536],
            )
        if out.working_context is not None:
            out.working_context = record_tool_result(
                out.working_context, name=name, result_text=result_text
            )
        seq += 1
        out.events.append(
            _event(
                EventType.TOOL_CALL_END,
                inp,
                seq,
                {
                    "call_id": call_id,
                    "name": name,
                    "result": result,
                    "receipt_committed": bool(runtime is not None and runtime.receipt_enabled),
                    "reliability": reliability_payload,
                },
            )
        )
        out.new_messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": result_text,
            }
        )
        succeeded.add(name)

    out.pending_tool_calls = []
    return out


def _parallel_batch_allowed(inp: ToolCallInput) -> bool:
    """只为明确标记的无审批、无 Receipt 批次启用并行。"""
    if (
        len(inp.pending_tool_calls) < 2
        or inp.max_parallelism < 2
        or inp.parallel_safe_decider is None
        or inp.capability_runtime is not None
        or bool(inp.dependencies)
    ):
        return False
    for pending in inp.pending_tool_calls:
        name = pending["name"]
        arguments = pending["arguments"]
        if name in inp.approval_required:
            return False
        if inp.approval_decider is not None and inp.approval_decider(name, arguments):
            return False
        if not inp.parallel_safe_decider(name, arguments):
            return False
    return True


async def _execute_parallel_batch(inp: ToolCallInput) -> ToolCallOutput:
    """Stream explicitly safe calls as they execute; retain input-order results."""
    semaphore = asyncio.Semaphore(inp.max_parallelism)

    async def invoke_one(pending):
        async with semaphore:

            def live(event):
                event = event.model_copy(update={"payload": {**event.payload, "parallel": True}})
                return inp.live_event_sink(event) if inp.live_event_sink else event

            return await execute_tool_calls(
                replace(
                    inp,
                    pending_tool_calls=(pending,),
                    parallel_safe_decider=None,
                    working_context=None,
                    seq_start=0,
                    live_event_sink=live,
                )
            )

    results = await asyncio.gather(*(invoke_one(pending) for pending in inp.pending_tool_calls))
    out = ToolCallOutput(working_context=inp.working_context)
    seq = inp.seq_start
    for result in results:
        for event in result.events:
            if inp.live_event_sink is None:
                seq += 1
                event = event.model_copy(update={"seq_id": seq})
            out.events.append(event)
        out.new_messages.extend(result.new_messages)
        if out.working_context is not None:
            for event in result.events:
                if event.event_type != EventType.TOOL_CALL_END:
                    continue
                if event.payload.get("error"):
                    out.working_context = record_tool_failure(
                        out.working_context,
                        name=event.payload["name"],
                        error=event.payload["error"],
                    )
                else:
                    out.working_context = record_tool_result(
                        out.working_context,
                        name=event.payload["name"],
                        result_text=json.dumps(event.payload.get("result"), ensure_ascii=False),
                    )
    return out


def _resolve_reliability(
    inp: ToolCallInput,
    name: str,
    arguments: dict[str, Any],
) -> ToolReliability:
    runtime = inp.capability_runtime
    receipt_enabled = bool(runtime is not None and runtime.receipt_enabled)
    resolver = getattr(inp.tool_executor, "reliability", None)
    if callable(resolver):
        return resolver(name, arguments, receipt_enabled=receipt_enabled)
    if runtime is not None:
        return runtime.reliability(name)
    return classify_tool_reliability(side_effect="unknown")


async def _invoke(
    executor: ToolExecutor | ToolExecuteFn | None,
    name: str,
    arguments: dict[str, Any],
    *,
    context: ToolExecutionContext,
) -> Any:
    if executor is None:
        raise RuntimeError(
            f"engine tool {name!r} is not available; it may be filtered or unpublished"
        )
    execute_with_context = getattr(executor, "execute_with_context", None)
    if callable(execute_with_context):
        return await execute_with_context(name, arguments, context)
    if hasattr(executor, "execute"):
        return await executor.execute(name, arguments)  # type: ignore[union-attr]
    return await executor(name, arguments)  # ToolExecuteFn


def _event(
    event_type: str,
    inp: ToolCallInput,
    seq_id: int,
    payload: dict[str, Any],
    *,
    phase: str | None = None,
) -> RuntimeEvent:
    event = RuntimeEvent.create(
        event_type,
        agent_id=inp.agent_id,
        user_id=inp.user_id,
        session_id=inp.session_id,
        invocation_id=inp.run_id,
        seq_id=seq_id,
        payload=payload,
        phase=phase,
    )
    return inp.live_event_sink(event) if inp.live_event_sink is not None else event


__all__ = [
    "APPROVED",
    "ApprovalResolver",
    "ContextualToolExecutor",
    "ToolCallInput",
    "ToolCallOutput",
    "ToolExecuteFn",
    "ToolExecutor",
    "ToolExecutionContext",
    "ParallelSafeDecider",
    "execute_tool_calls",
]
