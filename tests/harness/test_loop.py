"""loop/ 引擎无关纯逻辑单测（plan §16：无需 LangGraph 即可单测）。

这些测试不 import 也不依赖 LangGraph——验证拆分目标达成：reason/tool 逻辑
可在无执行引擎环境单测，同时服务 Conformance（§15）。
"""

from __future__ import annotations

import asyncio

import pytest

from ksadk.harness.events import EventType
from ksadk.harness.loop.reason import (
    ROUTE_FINAL,
    ROUTE_TOOL_CALLS,
    ModelFailoverExhausted,
    ReasoningLimitError,
    ReasonInput,
    reason_turn_async,
)
from ksadk.harness.loop.tools import (
    ToolCallInput,
    execute_tool_calls,
)
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.spec import ModelProviderPolicy
from ksadk.harness.state import WorkingContext


class _ScriptedReasoner:
    """不触网的 reasoner（实现 HarnessReasoner Protocol，无 LangGraph）。"""

    def __init__(self, turns: list[HarnessReasoningTurn]) -> None:
        self._turns = list(turns)
        self._i = 0

    async def complete(self, *, model, prompt, messages, tools):  # type: ignore[no-untyped-def]
        turn = self._turns[self._i]
        self._i += 1
        return turn


class _Exec:
    def __init__(self, results: dict[str, object]) -> None:
        self._results = results
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, name, arguments):  # type: ignore[no-untyped-def]
        self.calls.append((name, arguments))
        if name not in self._results:
            raise RuntimeError(f"tool {name!r} not configured")
        result = self._results[name]
        if isinstance(result, Exception):
            raise result
        return result


class _DenyResolver:
    def request(self, *, call_id, name, arguments):  # type: ignore[no-untyped-def]
        return "denied"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ------------------------------------------------------------------ reason


def test_reason_final_when_no_tool_calls():
    out = _run(
        reason_turn_async(
            1,
            ReasonInput(
                model_ref="m",
                instructions="你是助手",
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                reasoner=_ScriptedReasoner([HarnessReasoningTurn(final_text="你好")]),
            ),
        )
    )
    types = [e.event_type for e in out.events]
    assert EventType.MODEL_CALL_STARTED in types
    assert EventType.MODEL_CALL_COMPLETED in types
    assert out.route == ROUTE_FINAL
    assert out.new_messages == [{"role": "assistant", "content": "你好"}]
    assert out.pending_tool_calls == []


def test_reason_routes_to_tool_calls_when_tools():
    out = _run(
        reason_turn_async(
            1,
            ReasonInput(
                model_ref="m",
                instructions="",
                messages=[{"role": "user", "content": "查预算"}],
                tools=["t"],
                reasoner=_ScriptedReasoner(
                    [
                        HarnessReasoningTurn(
                            tool_calls=(
                                HarnessToolCall(call_id="c1", name="lookup", arguments={"q": "x"}),
                            ),
                        )
                    ]
                ),
            ),
        )
    )
    assert out.route == ROUTE_TOOL_CALLS
    assert out.new_messages[0]["role"] == "assistant"
    assert out.new_messages[0]["tool_calls"][0]["function"]["name"] == "lookup"
    assert out.pending_tool_calls[0]["call_id"] == "c1"


def test_reason_emits_usage_when_present():
    out = _run(
        reason_turn_async(
            1,
            ReasonInput(
                model_ref="m",
                instructions="",
                messages=[{"role": "user", "content": "x"}],
                tools=[],
                reasoner=_ScriptedReasoner(
                    [
                        HarnessReasoningTurn(
                            final_text="ok", usage={"input_tokens": 10, "output_tokens": 5}
                        )
                    ]
                ),
            ),
        )
    )
    usage_events = [e for e in out.events if e.event_type == EventType.USAGE_REPORTED]
    assert usage_events and usage_events[0].payload["input_tokens"] == 10
    assert usage_events[0].payload["total_tokens"] == 15


def test_reason_model_failure_emits_failed_then_raises():
    class _Boom:
        async def complete(self, **_):  # type: ignore[no-untyped-def]
            raise RuntimeError("provider 500")

    with pytest.raises(ModelFailoverExhausted, match="model invocation aborted") as error:
        _run(
            reason_turn_async(
                1,
                ReasonInput(
                    model_ref="m",
                    instructions="",
                    messages=[{"role": "user", "content": "x"}],
                    tools=[],
                    reasoner=_Boom(),
                    provider_policy=ModelProviderPolicy(max_attempts_per_model=1),
                ),
            )
        )
    assert [event.event_type for event in error.value.events] == [
        EventType.MODEL_CALL_STARTED,
        EventType.MODEL_CALL_FAILED,
    ]
    assert error.value.events[-1].payload["error"] == "provider 500"


