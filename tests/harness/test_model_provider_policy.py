from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from ksadk.harness.events import EventType
from ksadk.harness.loop.reason import ModelFailoverExhausted, ReasonInput, reason_turn_async
from ksadk.harness.model_provider import (
    ModelFailureAction,
    ModelFailureKind,
    classify_model_failure,
    decide_model_failure_action,
    retry_delay_ms,
)
from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.harness.spec import ModelProviderPolicy


class _HTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str = "provider failed") -> None:
        super().__init__(message)
        self.status_code = status_code


def _policy(**overrides: object) -> ModelProviderPolicy:
    return ModelProviderPolicy(
        initial_backoff_ms=0,
        max_backoff_ms=0,
        **overrides,
    )


def _input(reasoner: object, *, policy: ModelProviderPolicy) -> ReasonInput:
    return ReasonInput(
        model_ref="primary",
        fallback_model_refs=("backup",),
        provider_policy=policy,
        instructions="",
        messages=({"role": "user", "content": "x"},),
        tools=(),
        reasoner=reasoner,  # type: ignore[arg-type]
    )


def test_default_policy_retries_ten_times_and_reaches_sixty_seconds() -> None:
    policy = ModelProviderPolicy()

    assert policy.max_attempts_per_model == 10
    assert policy.total_attempt_budget == 20
    assert policy.max_backoff_ms == 60_000
    delays = [retry_delay_ms(policy, model_attempt=attempt) for attempt in range(1, 10)]
    assert delays == [1_000, 2_000, 3_000, 5_000, 8_000, 13_000, 22_000, 36_000, 60_000]
    assert delays == sorted(delays)


def test_default_policy_stops_after_ten_provider_attempts() -> None:
    class _Reasoner:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, **_: object) -> HarnessReasoningTurn:
            self.calls += 1
            raise _HTTPError(429)

    reasoner = _Reasoner()
    policy = ModelProviderPolicy(initial_backoff_ms=0, max_backoff_ms=0)

    with pytest.raises(ModelFailoverExhausted) as caught:
        asyncio.run(reason_turn_async(1, _input(reasoner, policy=policy)))

    # Ten attempts are made against the primary model, then the fallback can
    # still run because the default total budget reserves a second ten-attempt
    # window. Both providers therefore obey the same visible 10-attempt cap.
    assert reasoner.calls == 20
    failed = [
        event for event in caught.value.events if event.event_type == EventType.MODEL_CALL_FAILED
    ]
    assert len(failed) == 20
    assert failed[8].payload["max_attempts"] == 10
    assert failed[8].payload["model_attempt"] == 9
    assert failed[8].payload["action"] == "retry_same_model"
    assert failed[9].payload["action"] == "failover"


def test_classifier_prefers_structured_status_and_blocks_permanent_errors() -> None:
    assert classify_model_failure(_HTTPError(429)).kind == ModelFailureKind.RATE_LIMIT
    auth = classify_model_failure(_HTTPError(401))
    assert auth.kind == ModelFailureKind.AUTHENTICATION
    assert (
        decide_model_failure_action(
            auth,
            policy=_policy(),
            model_attempt=1,
            total_attempt=1,
            has_fallback=True,
        )
        == ModelFailureAction.ABORT
    )


def test_http_400_context_overflow_enters_context_recovery_not_failover() -> None:
    failure = classify_model_failure(
        _HTTPError(400, "maximum context length exceeded: too many tokens")
    )
    assert failure.kind == ModelFailureKind.CONTEXT_LENGTH
    assert (
        decide_model_failure_action(
            failure,
            policy=_policy(),
            model_attempt=1,
            total_attempt=1,
            has_fallback=True,
        )
        == ModelFailureAction.RECOVER_CONTEXT
    )


