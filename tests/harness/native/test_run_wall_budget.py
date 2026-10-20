"""A Run's pinned active execution deadline bounds every effect and survives pause."""

import asyncio

import pytest

from ksadk.harness.native.engine import NativeKernel
from ksadk.harness.native.model import ModelTurnResult, normalize_response
from ksadk.harness.native.ports import ToolResult, digest
from ksadk.harness.native.state import RunCommand, RunPhase
from ksadk.harness.stores.native_memory import MemoryRunStore
from ksadk.harness.stores.native_sqlite import SQLiteRunStore
from tests.harness.native.test_kernel import SCOPE, ScriptedModel, call, config


def make(model, *, tools=None, wall=0.05):
    return NativeKernel(
        scope=SCOPE, store=MemoryRunStore(), model=model, tools=tools,
        configuration=config(max_wall_seconds=wall, effect_timeout_seconds=1),
    )


def test_optional_wall_budget_keeps_old_configuration_identity_and_pins_a_limit():
    # The pre-a52ead0de default had max_model_calls=32, max_tool_calls=64.
    # After a52ead0de the default is None (unbounded). The old finite identity
    # must retain its exact digest; the new unbounded default must differ.
    old = config(max_model_calls=32, max_tool_calls=64)
    payload = old.model_dump(mode="json")
    payload["limits"].pop("max_wall_seconds", None)
    # Apply the same canonical compatibility rules to the old representation.
    assert digest(old) == digest(type(old).model_validate(payload))
    assert digest(old) == "10863851cbab5142a053efb166413977294f3d1370f875034b2883a8caaf84bf"
    assert digest(old) != digest(config(max_wall_seconds=1))
    assert config(max_wall_seconds=1).limits.max_wall_seconds == 1
    assert "max_wall_seconds" not in old.model_dump(mode="json")["limits"]
    assert config(max_wall_seconds=1).model_dump(mode="json")["limits"]["max_wall_seconds"] == 1
    # The new unbounded default (max_model_calls=None, max_tool_calls=None) has a
    # different digest from the old finite identity — they are distinct configs.
    new_default = config()
    assert digest(new_default) != digest(old)
    assert new_default.limits.max_model_calls is None
    assert new_default.limits.max_tool_calls is None


@pytest.mark.asyncio
async def test_run_deadline_cancels_an_unsealed_model_without_provider_retry(monkeypatch):
    # Exercise expiry while the effect is running, independent of host/GC time
    # before dispatch. The effect still waits on the real 50ms deadline.
    now = [0.0]
    monkeypatch.setattr("ksadk.harness.native.run_clock.monotonic", lambda: now[0])

    class HeldModel:
        calls = 0
        cancelled = False

        async def complete(self, _request):
            self.calls += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                now[0] = 0.05
                raise

    model = HeldModel()
    kernel = make(model)
    kernel.start(goal="Complete the task", session_id="s", run_id="r")
    result = await asyncio.wait_for(kernel.run_until_idle("r"), timeout=1)
    assert result.phase.terminal
    assert result.reason == "wall_time_hard_limit"
    assert result.outcome == "budget_exhausted"
    assert model.calls == result.model_calls == 1
    assert model.cancelled
    assert result.execution_elapsed_seconds >= 0.05
    assert not result.final_text
    assert any(event.kind == "budget.exhausted" for event in kernel.events("r"))


@pytest.mark.asyncio
async def test_run_deadline_preserves_unknown_cancellation_resistant_model_result(monkeypatch):
    # This case expires an already-running effect. Startup/GC time must not
    # consume its budget before the cancellation-resistant model is dispatched.
    now = [0.0]
    monkeypatch.setattr("ksadk.harness.native.run_clock.monotonic", lambda: now[0])
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class ResistantModel:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                now[0] = 0.05
                await release.wait()
                finished.set()
                return normalize_response(
                    request_id=request.request_id, attempt_id=request.attempt_id, text="late",
                )

    model = ResistantModel()
    kernel = make(model)
    kernel.start(goal="Answer", session_id="s", run_id="r")
    try:
        result = await asyncio.wait_for(kernel.run_until_idle("r"), timeout=1)
        assert result.phase == RunPhase.INTERRUPTED and result.outcome == "unverified"
        assert result.reason == "effect_outcome_unknown" and model.calls == 1
        before = kernel.events("r")
        assert any(e.kind == "budget.exhausted" for e in before)
        assert sum(e.kind == "usage.unavailable" for e in before) == 1
    finally:
        release.set()
        if entered.is_set():
            await asyncio.wait_for(finished.wait(), timeout=1)
    await asyncio.sleep(0)
    assert kernel.events("r") == before and not kernel.snapshot("r").final_text