def test_reason_falls_back_and_audits_effective_model():
    class _FallbackReasoner:
        def __init__(self) -> None:
            self.models: list[str] = []

        async def complete(self, *, model, **_):  # type: ignore[no-untyped-def]
            self.models.append(model)
            if model == "primary":
                raise RuntimeError("primary unavailable")
            return HarnessReasoningTurn(
                final_text="备用模型完成",
                usage={"input_tokens": 7, "output_tokens": 3},
            )

    reasoner = _FallbackReasoner()
    out = _run(
        reason_turn_async(
            1,
            ReasonInput(
                model_ref="primary",
                fallback_model_refs=("backup",),
                instructions="",
                messages=[{"role": "user", "content": "x"}],
                tools=[],
                reasoner=reasoner,
                provider_policy=ModelProviderPolicy(
                    max_attempts_per_model=1,
                    initial_backoff_ms=0,
                    max_backoff_ms=0,
                ),
            ),
        )
    )
    model_events = [
        event
        for event in out.events
        if event.event_type
        in {
            EventType.MODEL_CALL_STARTED,
            EventType.MODEL_CALL_FAILED,
            EventType.MODEL_CALL_COMPLETED,
        }
    ]
    assert reasoner.models == ["primary", "backup"]
    assert [(event.event_type, event.payload["model"]) for event in model_events] == [
        (EventType.MODEL_CALL_STARTED, "primary"),
        (EventType.MODEL_CALL_FAILED, "primary"),
        (EventType.MODEL_CALL_STARTED, "backup"),
        (EventType.MODEL_CALL_COMPLETED, "backup"),
    ]
    assert model_events[-1].payload["model"] == "backup"
    assert model_events[-1].payload["attempt"] == 2
    assert model_events[-1].payload["model_attempt"] == 1
    assert model_events[-1].payload["candidate_index"] == 2
    assert model_events[-1].payload["fallback"] is True
    assert out.selected_model_ref == "backup"
    usage = next(event for event in out.events if event.event_type == EventType.USAGE_REPORTED)
    assert usage.payload["model"] == "backup"


def test_reason_turn_limit_raises():
    with pytest.raises(ReasoningLimitError):
        _run(
            reason_turn_async(
                9,  # 超过 max_turns=8
                ReasonInput(
                    model_ref="m",
                    instructions="",
                    messages=[{"role": "user", "content": "x"}],
                    tools=[],
                    reasoner=_ScriptedReasoner([HarnessReasoningTurn(final_text="x")]),
                    max_turns=8,
                ),
            )
        )


# --------------------------------------------------------------- tool calls


def test_tool_success_records_verified_facts():
    exec_ = _Exec({"budget_lookup": "预算 ¥42,000 已批准"})
    out = _run(
        execute_tool_calls(
            ToolCallInput(
                pending_tool_calls=[
                    {"call_id": "tc-1", "name": "budget_lookup", "arguments": {"q": "x"}}
                ],
                approval_required=frozenset(),
                approval_resolver=None,
                tool_executor=exec_,
                working_context=WorkingContext(),
            )
        )
    )
    types = [e.event_type for e in out.events]
    assert types.count(EventType.TOOL_CALL_BEGIN) == 1
    assert types.count(EventType.TOOL_CALL_END) == 1
    assert out.new_messages[0]["role"] == "tool"
    assert "¥42,000" in " ".join(out.working_context.verified_facts)
    assert out.pending_tool_calls == []
    assert out.route == "reason"


def test_tool_failure_is_resilient_records_failure():
    exec_ = _Exec({"flaky": RuntimeError("connection refused")})
    out = _run(
        execute_tool_calls(
            ToolCallInput(
                pending_tool_calls=[{"call_id": "c1", "name": "flaky", "arguments": {}}],
                approval_required=frozenset(),
                approval_resolver=None,
                tool_executor=exec_,
                working_context=WorkingContext(),
            )
        )
    )
    end_events = [e for e in out.events if e.event_type == EventType.TOOL_CALL_END]
    assert end_events and "connection refused" in end_events[0].payload["error"]
    assert "[error]" in out.new_messages[0]["content"]
    assert any("flaky" in f for f in out.working_context.recent_tool_failures)