def test_transient_failure_retries_same_model_before_failover() -> None:
    class _Reasoner:
        def __init__(self) -> None:
            self.models: list[str] = []

        async def complete(self, *, model: str, **_: object) -> HarnessReasoningTurn:
            self.models.append(model)
            if len(self.models) == 1:
                raise _HTTPError(429)
            return HarnessReasoningTurn(final_text="ok")

    reasoner = _Reasoner()
    result = asyncio.run(
        reason_turn_async(1, _input(reasoner, policy=_policy(max_attempts_per_model=2)))
    )
    assert reasoner.models == ["primary", "primary"]
    failed = next(
        event for event in result.events if event.event_type == EventType.MODEL_CALL_FAILED
    )
    assert failed.payload["failure_category"] == "rate_limit"
    assert failed.payload["action"] == "retry_same_model"
    completed = next(
        event for event in result.events if event.event_type == EventType.MODEL_CALL_COMPLETED
    )
    assert completed.payload["attempt"] == 2
    assert completed.payload["model_attempt"] == 2
    assert completed.payload["fallback"] is False


def test_retry_failure_is_delivered_live_before_the_next_attempt() -> None:
    timeline: list[str] = []

    class _Reasoner:
        async def complete(self, **_: object) -> HarnessReasoningTurn:
            timeline.append("model")
            if timeline.count("model") == 1:
                raise _HTTPError(429)
            return HarnessReasoningTurn(final_text="ok")

    inp = replace(
        _input(_Reasoner(), policy=_policy(max_attempts_per_model=2)),
        live_event_sink=lambda event: timeline.append(event.event_type),
    )

    asyncio.run(reason_turn_async(1, inp))

    assert timeline == [
        "model.call.started",
        "model",
        "model.call.failed",
        "model.call.started",
        "model",
    ]


def test_retry_discards_failed_stream_text_before_chat_projection() -> None:
    live_events = []

    class _Reasoner:
        def __init__(self) -> None:
            self.attempt = 0

        async def stream_complete(self, **_: object):
            self.attempt += 1
            if self.attempt == 1:
                yield {"text_delta": "这段失败输出不能显示"}
                raise _HTTPError(429)
            yield {"text_delta": "最终答案"}
            yield {"turn": HarnessReasoningTurn(final_text="最终答案")}

    inp = replace(
        _input(_Reasoner(), policy=_policy(max_attempts_per_model=2)),
        streaming=True,
        live_event_sink=live_events.append,
    )

    result = asyncio.run(reason_turn_async(1, inp))

    deltas = [event for event in result.events if event.event_type == EventType.TEXT_DELTA]
    assert [event.payload["text"] for event in deltas] == ["最终答案"]
    assert not any(event.event_type == EventType.TEXT_DELTA for event in live_events)


def test_transient_failure_exhausts_model_then_uses_fallback() -> None:
    class _Reasoner:
        def __init__(self) -> None:
            self.models: list[str] = []

        async def complete(self, *, model: str, **_: object) -> HarnessReasoningTurn:
            self.models.append(model)
            if model == "primary":
                raise _HTTPError(503)
            return HarnessReasoningTurn(final_text="backup ok")

    reasoner = _Reasoner()
    result = asyncio.run(
        reason_turn_async(1, _input(reasoner, policy=_policy(max_attempts_per_model=2)))
    )
    assert reasoner.models == ["primary", "primary", "backup"]
    failed = [event for event in result.events if event.event_type == EventType.MODEL_CALL_FAILED]
    assert [event.payload["action"] for event in failed] == [
        "retry_same_model",
        "failover",
    ]
    assert result.selected_model_ref == "backup"


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (TimeoutError("provider timed out"), "timeout"),
        (ConnectionError("connection reset by peer"), "transport"),
    ],
)
def test_timeout_and_disconnect_follow_retry_then_fallback_policy(
    error: Exception, category: str
) -> None:
    class _Reasoner:
        def __init__(self) -> None:
            self.models: list[str] = []

        async def complete(self, *, model: str, **_: object) -> HarnessReasoningTurn:
            self.models.append(model)
            if model == "primary":
                raise error
            return HarnessReasoningTurn(final_text="recovered")

    reasoner = _Reasoner()
    result = asyncio.run(
        reason_turn_async(1, _input(reasoner, policy=_policy(max_attempts_per_model=2)))
    )
    assert reasoner.models == ["primary", "primary", "backup"]
    failed = [event for event in result.events if event.event_type == EventType.MODEL_CALL_FAILED]
    assert {event.payload["failure_category"] for event in failed} == {category}
    assert [event.payload["action"] for event in failed] == ["retry_same_model", "failover"]


