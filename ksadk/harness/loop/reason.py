"""reason 一轮判定（plan §7.1 reason 节点）——引擎无关纯逻辑。

输入：当前消息序列 + 可用工具 + 模型/指令；输出：模型调用结果、本轮应发事件、
路由决策（继续 tool_calls 还是 final）。不依赖 LangGraph 图 State、不直接
append 引擎事件队列——把事件作为列表返回，由引擎节点收集。

失败语义（§7.2 稳定性策略）：model 调用抛错时，started 必被闭合（failed 事件），
``raise`` 由调用方决定是否重试（见 ``loop/retry.py``）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.model_provider import (
    ModelFailureAction,
    classify_model_failure,
    decide_model_failure_action,
    retry_delay_ms,
    safe_model_error_message,
)
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn
from ksadk.harness.spec import ModelProviderPolicy

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
    fallback_model_refs: tuple[str, ...] = ()
    provider_policy: ModelProviderPolicy = field(default_factory=ModelProviderPolicy)
    #: 用于 RuntimeEvent 标识的锚点（由引擎注入）。
    agent_id: str = ""
    user_id: str = ""
    session_id: str = ""
    run_id: str = ""
    seq_start: int = 0
    #: turn 计数与上限（§7.1 reason 自环 + 上限保护）。
    turn_count: int = 0
    max_turns: int = 8
    #: 本次 Provider 调用允许生成的硬上限；None 表示未配置 Run 预算。
    max_output_tokens: int | None = None
    #: 流式模式：True 时调 reasoner.stream_complete 逐 chunk 发 TEXT_DELTA。
    streaming: bool = False
    #: 可选实时事件出口。流式引擎用它在模型调用尚未结束时交付 started/delta；
    #: 事件仍保留在 ReasonOutput，供非流式调用方与审计使用。
    live_event_sink: Callable[[RuntimeEvent], None] | None = None


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
    #: 本轮实际完成调用的模型；发生降级时不同于主模型引用。
    selected_model_ref: str | None = None


class ReasoningLimitError(RuntimeError):
    """超过最大推理轮数（§7.1），引擎应终止而非继续自环。"""


class ModelFailoverExhausted(RuntimeError):
    """模型调用被策略终止，并携带已闭合的审计事件。"""

    def __init__(
        self,
        *,
        events: Sequence[RuntimeEvent],
        attempted_models: Sequence[str],
        last_error: Exception,
        stop_reason: ModelFailureAction,
    ) -> None:
        safe_error = safe_model_error_message(last_error)
        if stop_reason == ModelFailureAction.ABORT:
            message = (
                "model invocation aborted by provider policy "
                f"({len(attempted_models)} attempts): {safe_error}"
            )
        elif stop_reason == ModelFailureAction.RECOVER_CONTEXT:
            message = (
                "model context overflow requires emergency context recovery "
                f"({len(attempted_models)} attempts): {safe_error}"
            )
        else:
            message = (
                "all configured model profiles failed "
                f"({len(attempted_models)} attempts): {safe_error}"
            )
        super().__init__(message)
        self.events = tuple(events)
        self.attempted_models = tuple(attempted_models)
        self.last_error = last_error
        self.stop_reason = stop_reason


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

    candidates = tuple(dict.fromkeys((inp.model_ref, *inp.fallback_model_refs)))
    turn: HarnessReasoningTurn | None = None
    selected_model_ref: str | None = None
    total_attempt = 0
    attempted_models: list[str] = []
    last_error: Exception | None = None
    stop_reason = ModelFailureAction.ABORT
    stop = False
    for candidate_index, model_ref in enumerate(candidates, start=1):
        for model_attempt in range(1, inp.provider_policy.max_attempts_per_model + 1):
            if total_attempt >= inp.provider_policy.total_attempt_budget:
                stop = True
                break
            total_attempt += 1
            attempted_models.append(model_ref)
            event_meta = {
                "model": model_ref,
                "attempt": total_attempt,
                "model_attempt": model_attempt,
                "candidate_index": candidate_index,
                "fallback": candidate_index > 1,
            }
            seq += 1
            started_event = _event(EventType.MODEL_CALL_STARTED, inp, seq, event_meta)
            out.events.append(started_event)
            if inp.live_event_sink is not None:
                inp.live_event_sink(started_event)
            try:
                if inp.streaming and hasattr(inp.reasoner, "stream_complete"):
                    text_parts: list[str] = []
                    async for item in inp.reasoner.stream_complete(
                        model=model_ref,
                        prompt=inp.instructions,
                        messages=tuple(inp.messages),
                        tools=list(inp.tools),
                        **(
                            {"max_output_tokens": inp.max_output_tokens}
                            if inp.max_output_tokens is not None
                            else {}
                        ),
                    ):
                        if "text_delta" in item:
                            text_parts.append(item["text_delta"])
                            seq += 1
                            delta_event = _event(
                                EventType.TEXT_DELTA,
                                inp,
                                seq,
                                {"text": item["text_delta"]},
                            )
                            out.events.append(delta_event)
                            if inp.live_event_sink is not None:
                                inp.live_event_sink(delta_event)
                        if "reasoning_delta" in item:
                            seq += 1
                            reasoning_event = _event(
                                EventType.REASONING_DELTA,
                                inp,
                                seq,
                                {"text": item["reasoning_delta"]},
                                phase="commentary",
                            )
                            out.events.append(reasoning_event)
                            if inp.live_event_sink is not None:
                                inp.live_event_sink(reasoning_event)
                        if "turn" in item:
                            turn = item["turn"]
                else:
                    turn = await inp.reasoner.complete(
                        model=model_ref,
                        prompt=inp.instructions,
                        messages=tuple(inp.messages),
                        tools=list(inp.tools),
                        **(
                            {"max_output_tokens": inp.max_output_tokens}
                            if inp.max_output_tokens is not None
                            else {}
                        ),
                    )
            except Exception as exc:  # noqa: BLE001 - 每次 started 必被 failed 闭合
                last_error = exc
                failure = classify_model_failure(exc)
                action = decide_model_failure_action(
                    failure,
                    policy=inp.provider_policy,
                    model_attempt=model_attempt,
                    total_attempt=total_attempt,
                    has_fallback=candidate_index < len(candidates),
                )
                stop_reason = action
                delay_ms = (
                    retry_delay_ms(inp.provider_policy, model_attempt=model_attempt)
                    if action == ModelFailureAction.RETRY
                    else 0
                )
                seq += 1
                out.events.append(
                    _event(
                        EventType.MODEL_CALL_FAILED,
                        inp,
                        seq,
                        {
                            **event_meta,
                            "error": safe_model_error_message(exc),
                            "error_type": type(exc).__name__,
                            "failure_category": failure.kind.value,
                            "status_code": failure.status_code,
                            "action": action.value,
                            "retry_delay_ms": delay_ms,
                        },
                    )
                )
                if action == ModelFailureAction.RETRY:
                    if delay_ms:
                        await asyncio.sleep(delay_ms / 1000)
                    continue
                if action == ModelFailureAction.FAILOVER:
                    break
                stop = True
                break

            selected_model_ref = model_ref
            seq += 1
            out.events.append(_event(EventType.MODEL_CALL_COMPLETED, inp, seq, event_meta))
            break
        if turn is not None or stop:
            break

    if turn is None or selected_model_ref is None:
        assert last_error is not None
        raise ModelFailoverExhausted(
            events=out.events,
            attempted_models=attempted_models,
            last_error=last_error,
            stop_reason=stop_reason,
        ) from last_error

    assert turn is not None and selected_model_ref is not None
    out.selected_model_ref = selected_model_ref

    if turn.usage:
        usage = dict(turn.usage)
        usage_payload = {
            "model": selected_model_ref,
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(
                usage.get("total_tokens")
                or (int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0))
            ),
        }
        if usage.get("cached_tokens") is not None:
            usage_payload["cached_tokens"] = int(usage.get("cached_tokens") or 0)
        if usage.get("reasoning_tokens") is not None:
            usage_payload["reasoning_tokens"] = int(usage.get("reasoning_tokens") or 0)
        seq += 1
        out.events.append(
            _event(
                EventType.USAGE_REPORTED,
                inp,
                seq,
                usage_payload,
            )
        )
        out.usage = usage

    if turn.reasoning:
        seq += 1
        out.events.append(
            _event(
                EventType.REASONING_COMPLETED,
                inp,
                seq,
                {"text": turn.reasoning},
                phase="commentary",
            ),
        )

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
        seq += 1
        out.events.append(
            _event(
                EventType.TEXT_COMPLETED,
                inp,
                seq,
                {"text": turn.final_text or ""},
                phase="final_answer",
            ),
        )
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
    "ModelFailoverExhausted",
    "ROUTE_FINAL",
    "ROUTE_TOOL_CALLS",
    "reason_turn",
    "reason_turn_async",
]