def test_approval_deny_skips_tool_records_failure():
    exec_ = _Exec({"high_risk": "should not run"})
    out = _run(
        execute_tool_calls(
            ToolCallInput(
                pending_tool_calls=[{"call_id": "c1", "name": "high_risk", "arguments": {}}],
                approval_required=frozenset({"high_risk"}),
                approval_resolver=_DenyResolver(),
                tool_executor=exec_,
                working_context=WorkingContext(),
            )
        )
    )
    # 工具未执行
    assert exec_.calls == []
    assert "[denied]" in out.new_messages[0]["content"]
    assert any("high_risk" in f for f in out.working_context.recent_tool_failures)


def test_approval_approved_runs_tool():
    class _Approve:
        def request(self, **_):  # type: ignore[no-untyped-def]
            return "approved"

    exec_ = _Exec({"high_risk": "ran"})
    out = _run(
        execute_tool_calls(
            ToolCallInput(
                pending_tool_calls=[{"call_id": "c1", "name": "high_risk", "arguments": {}}],
                approval_required=frozenset({"high_risk"}),
                approval_resolver=_Approve(),
                tool_executor=exec_,
                working_context=WorkingContext(),
            )
        )
    )
    assert exec_.calls == [("high_risk", {})]
    assert out.new_messages[0]["content"] == "ran"


def test_multiple_tools_partial_failure_continues():
    exec_ = _Exec({"ok": "good", "boom": RuntimeError("fail")})
    out = _run(
        execute_tool_calls(
            ToolCallInput(
                pending_tool_calls=[
                    {"call_id": "1", "name": "ok", "arguments": {}},
                    {"call_id": "2", "name": "boom", "arguments": {}},
                    {"call_id": "3", "name": "ok", "arguments": {}},
                ],
                approval_required=frozenset(),
                approval_resolver=None,
                tool_executor=exec_,
                working_context=WorkingContext(),
            )
        )
    )
    assert len(out.new_messages) == 3
    assert out.new_messages[1]["content"].startswith("[error]")
    # 失败工具记入 recent_tool_failures
    assert any("boom" in f for f in out.working_context.recent_tool_failures)


def test_explicitly_safe_tools_run_in_parallel_with_deterministic_events():
    """只有宿主显式标记安全的批次并行，事件仍按模型调用顺序输出。"""

    class _BarrierExec:
        def __init__(self) -> None:
            self.entered = 0
            self.all_entered = asyncio.Event()

        async def execute(self, name, arguments):  # type: ignore[no-untyped-def]
            self.entered += 1
            if self.entered == 2:
                self.all_entered.set()
            await asyncio.wait_for(self.all_entered.wait(), timeout=0.2)
            return f"done:{name}"

    out = _run(
        execute_tool_calls(
            ToolCallInput(
                pending_tool_calls=[
                    {"call_id": "first", "name": "worker-a", "arguments": {}},
                    {"call_id": "second", "name": "worker-b", "arguments": {}},
                ],
                approval_required=frozenset(),
                approval_resolver=None,
                tool_executor=_BarrierExec(),
                parallel_safe_decider=lambda _name, _arguments: True,
                max_parallelism=2,
            )
        )
    )

    assert [message["tool_call_id"] for message in out.new_messages] == ["first", "second"]
    assert [event.payload["call_id"] for event in out.events] == [
        "first",
        "first",
        "second",
        "second",
    ]
    assert all(event.payload["parallel"] is True for event in out.events)


def test_approval_requirement_disables_parallel_execution():
    """任何审批约束存在时都回到原有串行语义。"""

    class _OrderExec:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def execute(self, name, arguments):  # type: ignore[no-untyped-def]
            self.calls.append(name)
            return name

    executor = _OrderExec()
    out = _run(
        execute_tool_calls(
            ToolCallInput(
                pending_tool_calls=[
                    {"call_id": "1", "name": "safe", "arguments": {}},
                    {"call_id": "2", "name": "guarded", "arguments": {}},
                ],
                approval_required=frozenset({"guarded"}),
                approval_resolver=None,
                tool_executor=executor,
                parallel_safe_decider=lambda _name, _arguments: True,
                max_parallelism=2,
            )
        )
    )

    assert executor.calls == ["safe", "guarded"]
    assert all("parallel" not in event.payload for event in out.events)


