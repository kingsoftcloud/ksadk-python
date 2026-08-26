"""reason 一轮判定（plan §7.1 reason 节点）——引擎无关纯逻辑。

输入：当前消息序列 + 可用工具 + 模型/指令；输出：模型调用结果、本轮应发事件、
路由决策（继续 tool_calls 还是 final）。不依赖 LangGraph 图 State、不直接
append 引擎事件队列——把事件作为列表返回，由引擎节点收集。

失败语义（§7.2 稳定性策略）：model 调用抛错时，started 必被闭合（failed 事件），
``raise`` 由调用方决定是否重试（见 ``loop/retry.py``）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from ksadk.events import EventType, RuntimeEvent
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.working_context import WorkingContext

#: reason 节点的路由结果。引擎据此走 tool_calls 或 final 出口。
ROUTE_TOOL_CALLS = "tool_calls"
ROUTE_FINAL = "final"


@dataclass(frozen=True)
class ReasonInput:
    """reason 一轮的输入（纯数据，不持有图 State）。"""

    model_ref: str
    instructions: str
    messages: Sequence[dict[str, Any]]
    tools: Sequence[Any]
    reasoner: HarnessReasoner
    #: 用于 RuntimeEvent 标识的锚点（由引擎注入）。
    agent_id: str = ""
    user_id: str = ""
    session_id: str = ""
    run_id: str = ""
    seq_start: int = 0
    #: turn 计数与上限（§7.1 reason 自环 + 上限保护）。
    turn_count: int = 0
    max_turns: int = 8


@dataclass
class ReasonOutput:
    """reason 一轮的输出（待发事件 + 路由 + 更新后的消息/工作上下文）。"""

    #: 引擎应 append 的 RuntimeEvent（已带 seq_id，按顺序）。
    events: list[RuntimeEvent] = field(default_factory=list)
    route: str = ROUTE_FINAL
    #: 追加到对话的 OpenAI 形态消息（assistant 含 tool_calls，或纯文本）。
    new_messages: list[dict[str, Any]] = field(default_factory=list)
    #: 待执行的工具调用（route=tool_calls 时非空）。
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: 本轮 usage（供引擎发 usage.reported，已含在 events 中）。
    usage: dict[str, int] | None = None


class ReasoningLimitError(RuntimeError):
    """超过最大推理轮数（§7.1），引擎应终止而非继续自环。"""


def reason_turn(turn_count: int, inp: ReasonInput) -> ReasonOutput | None:
    """执行一轮 reason（同步签名——reasoner.complete 是异步的，故返回 None 时
    调用方需用 :func:`reason_turn_async`）。

    本函数是引擎无关的判定入口，但实际上模型调用是异步的，因此引擎节点应
    调用 :func:`reason_turn_async`。保留同步签名仅为类型完整与单测的"轮数
    超限"前置校验。
    """
    if turn_count > inp.max_turns:
        raise ReasoningLimitError(f"reasoning exceeded {inp.max_turns} turns")
    return None


async def reason_turn_async(turn_count: int, inp: ReasonInput) -> ReasonOutput:
    """执行一轮 reason（异步，调 reasoner.complete）。

    返回 :class:`ReasonOutput`：model.start/completed（+usage）事件 + route
    决策 + 新消息。model 调用失败时发 model.call.failed 后 raise（由调用方
    决定重试/收尾）。
    """
    if turn_count > inp.max_turns:
        raise ReasoningLimitError(f"reasoning exceeded {inp.max_turns} turns")

    seq = inp.seq_start
    out = ReasonOutput()

    seq += 1
    out.events.append(
        _event(EventType.MODEL_CALL_STARTED, inp, seq, {"model": inp.model_ref})
    )
    try:
        turn: HarnessReasoningTurn = await inp.reasoner.complete(
            model=inp.model_ref,
            prompt=inp.instructions,
            messages=tuple(inp.messages),
            tools=list(inp.tools),
        )
    except Exception as exc:  # noqa: BLE001 - started 必被闭合
        seq += 1
        out.events.append(
            _event(
                EventType.MODEL_CALL_FAILED,
                inp,
                seq,
                {"model": inp.model_ref, "error": str(exc)},
            )
        )
        raise

    seq += 1
    out.events.append(
        _event(EventType.MODEL_CALL_COMPLETED, inp, seq, {"model": inp.model_ref})
    )

    if turn.usage:
        usage = dict(turn.usage)
        seq += 1
        out.events.append(
            _event(
                EventType.USAGE_REPORTED,
                inp,
                seq,
                {
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "total_tokens": int(
                        usage.get("total_tokens")
                        or (int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0))
                    ),
                },
            )
        )
        out.usage = usage

    if turn.tool_calls:
        out.new_messages.append(
            {
                "role": "assistant",
                "content": turn.final_text,
                "tool_calls": [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in turn.tool_calls
                ],
            }
        )
        out.pending_tool_calls = [
            {"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
            for c in turn.tool_calls
        ]
        out.route = ROUTE_TOOL_CALLS
    else:
        out.new_messages.append({"role": "assistant", "content": turn.final_text or ""})
        out.route = ROUTE_FINAL
    return out


def _event(
    event_type: str,
    inp: ReasonInput,
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
    "ReasonInput",
    "ReasonOutput",
    "ReasoningLimitError",
    "ROUTE_FINAL",
    "ROUTE_TOOL_CALLS",
    "reason_turn",
    "reason_turn_async",
]