@pytest.mark.asyncio
async def test_wall_deadline_retains_one_uncertain_tool_effect_without_replay(tmp_path):
    class HeldTool:
        calls = 0

        async def execute(self, _request):
            self.calls += 1
            (tmp_path / "effect.txt").write_text("once")
            await asyncio.Event().wait()
            return ToolResult(status="ok", content={})

    tool = HeldTool()
    kernel = make(ScriptedModel(call()), tools=tool)
    kernel.start(goal="Write once", session_id="s", run_id="r")
    result = await asyncio.wait_for(kernel.run_until_idle("r"), timeout=1)
    assert result.reason == "wall_time_hard_limit"
    assert result.outcome != "verified"
    assert (tmp_path / "effect.txt").read_text() == "once"
    assert tool.calls == 1
    await kernel.run_until_idle("r")
    assert tool.calls == 1
    receipts = [e for e in kernel.events("r")
                if e.kind == "effect.received" and e.payload.get("kind") == "tool"]
    assert len(receipts) == 1 and receipts[0].payload["status"] == "uncertain"


@pytest.mark.asyncio
async def test_pause_wait_does_not_spend_the_active_execution_budget():
    kernel = make(ScriptedModel({"text": "done"}), wall=0.5)
    kernel.start(goal="Answer", session_id="s", run_id="r")
    kernel.submit("r", RunCommand(command_id="pause", kind="pause"))
    assert (await kernel.advance("r")).phase == RunPhase.PAUSED
    paused = kernel.snapshot("r").execution_elapsed_seconds
    await asyncio.sleep(0.6)
    assert kernel.snapshot("r").execution_elapsed_seconds == paused
    kernel.submit("r", RunCommand(command_id="resume", kind="resume"))
    result = await asyncio.wait_for(kernel.run_until_idle("r"), timeout=1)
    assert result.phase.terminal and result.reason != "wall_time_hard_limit"
    assert result.execution_elapsed_seconds < 0.5


