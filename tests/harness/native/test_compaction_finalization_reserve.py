"""A summary cannot consume the final delivery call, including after recovery."""

import json

import pytest

from ksadk.harness.native.context_runtime import CompactionPolicy, ContextPlanner
from ksadk.harness.native.engine import NativeKernel
from ksadk.harness.native.model import normalize_response
from ksadk.harness.native.ports import ToolResourceUsage, ToolResult
from ksadk.harness.native.state import RunPhase
from ksadk.harness.stores.native_memory import MemoryRunStore
from ksadk.harness.stores.native_sqlite import SQLiteRunStore
from tests.harness.native.test_context_runtime import count_tokens, messages, plan, planner
from tests.harness.native.test_kernel import SCOPE, config


class ObservationTools:
    def __init__(self, *, descendant_calls=0, words=1400):
        self.keys = []
        self.content = "Complete original evidence " + "observation " * words + "END-OF-EVIDENCE"
        self.descendant_calls = descendant_calls

    async def execute(self, request):
        self.keys.append(request.operation_key)
        return ToolResult(
            status="ok",
            content=self.content,
            resource_usage=ToolResourceUsage(model_calls=self.descendant_calls),
        )


class DeliveryModel:
    def __init__(self, *, summary_finish="stop", observe_before_summary=False):
        self.requests = []
        self.summary_finish = summary_finish
        self.observe_before_summary = observe_before_summary

    async def complete(self, request):
        self.requests.append(request)
        identity = dict(request_id=request.request_id, attempt_id=request.attempt_id)
        if request.purpose == "compaction":
            return normalize_response(
                **identity,
                text="UNSEALED-SUMMARY",
                finish_reason=self.summary_finish,
            )
        if request.tools and self.observe_before_summary:
            observed = [message for message in request.messages if message.get("role") == "tool"]
            if observed:
                # This is a real task response after the entire receipt reached
                # ModelPort, not an injected assistant/snapshot acknowledgement.
                assert any(
                    json.loads(message["content"])
                    == {
                        "status": "ok",
                        "content": ObservationTools().content,
                    }
                    for message in observed
                )
                return normalize_response(
                    **identity,
                    text="The committed observation is available. ",
                    finish_reason="length",
                )
        if request.tools:
            return normalize_response(
                **identity, tool_calls=[{"call_id": "observe", "name": "write", "arguments": {}}]
            )
        return normalize_response(**identity, text="Delivered from the original evidence.")


def configured(*, calls=2, reserve=1):
    cfg = config(max_model_calls=calls, finalization_reserve_model_calls=reserve)
    return cfg.model_copy(
        update={
            "profile": cfg.profile.model_copy(
                update={
                    "context_window": 4000,
                    "max_output_tokens": 256,
                    "max_continuations": 1,
                }
            )
        }
    )


def context_planner():
    return ContextPlanner(
        count_tokens=count_tokens,
        tokenizer_id="finalization-test/v1",
        policy=CompactionPolicy(
            trigger_ratio=0.4,
            target_ratio=0.2,
            summary_max_tokens=128,
            min_model_turns_between=1,
        ),
    )