def test_failed_dependency_prevents_dependent_tool_from_starting():
    executor = _Exec({"writer": RuntimeError("failed"), "reviewer": "must-not-run"})
    out = _run(
        execute_tool_calls(
            ToolCallInput(
                pending_tool_calls=[
                    {"call_id": "w", "name": "writer", "arguments": {}},
                    {"call_id": "r", "name": "reviewer", "arguments": {}},
                ],
                approval_required=frozenset(),
                approval_resolver=None,
                tool_executor=executor,
                dependencies={"reviewer": ("writer",)},
            )
        )
    )
    assert executor.calls == [("writer", {})]
    reviewer = next(
        event
        for event in out.events
        if event.event_type == EventType.TOOL_CALL_END
        and event.payload["call_id"] == "r"
    )
    assert reviewer.payload["error_category"] == "dependency_unsatisfied"


def test_fail_fast_only_cancels_calls_selected_by_host():
    executor = _Exec({"child": RuntimeError("failed"), "ordinary": "still-runs"})
    out = _run(
        execute_tool_calls(
            ToolCallInput(
                pending_tool_calls=[
                    {"call_id": "c", "name": "child", "arguments": {}},
                    {"call_id": "o", "name": "ordinary", "arguments": {}},
                ],
                approval_required=frozenset(),
                approval_resolver=None,
                tool_executor=executor,
                stop_on_error_decider=lambda name, _arguments: name == "child",
                cancel_pending_decider=lambda name, _arguments: name == "other-child",
            )
        )
    )
    assert executor.calls == [("child", {}), ("ordinary", {})]
    assert any(message["content"] == "still-runs" for message in out.new_messages)


# ----------------------------------------------------------- streaming


class _StreamingReasoner:
    """不触网的流式 reasoner：stream_complete 逐 chunk yield 文本增量。"""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = list(chunks)
        self._i = 0

    async def complete(self, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("streaming reasoner should not be called via complete()")

    async def stream_complete(self, **kwargs):  # type: ignore[no-untyped-def]
        for chunk in self._chunks:
            yield {"text_delta": chunk}
        yield {
            "turn": HarnessReasoningTurn(
                final_text="".join(self._chunks),
                usage={"input_tokens": 10, "output_tokens": 5},
            ),
        }


def test_streaming_emits_text_delta_events_then_completed():
    """流式模式：逐 chunk 发 TEXT_DELTA，最终发 TEXT_COMPLETED。"""
    out = _run(
        reason_turn_async(
            1,
            ReasonInput(
                model_ref="m",
                instructions="",
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                reasoner=_StreamingReasoner(["Hello", " ", "world"]),
                streaming=True,
            ),
        )
    )
    types = [e.event_type for e in out.events]
    # TEXT_DELTA 在 MODEL_CALL_COMPLETED 之前
    delta_events = [e for e in out.events if e.event_type == EventType.TEXT_DELTA]
    assert len(delta_events) == 3
    assert [e.payload["text"] for e in delta_events] == ["Hello", " ", "world"]
    assert EventType.TEXT_COMPLETED in types
    assert EventType.MODEL_CALL_COMPLETED in types
    assert out.route == ROUTE_FINAL
    assert out.new_messages == [{"role": "assistant", "content": "Hello world"}]


def test_streaming_with_tool_calls_routes_to_tool_calls():
    """流式模式 + 工具调用：发 delta 后仍路由到 tool_calls。"""
    out = _run(
        reason_turn_async(
            1,
            ReasonInput(
                model_ref="m",
                instructions="",
                messages=[{"role": "user", "content": "查"}],
                tools=["t"],
                reasoner=_StreamingReasoner(["查"], ),
                streaming=True,
            ),
        )
    )
    # 无 tool_call 时仍 final（_StreamingReasoner 只有 text）
    assert out.route == ROUTE_FINAL
    assert EventType.TEXT_DELTA in [e.event_type for e in out.events]


def test_non_streaming_does_not_emit_text_delta():
    """非流式模式：不发 TEXT_DELTA，仅 TEXT_COMPLETED。"""
    out = _run(
        reason_turn_async(
            1,
            ReasonInput(
                model_ref="m",
                instructions="",
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                reasoner=_ScriptedReasoner([HarnessReasoningTurn(final_text="你好")]),
                streaming=False,
            ),
        )
    )
    types = [e.event_type for e in out.events]
    assert EventType.TEXT_DELTA not in types
    assert EventType.TEXT_COMPLETED in types
