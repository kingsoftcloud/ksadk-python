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
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, Sequence

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.state import WorkingContext
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


#: 同步审批解析器（适合 LangGraph interrupt 同步语义）。
SyncApprovalResolver = ApprovalResolver


class ToolExecutor(Protocol):
    """工具执行器（引擎注入：``await tool(args)``）。"""

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any: ...


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
    tenant_id: str = "default"


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
    out = ToolCallOutput(working_context=inp.working_context)
    seq = inp.seq_start
    runtime = inp.capability_runtime

    for pending in inp.pending_tool_calls:
        call_id, name = pending["call_id"], pending["name"]
        arguments = pending["arguments"]

        # 1. Receipt 幂等回放：审批恢复重放节点时不再重复触发副作用。
        prior = runtime.check_receipt(inp.run_id, call_id) if runtime is not None else None
        if prior is not None and prior.status == "executed":
            result_text = prior.result_digest or "[idempotent replay: no result recorded]"
            seq += 1
            out.events.append(
                _event(
                    EventType.TOOL_CALL_BEGIN,
                    inp,
                    seq,
                    {"call_id": call_id, "name": name, "args": arguments},
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
                        "result": result_text,
                        "replayed": True,
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
            continue

        # 2. 决策：统一 Policy 优先；未注入时回退静态 approval_required 集合。
        decision = APPROVED
        if runtime is not None:
            policy_decision = runtime.decide(
                tenant_id=inp.tenant_id,
                user_id=inp.user_id,
                agent_id=inp.agent_id,
                tool_name=name,
                arguments=arguments,
            )
            if policy_decision.action == "deny":
                decision = f"policy-denied: {policy_decision.reason}"
            elif policy_decision.action == "require_approval":
                if inp.approval_resolver is not None:
                    decision = inp.approval_resolver.request(
                        call_id=call_id, name=name, arguments=arguments
                    )
                else:
                    decision = f"policy-denied: approval required ({policy_decision.reason})"
        elif name in inp.approval_required and inp.approval_resolver is not None:
            decision = inp.approval_resolver.request(
                call_id=call_id, name=name, arguments=arguments
            )

        if decision != APPROVED:
            seq += 1
            out.events.append(
                _event(
                    EventType.TOOL_CALL_BEGIN,
                    inp,
                    seq,
                    {"call_id": call_id, "name": name, "args": arguments},
                )
            )
            seq += 1
            out.events.append(
                _event(
                    EventType.TOOL_CALL_END,
                    inp,
                    seq,
                    {"call_id": call_id, "name": name, "error": f"approval {decision}"},
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
                {"call_id": call_id, "name": name, "args": arguments},
            )
        )
        try:
            result = await _invoke(inp.tool_executor, name, arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单工具失败不终止 Run
            seq += 1
            out.events.append(
                _event(
                    EventType.TOOL_CALL_END,
                    inp,
                    seq,
                    {"call_id": call_id, "name": name, "error": f"{type(exc).__name__}: {exc}"},
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
                {"call_id": call_id, "name": name, "result": result},
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

    out.pending_tool_calls = []
    return out


async def _invoke(
    executor: ToolExecutor | ToolExecuteFn | None,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    if executor is None:
        raise RuntimeError(
            f"engine tool {name!r} is not available; it may be filtered or unpublished"
        )
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
    return RuntimeEvent.create(
        event_type,
        agent_id=inp.agent_id,
        user_id=inp.user_id,
        session_id=inp.session_id,
        invocation_id=inp.run_id,
        seq_id=seq_id,
        payload=payload,
        phase=phase,
    )


__all__ = [
    "APPROVED",
    "ApprovalResolver",
    "ToolCallInput",
    "ToolCallOutput",
    "ToolExecuteFn",
    "ToolExecutor",
    "execute_tool_calls",
]