@pytest.mark.asyncio
async def test_approval_wait_does_not_spend_the_active_execution_budget(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("ksadk.harness.native.run_clock.monotonic", lambda: now[0])

    class ApprovingTool:
        writes = 0

        async def execute(self, request):
            if request.interaction is None:
                return ToolResult(status="approval_required", interaction_id="approve")
            self.writes += 1
            return ToolResult(status="ok", content={"written": True})

    tools = ApprovingTool()
    kernel = make(ScriptedModel(call(), {"text": "done"}), tools=tools, wall=1)
    kernel.start(goal="Write", session_id="s", run_id="r")
    paused = await kernel.run_until_idle("r")
    assert paused.phase == RunPhase.AWAITING_INTERACTION and tools.writes == 0
    now[0] = 100
    kernel.submit("r", RunCommand(
        command_id="approve", kind="submit_interaction",
        payload={"interaction_id": paused.pending_interaction, "decision": "allow"},
    ))
    result = await kernel.run_until_idle("r")
    assert result.phase.terminal and result.reason != "wall_time_hard_limit"
    assert result.execution_elapsed_seconds == 0 and tools.writes == 1


@pytest.mark.asyncio
async def test_steering_keeps_the_consumed_budget(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("ksadk.harness.native.run_clock.monotonic", lambda: now[0])
    model = ScriptedModel({"text": "never dispatch"})
    kernel = make(model, wall=1)
    kernel.start(goal="Answer", session_id="s", run_id="r")
    now[0] = 0.75
    kernel.steer("r", text="Answer the correction", command_id="steer", expected_goal_revision=0)
    updated = await kernel.advance("r")
    assert updated.goal_revision == 1 and updated.execution_elapsed_seconds == 0.75
    now[0] = 1.01
    result = await kernel.run_until_idle("r")
    assert result.reason == "wall_time_hard_limit" and not model.requests


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
@pytest.mark.asyncio
async def test_detach_attach_retains_committed_consumption(storage, tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("ksadk.harness.native.run_clock.monotonic", lambda: now[0])
    store = MemoryRunStore() if storage == "memory" else SQLiteRunStore(tmp_path / "runs.db")
    configuration = config(max_wall_seconds=1)
    model = ScriptedModel({"text": "never dispatch"})
    try:
        kernel = NativeKernel(scope=SCOPE, store=store, model=model, configuration=configuration)
        kernel.start(goal="Answer", session_id="s", run_id="r")
        now[0] = 0.8
        before = await kernel.advance("r")
        assert before.execution_elapsed_seconds == 0.8
        kernel.release("r")
        if storage == "sqlite":
            store.close()
            store = SQLiteRunStore(tmp_path / "runs.db")
        now[0] = 100
        successor = NativeKernel(scope=SCOPE, store=store, model=model, configuration=configuration)
        successor.attach("r")
        assert successor.snapshot("r").execution_elapsed_seconds == 0.8
        now[0] = 100.21
        result = await successor.run_until_idle("r")
        assert result.reason == "wall_time_hard_limit" and not model.requests
        assert result.execution_elapsed_seconds == pytest.approx(1.01)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_deadline_does_not_treat_an_unfinished_verifier_as_success():
    class HeldVerifier:
        entered = False
        cancelled = False

        async def verify(self, _request):
            self.entered = True
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    verifier = HeldVerifier()
    kernel = NativeKernel(
        scope=SCOPE, store=MemoryRunStore(), model=ScriptedModel({"text": "done"}),
        verifier=verifier,
        configuration=config(max_wall_seconds=0.05, effect_timeout_seconds=1),
    )
    kernel.start(goal="Answer and verify", session_id="s", run_id="r")
    result = await asyncio.wait_for(kernel.run_until_idle("r"), timeout=1)
    assert verifier.entered and verifier.cancelled
    assert result.reason == "wall_time_hard_limit" and result.outcome != "verified"
    receipts = [e for e in kernel.events("r") if e.kind == "effect.received"
                and e.payload.get("kind") == "verify"]
    assert len(receipts) == 1 and receipts[0].payload["status"] == "uncertain"


@pytest.mark.asyncio
async def test_expired_run_does_not_dispatch_a_prepared_effect():
    model = ScriptedModel({"text": "late"})
    kernel = make(model)
    kernel.start(goal="Answer", session_id="s", run_id="r")
    while kernel.snapshot("r").pending_intent is None:
        await kernel.advance("r")
    await asyncio.sleep(0.06)
    result = await asyncio.wait_for(kernel.run_until_idle("r"), timeout=1)
    assert result.reason == "wall_time_hard_limit"
    assert result.model_calls == 0 and not model.requests
    receipts = [e for e in kernel.events("r") if e.kind == "effect.received"]
    assert len(receipts) == 1 and receipts[0].payload["status"] == "failed"


@pytest.mark.asyncio
async def test_expired_budget_does_not_hide_a_lost_running_effect():
    model = ScriptedModel({"text": "never dispatch"})
    kernel = make(model)
    kernel.start(goal="Answer", session_id="s", run_id="r")
    while (snapshot := kernel.snapshot("r")).pending_intent is None:
        await kernel.advance("r")
    kernel.store.claim_intent(
        SCOPE, "r", snapshot.pending_intent, fencing_token=kernel.fencing_token("r"),
    )
    await asyncio.sleep(0.06)
    result = await asyncio.wait_for(kernel.run_until_idle("r"), timeout=1)
    assert result.phase == RunPhase.INTERRUPTED
    assert result.outcome == "unverified" and result.reason == "effect_outcome_unknown"
    assert result.model_calls == 1 and not model.requests
    receipts = [e for e in kernel.events("r") if e.kind == "effect.received"]
    assert len(receipts) == 1 and receipts[0].payload["status"] == "uncertain"
    assert any(e.kind == "budget.exhausted" for e in kernel.events("r"))


@pytest.mark.asyncio
async def test_provider_backoff_cannot_outlive_the_remaining_run_budget():
    class LimitedModel:
        calls = 0

        async def complete(self, request):
            self.calls += 1
            return ModelTurnResult(
                request_id=request.request_id, attempt_id=request.attempt_id,
                response_kind="provider_retryable_error", failure_kind="rate_limit",
                retry_after_seconds=30,
            )

    model = LimitedModel()
    kernel = make(model)
    kernel.start(goal="Answer", session_id="s", run_id="r")
    result = await asyncio.wait_for(kernel.run_until_idle("r"), timeout=1)
    assert result.reason == "wall_time_hard_limit" and model.calls == 1