def make_kernel(model, tools, cfg, store=None):
    return NativeKernel(
        store=store or MemoryRunStore(),
        scope=SCOPE,
        model=model,
        tools=tools,
        configuration=cfg,
        context_planner=context_planner(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("descendant_calls", [0, 3])
@pytest.mark.parametrize("reserve", [1, 2])
async def test_reserved_delivery_uses_complete_context_above_soft_trigger(
    descendant_calls,
    reserve,
):
    model, tools = DeliveryModel(), ObservationTools(descendant_calls=descendant_calls)
    cfg = configured(calls=1 + descendant_calls + reserve, reserve=reserve)
    kernel = make_kernel(model, tools, cfg)
    run = kernel.start(goal="Collect evidence and deliver the result", session_id="s")
    result = await kernel.run_until_idle(run.run_id)
    assert result.phase == RunPhase.COMPLETED
    assert result.final_text == "Delivered from the original evidence."
    assert result.outcome == "unverified"  # Missing an independent verifier remains honest.
    assert result.model_calls == len(model.requests) == 2
    assert result.descendant_model_calls == descendant_calls
    assert result.model_calls + result.descendant_model_calls <= cfg.limits.max_model_calls
    assert [r.purpose for r in model.requests] == ["task", "task"]
    final = model.requests[-1]
    assert final.tools == () and final.turn_control.reason == "model_budget_reserved"
    assert tools.content in str(final.messages)
    assert tools.content in str(result.messages)
    assert len(tools.keys) == 1
    assert not any(e.kind == "context.compaction_started" for e in kernel.events(run.run_id))


@pytest.mark.asyncio
async def test_zero_reserve_exposes_fresh_observation_and_keeps_the_original_hard_limit():
    model, tools = DeliveryModel(), ObservationTools()
    cfg = configured(reserve=0)
    kernel = make_kernel(model, tools, cfg)
    run = kernel.start(goal="Collect evidence and deliver the result", session_id="s")
    result = await kernel.run_until_idle(run.run_id)
    assert result.reason == "model_budget_exhausted"
    assert result.model_calls == len(model.requests) == 2
    assert [r.purpose for r in model.requests] == ["task", "task"]
    assert tools.content in str(model.requests[1].messages)
    assert not any(e.kind == "context.compaction_started" for e in kernel.events(run.run_id))


@pytest.mark.asyncio
async def test_one_call_budget_is_available_for_delivery_without_tool_side_effects():
    model, tools = DeliveryModel(), ObservationTools()
    kernel = make_kernel(model, tools, configured(calls=1))
    run = kernel.start(goal="Answer using the available information", session_id="s")
    result = await kernel.run_until_idle(run.run_id)
    assert result.phase == RunPhase.COMPLETED and result.model_calls == 1
    assert model.requests[0].purpose == "task" and model.requests[0].tools == ()
    assert tools.keys == []


@pytest.mark.asyncio
async def test_real_overflow_does_not_spend_reserve_or_discard_evidence():
    # Stay below the independent 64 KiB tool-result governor, but exceed
    # the model window with the full host ledger and output reservation.
    model, tools = DeliveryModel(), ObservationTools(words=3500)
    kernel = make_kernel(model, tools, configured())
    run = kernel.start(goal="Collect evidence and deliver the result", session_id="s")
    result = await kernel.run_until_idle(run.run_id)
    assert result.reason == "context_finalization_budget_unavailable"
    assert result.model_calls == len(model.requests) == 1
    assert tools.content in str(result.messages)
    assert not result.final_text


@pytest.mark.parametrize("forced", [False, True])
def test_reserved_planning_does_not_override_hard_or_provider_window(forced):
    raw = messages(2 if forced else 8000)
    result = plan(
        planner(), messages=raw, tools=(), force_compaction=forced, allow_compaction=False
    )
    assert result.action == "blocked"
    assert result.reason == "context_finalization_budget_unavailable"
    assert result.prepared is None and result.request is None


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_finish", ["length", "provider_retryable_error"])
@pytest.mark.parametrize("cold", [False, True])
async def test_summary_retry_returns_reserve_to_delivery_without_sealing_partial_history(
    tmp_path,
    summary_finish,
    cold,
):
    model = DeliveryModel(summary_finish=summary_finish, observe_before_summary=True)
    tools = ObservationTools()
    # First-task observation is now protected. Its real length response makes
    # that group historical before the unchanged planner starts a summary.
    cfg = configured(calls=4)
    path = tmp_path / "reserve.sqlite"
    store = SQLiteRunStore(path) if cold else MemoryRunStore()
    kernel = make_kernel(model, tools, cfg, store)
    run = kernel.start(goal="Collect evidence and deliver the result", session_id="s")
    for _ in range(40):
        snapshot = await kernel.advance(run.run_id)
        if (
            snapshot.phase == RunPhase.COMPACTING
            and snapshot.model_calls == 3
            and snapshot.pending_intent is None
        ):
            break
    else:
        raise AssertionError("summary retry checkpoint was not reached")
    assert [r.purpose for r in model.requests] == ["task", "task", "compaction"]
    assert tools.content in str(model.requests[1].messages)
    assert snapshot.tool_calls == len(tools.keys) == 1
    assert snapshot.compaction_request is not None
    assert not snapshot.compaction_history.get("count", 0)
    prefix = kernel.events(run.run_id)
    keys = [e.payload["operation_key"] for e in prefix if e.kind == "effect.received"]
    receipts = {key: store.get_receipt(SCOPE, run.run_id, key) for key in keys}
    if cold:
        kernel.release(run.run_id)
        store.close()
        store = SQLiteRunStore(path)
        kernel = make_kernel(model, tools, cfg, store)
        assert kernel.attach(run.run_id) == snapshot
    result = await kernel.run_until_idle(run.run_id)
    assert result.phase == RunPhase.COMPLETED
    assert result.model_calls == len(model.requests) == cfg.limits.max_model_calls == 4
    assert [r.purpose for r in model.requests] == ["task", "task", "compaction", "task"]
    assert tools.content in str(model.requests[-1].messages)
    assert "UNSEALED-SUMMARY" not in str(model.requests[-1].messages)
    assert (result.configuration_digest, result.profile_digest, result.context_binding) == (
        snapshot.configuration_digest,
        snapshot.profile_digest,
        snapshot.context_binding,
    )
    assert result.compaction_request is None
    assert not result.compaction_history.get("count", 0)
    assert len(tools.keys) == len(set(tools.keys)) == 1
    assert kernel.events(run.run_id)[: len(prefix)] == prefix
    assert all(
        store.get_receipt(SCOPE, run.run_id, key) == receipt for key, receipt in receipts.items()
    )
    deferred = [e for e in kernel.events(run.run_id) if e.kind == "context.compaction_deferred"]
    assert len(deferred) == 1
    if cold:
        kernel.release(run.run_id)
        store.close()
