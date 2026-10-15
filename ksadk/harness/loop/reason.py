"""reason 一轮判定（plan §7.1 reason 节点）——引擎无关纯逻辑。

输入：当前消息序列 + 可用工具 + 模型/指令；输出：模型调用结果、本轮应发事件、
路由决策（继续 tool_calls 还是 final）。不依赖 LangGraph 图 State、不直接
append 引擎事件队列——把事件作为列表返回，由引擎节点收集。

失败语义（§7.2 稳定性策略）：model 调用抛错时，started 必被闭合（failed 事件），
``raise`` 由调用方决定是否重试（重试与退避逻辑见 ``model_provider`` 的
``classify_model_failure`` / ``retry_delay_ms``，本模块已按需导入）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Sequence

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.model_provider import (
    ModelFailureAction,
    ModelFailureKind,
    classify_model_failure,
    decide_model_failure_action,
    retry_after_ms_from_error,
    retry_delay_ms,
    safe_model_error_message,
)
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn
from ksadk.harness.spec import ModelProviderPolicy
from ksadk.model_gateway.events import ModelStreamEvent, ModelStreamEventType

_LOGGER = logging.getLogger(__name__)

#: reason 节点的路由结果。引擎据此走 tool_calls 或 final 出口。
ROUTE_TOOL_CALLS = "tool_calls"
ROUTE_FINAL = "final"

_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(r"<think\b[^>]*>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think\s*>", re.IGNORECASE)


def _visible_answer(text: str | None) -> str:
    """Remove provider think envelopes from the assistant's public text.

    Some compatible endpoints return the reasoning prefix in ``content`` and
    only the closing ``</think>`` marker survives their stream projection.
    That prefix must not become a Studio answer or compaction fact.
    """
    value = text or ""
    if not any(marker in value.lower() for marker in ("<think", "</think")):
        return value
    value = _THINK_BLOCK.sub("", value)
    if _THINK_CLOSE.search(value):
        value = _THINK_CLOSE.split(value, maxsplit=1)[-1]
    if _THINK_OPEN.search(value):
        value = _THINK_OPEN.split(value, maxsplit=1)[0]
    return value.strip()


def _provider_failure_stream_event(
    *,
    model: str,
    sequence: int,
    failure_category: str,
    status_code: int | None,
    retrying: bool,
    retry_after_ms: int | None,
    error: Exception,
) -> dict[str, Any]:
    """Project a provider failure/retry decision into the shared stream contract."""

    if retrying:
        event_type = ModelStreamEventType.PROVIDER_RETRYING
    elif failure_category == ModelFailureKind.RATE_LIMIT.value:
        event_type = ModelStreamEventType.PROVIDER_RATE_LIMITED
    else:
        event_type = ModelStreamEventType.TURN_FAILED

    details = getattr(error, "details", None)
    provider_request_id = ""
    if isinstance(details, dict):
        provider_request_id = str(details.get("providerRequestId") or "")

    return ModelStreamEvent(
        type=event_type,
        model=model,
        sequence=sequence,
        failure_category=failure_category,
        status_code=status_code,
        provider_request_id=provider_request_id,
        retry_after_ms=retry_after_ms,
    ).as_dict()


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
    #: 本次 Provider 调用的推理强度。它来自宿主的回合级选择（如 Studio
    #: reasoning_effort），不是 Harness 自行猜测的模型默认值。
    reasoning_effort: str | None = None
    #: 流式模式：True 时调 reasoner.stream_complete 逐 chunk 发 TEXT_DELTA。
    streaming: bool = False
    #: 可选实时事件出口。流式引擎用它在模型调用尚未结束时交付 started/delta；
    #: 事件仍保留在 ReasonOutput，供非流式调用方与审计使用。
    live_event_sink: Callable[[RuntimeEvent], None] | None = None
    before_attempt: Callable[[], None] | None = None


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
        failure = classify_model_failure(last_error)
        retry_detail = f" after {len(attempted_models)} attempts"
        if failure.status_code is not None:
            retry_detail += f" (HTTP {failure.status_code})"
        elif failure.kind.value != "unknown":
            retry_detail += f" ({failure.kind.value})"
        if stop_reason == ModelFailureAction.ABORT:
            message = (
                "model invocation aborted by provider policy "
                f"{retry_detail}: {safe_error}"
            )
        elif stop_reason == ModelFailureAction.RECOVER_CONTEXT:
            message = (
                "model context overflow requires emergency context recovery "
                f"{retry_detail}: {safe_error}"
            )
        elif failure.kind.value == "rate_limit":
            message = (
                "模型服务限流，已停止自动重试"
                f"{retry_detail}: {safe_error}"
            )
        else:
            message = (
                "all configured model profiles failed "
                f"{retry_detail}: {safe_error}"
            )
        super().__init__(message)
        self.events = tuple(events)
        self.attempted_models = tuple(attempted_models)
        self.last_error = last_error
        self.failure = failure
        self.stop_reason = stop_reason

    def runtime_error(self) -> RuntimeError:
        """Keep classified failure facts across the graph exception boundary."""
        wrapped = RuntimeError(str(self))
        if self.failure.kind.value != "unknown":
            wrapped.error_category = self.failure.kind.value
        if self.failure.status_code is not None:
            wrapped.status_code = self.failure.status_code
        if self.failure.kind.value == "rate_limit":
            wrapped.error_code = "MODEL_RATE_LIMITED"
        return wrapped


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

    # Bound alternatives are not an implicit failover chain.  The caller must
    # explicitly opt into automatic failover, otherwise an explicit model
    # choice remains stable for the whole turn.
    candidates = (
        tuple(dict.fromkeys((inp.model_ref, *inp.fallback_model_refs)))
        if inp.provider_policy.allow_failover
        else (inp.model_ref,)
    )
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
            if inp.before_attempt is not None:
                inp.before_attempt()
            total_attempt += 1
            attempted_models.append(model_ref)
            event_meta = {
                "model": model_ref,
                "attempt": total_attempt,
                "model_attempt": model_attempt,
                # Only rate limits receive the full ten-attempt schedule.
                # A no-progress timeout gets at most two same-model attempts
                # when automatic failover is disabled, so the public activity
                # must not claim it is on attempt N/10.
                "max_attempts": inp.provider_policy.max_attempts_per_model,
                "candidate_index": candidate_index,
                "fallback": candidate_index > 1,
            }
            seq += 1
            started_event = _event(EventType.MODEL_CALL_STARTED, inp, seq, event_meta)
            out.events.append(started_event)
            if inp.live_event_sink is not None:
                inp.live_event_sink(started_event)
            from ksadk.harness.public_summary import PublicSummaryBuffer

            summary_parts: list[str] = []
            summary_buffer = PublicSummaryBuffer()
            summary_raw = ""

            def append_summary(raw="", *, final=False):
                nonlocal seq, summary_raw
                summary_raw = (summary_raw + raw)[:summary_buffer.LIMIT]
                summary = summary_buffer.drain(summary_raw, final=final)
                if not summary:
                    return
                seq += 1
                event = _event(EventType.PUBLIC_SUMMARY_DELTA, inp, seq, {
                    "text": summary, "summary_id": f"{turn_count}:{model_ref}:{total_attempt}",
                    "summary_start": not summary_parts,
                }, phase="commentary")
                summary_parts.append(summary)
                out.events.append(event)
                if inp.live_event_sink is not None:
                    inp.live_event_sink(event)

            try:
                if inp.streaming and hasattr(inp.reasoner, "stream_complete"):
                    text_parts: list[str] = []
                    reasoning_parts: list[str] = []
                    request_options = {
                        **(
                            {"max_output_tokens": inp.max_output_tokens}
                            if inp.max_output_tokens is not None
                            else {}
                        ),
                        **(
                            {"reasoning_effort": inp.reasoning_effort}
                            if inp.reasoning_effort
                            else {}
                        ),
                    }
                    async for item in inp.reasoner.stream_complete(
                        model=model_ref,
                        prompt=inp.instructions,
                        messages=tuple(inp.messages),
                        tools=list(inp.tools),
                        **request_options,
                    ):
                        if "text_delta" in item:
                            text_parts.append(item["text_delta"])
                            seq += 1
                            delta_event = _event(
                                EventType.TEXT_DELTA,
                                inp,
                                seq,
                                {
                                    "text": item["text_delta"],
                                    "delta_index": len(text_parts) - 1,
                                },
                            )
                            out.events.append(delta_event)
                            # A provider may stream explanatory text before it
                            # emits tool-call fragments (for example, "let me
                            # delegate...").  Until the turn is complete we do
                            # not know whether that text is a final answer.  A
                            # tools-enabled turn therefore buffers answer
                            # deltas; final-only turns retain live streaming.
                            # A retryable/failover attempt may fail after it has
                            # streamed partial prose.  Publishing that prose
                            # immediately makes every retry append another
                            # abandoned answer to Studio.  Keep it buffered
                            # until the attempt succeeds unless the policy has
                            # exactly one possible model attempt.
                            if (
                                inp.live_event_sink is not None
                                and not inp.tools
                                and inp.provider_policy.max_attempts_per_model == 1
                                and not inp.fallback_model_refs
                            ):
                                inp.live_event_sink(delta_event)
                        if "summary_delta" in item:
                            append_summary(str(item["summary_delta"]))
                        if "reasoning_delta" in item:
                            reasoning_parts.append(item["reasoning_delta"])
                            seq += 1
                            reasoning_event = _event(
                                EventType.REASONING_DELTA,
                                inp,
                                seq,
                                    {
                                        "text": item["reasoning_delta"],
                                        "delta_index": len(reasoning_parts) - 1,
                                    },
                                phase="commentary",
                            )
                            out.events.append(reasoning_event)
                            if inp.live_event_sink is not None:
                                inp.live_event_sink(reasoning_event)
                        if "turn" in item:
                            turn = item["turn"]
                    append_summary(final=True)
                    if summary_parts:
                        seq += 1
                        completed_summary = _event(EventType.PUBLIC_SUMMARY_COMPLETED, inp, seq, {
                            "text": "".join(summary_parts), "summary_id": f"{turn_count}:{model_ref}:{total_attempt}",
                        }, phase="commentary")
                        out.events.append(completed_summary)
                        if inp.live_event_sink is not None:
                            inp.live_event_sink(completed_summary)
                else:
                    request_options = {
                        **(
                            {"max_output_tokens": inp.max_output_tokens}
                            if inp.max_output_tokens is not None
                            else {}
                        ),
                        **(
                            {"reasoning_effort": inp.reasoning_effort}
                            if inp.reasoning_effort
                            else {}
                        ),
                    }
                    turn = await inp.reasoner.complete(
                        model=model_ref,
                        prompt=inp.instructions,
                        messages=tuple(inp.messages),
                        tools=list(inp.tools),
                        **request_options,
                    )
            except Exception as exc:  # noqa: BLE001 - 每次 started 必被 failed 闭合
                append_summary(final=True)
                if summary_parts:
                    seq += 1
                    summary_end = _event(EventType.PUBLIC_SUMMARY_COMPLETED, inp, seq, {
                        "text": "".join(summary_parts), "summary_id": f"{turn_count}:{model_ref}:{total_attempt}",
                    }, phase="commentary")
                    out.events.append(summary_end)
                    if inp.live_event_sink is not None:
                        inp.live_event_sink(summary_end)
                # Failed-attempt output is not a user answer.  Retain the
                # model.call.failed fact, but remove its partial text/reasoning
                # before retry/failover so it cannot be replayed into chat.
                out.events = [
                    event
                    for event in out.events
                    if event.event_type
                    not in {EventType.TEXT_DELTA, EventType.REASONING_DELTA}
                ]
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
                public_max_attempts = inp.provider_policy.max_attempts_per_model
                if (
                    failure.kind is ModelFailureKind.TIMEOUT
                    and not inp.provider_policy.allow_failover
                ):
                    # The timeout policy permits one same-model recovery only.
                    # Keep the durable/public attempt denominator honest.
                    public_max_attempts = min(public_max_attempts, 2)
                delay_ms = 0
                retry_after_source = ""
                if action == ModelFailureAction.RETRY:
                    delay_ms = retry_delay_ms(
                        inp.provider_policy, model_attempt=model_attempt
                    )
                    # 限流类错误优先尊重服务端 Retry-After 头（G15.2）；
                    # 以策略 max_backoff 封顶，超限时退回确定性退避节奏。
                    if failure.status_code == 429:
                        header_ms = retry_after_ms_from_error(exc)
                        if header_ms is not None:
                            max_backoff = inp.provider_policy.max_backoff_ms
                            delay_ms = (
                                min(header_ms, max_backoff)
                                if max_backoff > 0
                                else header_ms
                            )
                            retry_after_source = "header"
                seq += 1
                # 统一的失败取证点：无论异常从哪个抛点冒出来（缓冲 / 流式 / Responses /
                # 传输层），这里都能拿到异常类型、错误码、状态码与安全化消息。
                # 2026-09-19 实测教训：逐个抛点加日志会漏——我在 model_client 的两个抛点
                # 都加了日志，18 条 MODEL_REQUEST_FAILED 重试却一条都没触发；必须在
                # **分类发生的那一处**统一留痕，才不会再靠猜。
                _LOGGER.warning(
                    "模型调用失败：attempt=%s/%s category=%s exc=%s code=%s status=%s msg=%s",
                    model_attempt,
                    inp.provider_policy.max_attempts_per_model,
                    failure.kind.value,
                    type(exc).__name__,
                    str(getattr(exc, "code", "") or ""),
                    failure.status_code,
                    safe_model_error_message(exc),
                )
                failed_event = _event(
                    EventType.MODEL_CALL_FAILED,
                    inp,
                    seq,
                    {
                        **event_meta,
                        "max_attempts": public_max_attempts,
                        "error": safe_model_error_message(exc),
                        "error_type": type(exc).__name__,
                        # 错误码必须进事件：否则"上游真的 502"和"我们自判失败用了
                        # 502"在公开叙述里长得一模一样，无法区分（2026-09-18 实测：
                        # 18 条 unavailable[502] 重试，查不出到底是谁的问题）。
                        "error_code": str(getattr(exc, "code", "") or ""),
                        "failure_category": failure.kind.value,
                        "status_code": failure.status_code,
                        "action": action.value,
                        "failover_model": (
                            candidates[candidate_index]
                            if action is ModelFailureAction.FAILOVER
                            and candidate_index < len(candidates)
                            else ""
                        ),
                        "retry_delay_ms": delay_ms,
                        "retry_after_source": retry_after_source,
                        "model_stream_event": _provider_failure_stream_event(
                            model=model_ref,
                            sequence=total_attempt,
                            failure_category=failure.kind.value,
                            status_code=failure.status_code,
                            retrying=action is ModelFailureAction.RETRY,
                            retry_after_ms=delay_ms or None,
                            error=exc,
                        ),
                    },
                )
                out.events.append(failed_event)
                if inp.live_event_sink is not None:
                    inp.live_event_sink(failed_event)
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

    # Text streamed alongside a tool call is provider scratch narration, not
    # a completed assistant answer.  Keeping those deltas would leave an
    # orphan message in Studio even though the real work continues in tools.
    if turn.tool_calls:
        out.events = [
            event for event in out.events if event.event_type != EventType.TEXT_DELTA
        ]

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
            # 单次模型调用的增量（长任务方案 §19.2 usage_mode）：消费者
            # 按 mode=delta 累加，而非覆盖（交接记录 §6 Usage 计量缺陷）。
            "mode": "delta",
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

    visible_text = _visible_answer(turn.final_text)
    if visible_text != (turn.final_text or ""):
        # Buffered deltas may still contain the provider's private prefix.
        # The final snapshot below is built only from normalized public text.
        out.events = [
            event for event in out.events if event.event_type != EventType.TEXT_DELTA
        ]
    turn = replace(turn, final_text=visible_text)

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
        # Tool-call content is not a trustworthy public commentary channel.
        # Some OpenAI-compatible reasoning endpoints place private analysis
        # (including stray </think> markers) there. The tool call and its
        # result carry the actionable context; keep model prose out of both
        # the public feed and the next model/compaction transcript.
        out.events = [
            event for event in out.events if event.event_type != EventType.TEXT_DELTA
        ]
        out.new_messages.append(
            {
                "role": "assistant",
                "content": "",
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
            {
                "call_id": c.call_id,
                "name": c.name,
                "arguments": c.arguments,
                "invalid_reason": c.invalid_reason,
            }
            for c in turn.tool_calls
        ]
        out.route = ROUTE_TOOL_CALLS
    else:
        if turn.degraded:
            # 可见降级（不静默）：例如始终思考的端点把输出预算耗在
            # reasoning_content 上，本回合既无内容也无工具调用。
            seq += 1
            out.events.append(
                _event(
                    EventType.POLICY_DECISION,
                    inp,
                    seq,
                    {
                        "action": "degraded",
                        "reason": turn.degraded,
                        "outcome": "degraded",
                    },
                )
            )
        out.new_messages.append({"role": "assistant", "content": turn.final_text or ""})
        seq += 1
        out.events.append(
            _event(
                EventType.TEXT_COMPLETED,
                inp,
                seq,
                {
                    "text": turn.final_text or "",
                    "streamed": bool(inp.streaming and hasattr(inp.reasoner, "stream_complete")),
                },
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
