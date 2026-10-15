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
from ksadk.harness.hooks import RunContext
from ksadk.harness.state import WorkingContext
from ksadk.harness.tool_output import PublicOutputBuffer, tool_output_scope
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
ArgumentValidator = Callable[[str, dict[str, Any]], str | None]


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
    authorization_agent_id: str | None = None
    succeeded_tools: frozenset[str] = frozenset()
    prior_failure: bool = False
    live_event_sink: Callable[[RuntimeEvent], None] | None = None
    #: Validate model-generated arguments before policy/approval.  Returning a
    #: message records a normal tool error for the model to repair, without
    #: asking a human to approve an invocation that cannot execute.
    argument_validator: ArgumentValidator | None = None
    #: 中央结果治理（G10/G16）：``govern(name, raw_text) -> (context_text, meta)``。
    #: 为 None 时结果原样透传（旧语义/纯单测路径）。
    result_governor: Any = None
    #: G5 §5.2 on_tool_result 钩子：``hook(run_ctx, call, result) -> dict | None``。
    #: 由引擎构造（内部按注册顺序跑钩子链，降级走引擎缓冲）；返回改写后的
    #: result dict（带新 content）表示改写生效，返回 None 表示未改写/未启用。
    tool_result_hook: Any = None


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
    succeeded: set[str] = set(inp.succeeded_tools)
    cancelled_call_ids: set[str] = {
        str(p["call_id"]) for p in inp.pending_tool_calls
        if inp.prior_failure and inp.cancel_pending_decider is not None
        and inp.cancel_pending_decider(p["name"], p["arguments"])
    }

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
                    {"call_id": call_id, "name": name, "args": arguments, "skipped": True,
                     "reliability": reliability_payload},
                )
            )
            seq += 1
            out.events.append(
                _event(
                    EventType.TOOL_CALL_END,
                    inp,
                    seq,
                    {"call_id": call_id, "name": name,
                     "error": "cancelled by fail-fast sibling failure",
                     "error_category": "cancelled_by_fail_fast",
                     "receipt_committed": False, "skipped": True,
                     "reliability": reliability_payload},
                )
            )
            out.new_messages.append(
                {"role": "tool", "tool_call_id": call_id, "name": name,
                 "content": "[error] cancelled by fail-fast sibling failure"}
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

        validation_error = (
            # 参数本身无法解析（模型输出损坏/截断）时，直接走 invalid_arguments
            # 回灌路径，不执行、也不升级为整轮失败。
            str(pending.get("invalid_reason") or "").strip() or None
            if pending.get("invalid_reason")
            else (
                inp.argument_validator(name, arguments)
                if inp.argument_validator is not None
                else None
            )
        )
        if validation_error:
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
                        "error": validation_error,
                        "error_category": "invalid_arguments",
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
                    "content": f"[error] invalid tool arguments: {validation_error}",
                }
            )
            if out.working_context is not None:
                out.working_context = record_tool_failure(
                    out.working_context,
                    name=name,
                    error=f"invalid arguments: {validation_error}",
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
        dynamic_requires = (
            inp.approval_decider is not None and inp.approval_decider(name, arguments)
        )
        if runtime is not None:
            policy_decision = runtime.decide(
                tenant_id=inp.tenant_id,
                user_id=inp.user_id,
                agent_id=inp.authorization_agent_id or inp.agent_id,
                tool_name=name,
                arguments=arguments,
            )
            if policy_decision.action == "deny":
                decision = f"policy-denied: {policy_decision.reason}"
            elif (
                policy_decision.action == "require_approval"
                or dynamic_requires or name in inp.approval_required
            ):
                if inp.approval_resolver is not None:
                    decision = inp.approval_resolver.request(
                        call_id=call_id, name=name, arguments=arguments
                    )
                else:
                    decision = f"policy-denied: approval required ({policy_decision.reason})"
        elif (
            name in inp.approval_required or dynamic_requires
        ) and inp.approval_resolver is not None:
            decision = inp.approval_resolver.request(
                call_id=call_id, name=name, arguments=arguments
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
        def observe_display(display: dict) -> None:
            nonlocal seq
            seq += 1
            out.events.append(_event(EventType.TOOL_OUTPUT_UPDATED, inp, seq, {
                "call_id": call_id, "name": name, "public_display": display,
            }))

        output = PublicOutputBuffer(name, arguments, observe_display)
        try:
            with tool_output_scope(output.observe):
                try:
                    result = await _invoke(
                        inp.tool_executor,
                        name,
                        arguments,
                        context=ToolExecutionContext(run_id=inp.run_id, call_id=call_id),
                    )
                finally:
                    output.finish()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单工具失败不终止 Run
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
            if inp.stop_on_error_decider is not None and inp.stop_on_error_decider(
                name, arguments
            ):
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

        raw_result_text = (
            result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        )
        try:
            result_text, governance = _govern(inp, name, raw_result_text)
        except Exception as exc:  # noqa: BLE001 - 治理失败 = 明确报错降级（G10）
            seq += 1
            out.events.append(
                _event(
                    EventType.TOOL_CALL_END,
                    inp,
                    seq,
                    {
                        "call_id": call_id,
                        "name": name,
                        "error": f"result_governance_failed: {exc}",
                        "error_category": "result_governance_failed",
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
                    "content": f"[error] 工具结果治理失败，结果未写入上下文: {exc}",
                }
            )
            if out.working_context is not None:
                out.working_context = record_tool_failure(
                    out.working_context, name=name, error=f"result governance: {exc}"
                )
            continue
        result_text, modified_by_hook = await _apply_tool_result_hook(
            inp, call_id=call_id, name=name, arguments=arguments, result_text=result_text
        )
        # Disclosure comes from the trusted executor, never from a JSON key
        # in a tool result. Presentation failure cannot alter execution facts.
        public_display = None
        presenter = getattr(inp.tool_executor, "public_display", None)
        if callable(presenter):
            try:
                from ksadk.harness.tool_display import safe_tool_display

                public_display = safe_tool_display(presenter(name, arguments, result))
            except Exception:
                public_display = None
        if runtime is not None:
            runtime.record_receipt(
                invocation_id=inp.run_id,
                call_id=call_id,
                tool_name=name,
                arguments=arguments,
                decision="approved",
                status="executed",
                # Receipt 记录原文引用（审计不丢，G10 "不静默丢弃"；on_tool_result
                # 改写只作用于模型可见链，不改变审计事实）。
                result_digest=raw_result_text[:65_536],
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
                    **({"public_display": public_display} if public_display else {}),
                    **(
                        {"result_governance": governance}
                        if governance.get("truncated") or governance.get("offloaded")
                        or governance.get("delimited")
                        else {}
                    ),
                    # G5 §5.2：on_tool_result 改写生效的留痕（Receipt 载荷为固定
                    # schema 不可扩展——见 tool_receipts——以事件+消息留痕为准）。
                    **({"modified_by_hook": True} if modified_by_hook else {}),
                    "receipt_committed": bool(
                        runtime is not None and runtime.receipt_enabled
                    ),
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
                **({"modified_by_hook": True} if modified_by_hook else {}),
            }
        )
        succeeded.add(name)

    out.pending_tool_calls = []
    return out


def _govern(inp: ToolCallInput, name: str, raw_text: str) -> tuple[str, dict[str, Any]]:
    """应用中央结果治理（G10/G16）；未注入 governor 时原样透传。"""

    governor = inp.result_governor
    if governor is None:
        return raw_text, {}
    return governor.govern(name, raw_text)


async def _apply_tool_result_hook(
    inp: ToolCallInput,
    *,
    call_id: str,
    name: str,
    arguments: dict[str, Any],
    result_text: str,
) -> tuple[str, bool]:
    """G5 §5.2 on_tool_result：结果消息回填前应用钩子改写。

    钩子由引擎注入（内部 ``run_hooks`` 按注册顺序跑钩子链；单钩子异常与协议
    违约在闭包内可见降级并沿用上一有效值，不再向上抛）。返回
    ``(最终文本, 是否被钩子改写)``：改写生效时调用侧在结果消息与
    tool.call.end payload 加 ``modified_by_hook=True`` 留痕；未配置钩子或返回
    None（未改写）时不新增任何键——保持无钩子路径的旧语义逐字节不变。
    """
    hook = inp.tool_result_hook
    if hook is None:
        return result_text, False
    rewritten = await hook(
        RunContext(
            run_id=inp.run_id,
            agent_id=inp.agent_id,
            user_id=inp.user_id,
            session_id=inp.session_id,
        ),
        {"call_id": call_id, "name": name, "arguments": arguments},
        {"content": result_text},
    )
    if rewritten is None:
        return result_text, False
    content = str(rewritten.get("content") or "")
    return content, content != result_text


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
        if inp.argument_validator is not None and inp.argument_validator(name, arguments):
            return False
        if name in inp.approval_required:
            return False
        if inp.approval_decider is not None and inp.approval_decider(name, arguments):
            return False
        if not inp.parallel_safe_decider(name, arguments):
            return False
    return True


async def _execute_parallel_batch(inp: ToolCallInput) -> ToolCallOutput:
    """并行执行一批只读调用，按原输入顺序生成确定性事件与消息。"""
    semaphore = asyncio.Semaphore(inp.max_parallelism)

    async def invoke_one(pending: dict[str, Any]) -> tuple[Any | None, Exception | None]:
        async with semaphore:
            try:
                value = await _invoke(
                    inp.tool_executor,
                    pending["name"],
                    pending["arguments"],
                    context=ToolExecutionContext(
                        run_id=inp.run_id,
                        call_id=pending["call_id"],
                    ),
                )
                return value, None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                return None, exc

    results = await asyncio.gather(*(invoke_one(pending) for pending in inp.pending_tool_calls))
    out = ToolCallOutput(working_context=inp.working_context)
    seq = inp.seq_start
    for pending, (result, error) in zip(inp.pending_tool_calls, results, strict=True):
        call_id, name, arguments = (
            pending["call_id"],
            pending["name"],
            pending["arguments"],
        )
        reliability_payload = _resolve_reliability(inp, name, arguments).to_event_payload()
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
                    "parallel": True,
                    "reliability": reliability_payload,
                },
            )
        )
        seq += 1
        modified_by_hook = False
        if error is not None:
            error_text = f"{type(error).__name__}: {error}"
            category = str(getattr(error, "category", "") or "")
            out.events.append(
                _event(
                    EventType.TOOL_CALL_END,
                    inp,
                    seq,
                    {
                        "call_id": call_id,
                        "name": name,
                        "error": error_text,
                        **({"error_category": category} if category else {}),
                        "parallel": True,
                        "receipt_committed": False,
                        "reliability": reliability_payload,
                    },
                )
            )
            result_text = f"[error] {error_text}"
            if out.working_context is not None:
                out.working_context = record_tool_failure(
                    out.working_context, name=name, error=error_text
                )
        else:
            raw_result_text = (
                result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            )
            result_text, governance = _govern(inp, name, raw_result_text)
            result_text, modified_by_hook = await _apply_tool_result_hook(
                inp, call_id=call_id, name=name, arguments=arguments, result_text=result_text
            )
            out.events.append(
                _event(
                    EventType.TOOL_CALL_END,
                    inp,
                    seq,
                    {
                        "call_id": call_id,
                        "name": name,
                        "result": result,
                        "parallel": True,
                        **(
                            {"result_governance": governance}
                            if governance.get("truncated") or governance.get("offloaded")
                            or governance.get("delimited")
                            else {}
                        ),
                        **({"modified_by_hook": True} if modified_by_hook else {}),
                        "receipt_committed": False,
                        "reliability": reliability_payload,
                    },
                )
            )
            if out.working_context is not None:
                out.working_context = record_tool_result(
                    out.working_context, name=name, result_text=result_text
                )
        out.new_messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": result_text,
                **({"modified_by_hook": True} if modified_by_hook else {}),
            }
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
    if inp.live_event_sink is not None:
        inp.live_event_sink(event)
    return event


__all__ = [
    "APPROVED",
    "ArgumentValidator",
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