@pytest.mark.parametrize("status_code", [400, 403, 404])
def test_permanent_client_errors_never_try_fallback(status_code: int) -> None:
    class _Reasoner:
        def __init__(self) -> None:
            self.models: list[str] = []

        async def complete(self, *, model: str, **_: object) -> HarnessReasoningTurn:
            self.models.append(model)
            raise _HTTPError(status_code, "permanent client error")

    reasoner = _Reasoner()
    with pytest.raises(ModelFailoverExhausted) as caught:
        asyncio.run(reason_turn_async(1, _input(reasoner, policy=_policy())))
    assert reasoner.models == ["primary"]
    assert caught.value.events[-1].payload["action"] == "abort"


def test_authentication_failure_does_not_try_fallback() -> None:
    class _Reasoner:
        def __init__(self) -> None:
            self.models: list[str] = []

        async def complete(self, *, model: str, **_: object) -> HarnessReasoningTurn:
            self.models.append(model)
            raise _HTTPError(401, "invalid credential")

    reasoner = _Reasoner()
    with pytest.raises(ModelFailoverExhausted) as caught:
        asyncio.run(reason_turn_async(1, _input(reasoner, policy=_policy())))
    assert reasoner.models == ["primary"]
    failed = caught.value.events[-1]
    assert failed.payload["failure_category"] == "authentication"
    assert failed.payload["action"] == "abort"
    assert caught.value.stop_reason.value == "abort"


def test_provider_failure_events_and_exception_redact_credentials() -> None:
    fake_api_key = "sk-" + "testsecret123"

    class _Reasoner:
        async def complete(self, **_: object) -> HarnessReasoningTurn:
            raise RuntimeError(f"Authorization=Bearer secret-token api_key={fake_api_key}")

    with pytest.raises(ModelFailoverExhausted) as caught:
        asyncio.run(reason_turn_async(1, _input(_Reasoner(), policy=_policy())))
    event_error = caught.value.events[-1].payload["error"]
    assert "secret-token" not in event_error
    assert fake_api_key not in event_error
    assert "secret-token" not in str(caught.value)
    assert fake_api_key not in str(caught.value)


def test_total_attempt_budget_caps_retry_and_failover_chain() -> None:
    class _Reasoner:
        def __init__(self) -> None:
            self.models: list[str] = []

        async def complete(self, *, model: str, **_: object) -> HarnessReasoningTurn:
            self.models.append(model)
            raise _HTTPError(503)

    reasoner = _Reasoner()
    with pytest.raises(ModelFailoverExhausted) as caught:
        asyncio.run(
            reason_turn_async(
                1,
                _input(
                    reasoner,
                    policy=_policy(max_attempts_per_model=5, total_attempt_budget=2),
                ),
            )
        )
    assert reasoner.models == ["primary", "primary"]
    assert caught.value.events[-1].payload["action"] == "budget_exhausted"


def test_http_400_input_token_limit_is_context_length_for_real_gateways() -> None:
    # 真实网关实测文案：HTTP 400 "input token limit is 1048576"。
    failure = classify_model_failure(_HTTPError(400, "input token limit is 1048576"))
    assert failure.kind == ModelFailureKind.CONTEXT_LENGTH
    assert (
        decide_model_failure_action(
            failure,
            policy=_policy(),
            model_attempt=1,
            total_attempt=1,
            has_fallback=True,
        )
        == ModelFailureAction.RECOVER_CONTEXT
    )


@pytest.mark.parametrize(
    "message",
    (
        "Prompt exceeds max length",
        "prompt exceeds maximum length",
        "Prompt is too long",
    ),
)
def test_http_400_prompt_length_variants_are_context_length(message: str) -> None:
    failure = classify_model_failure(_HTTPError(400, message))
    assert failure.kind == ModelFailureKind.CONTEXT_LENGTH
