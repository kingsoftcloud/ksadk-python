"""Real canonical-contract conformance with scripted ports, not model-quality evidence."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import replace

import pytest

from ksadk.events.canonical import (
    InteractionRequested,
    ItemCompleted,
    ItemStarted,
    RunCompleted,
    UsageReported,
    dump_runtime_event,
    parse_runtime_event,
)
from ksadk.events.content import ToolCallContent, ToolResultContent
from ksadk.events.reducer import StreamReducer
from ksadk.harness.artifact_adapters.native_tool import ARTIFACT_READ_DEFINITION
from ksadk.harness.native.adapters import StrictToolExecutor, ToolBinding
from ksadk.harness.native.completion import AcceptanceItem, CompletionCandidate, TaskContract
from ksadk.harness.native.context_runtime import ContextPlanner
from ksadk.harness.native.model import (
    ModelProfile,
    ModelStreamEvent,
    ModelToolCall,
    normalize_response,
)
from ksadk.harness.native.ports import (
    RunConfiguration,
    ToolDefinition,
    ToolResult,
    VerificationBundle,
)
from ksadk.harness.native.state import RunScope
from ksadk.harness.native.task_progress import TODO_WRITE_DEFINITION
from ksadk.harness.native_facade import NativeHarnessEngine
from ksadk.harness.native_factory import (
    HarnessExecutionEngineFactory,
    NativeHarnessSettings,
    NativeRuntimeBindings,
)
from ksadk.harness.native_runtime import NativeRuntimeAdapter, TaskContractInput
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.harness.stores.native_memory import MemoryRunStore
from ksadk.harness.stores.native_sqlite import SQLiteRunStore
from ksadk.kernel.contracts import SteerPayload
from ksadk.kernel.errors import UnsupportedControlError
from ksadk.runtime import (
    CancelResult,
    ResumePayload,
    ResumeTarget,
    RuntimeLaunchContext,
    RuntimeRegistry,
    StartRequest,
    build_default_runtime_registry,
)
from ksadk.studio.service import StudioService

SCOPE = RunScope(tenant_id="tenant", workspace_id="workspace", user_id="user", agent_id="agent")


class Model:
    def __init__(self, *responses):
        self.responses = iter(responses or ({"text": "answer"},))
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return normalize_response(
            request_id=request.request_id, attempt_id=request.attempt_id, **next(self.responses)
        )


class Tools:
    async def execute(self, request):
        return ToolResult(status="ok", content="fixture")


@pytest.mark.asyncio
async def test_native_canonical_tool_trace_keeps_identity_without_private_arguments_or_result():
    private_argument = "private-argument-placeholder"
    private_result = "private-result-placeholder"

    class PrivateTool:
        async def execute(self, _request):
            return ToolResult(status="ok", content={"private": private_result})

    runtime = adapter(
        bindings(
            model=Model(
                {
                    "tool_calls": (
                        ModelToolCall(
                            call_id="read-1",
                            name="read",
                            arguments={"credential": private_argument},
                        ),
                    )
                },
                {"text": "done"},
            ),
            tools=PrivateTool(),
            definitions=(
                ToolDefinition(name="read", description="Read fixture", parameters={}),
            ),
        )
    )
    handle = await start(runtime)
    events = [event async for event in runtime.stream(handle)]
    exported = str([dump_runtime_event(event) for event in events])
    assert private_argument not in exported and private_result not in exported
    completed = [
        event
        for event in events
        if isinstance(event, ItemCompleted) and event.item_kind == "tool_call"
    ]
    assert len(completed) == 1
    call = next(part for part in completed[0].snapshot.parts if isinstance(part, ToolCallContent))
    result = next(
        part for part in completed[0].snapshot.parts if isinstance(part, ToolResultContent)
    )
    assert (call.call_id, call.name, call.arguments) == (
        "read-1", "read", {"redacted": True}
    )
    assert result.result == {"status": "ok"}
    assert runtime.replay(handle) == tuple(events)


class Verifier:
    async def verify(self, request):
        return VerificationBundle(
            candidate=CompletionCandidate(
                candidate_id=request.candidate_id,
                final_text=request.final_text,
                subject_digest="fixture",
                subject_context_digest=request.subject_context_digest,
                environment_digest="fixture",
            ),
            report=None,
        )


class Artifacts:
    async def write(self, request):
        raise AssertionError("fixture does not write an artifact")

    async def read(self, request):
        raise AssertionError("fixture does not read an artifact")


def bindings(
    *,
    scope=SCOPE,
    model=None,
    store=None,
    streaming=False,
    tools=None,
    definitions=(),
    durable=False,
    artifacts=None,
):
    configuration = RunConfiguration(
        profile=ModelProfile(
            profile_id="fixture", profile_version="1", provider_family="test", streaming=streaming
        ),
        contract=TaskContract(revision="1", items=()),
        tools=definitions,
        instructions="Fixture.",
    )
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://agent@1",
        model=ModelBinding(profile_ref="model-profile://fixture@1"),
        prompt=PromptSpec(instructions="Fixture."),
    )
    return NativeRuntimeBindings(
        scope=scope,
        spec=spec,
        configuration=configuration,
        model=model or Model(),
        tools=tools or Tools(),
        verifier=Verifier(),
        context_planner=ContextPlanner(
            count_tokens=lambda s: len(s), tokenizer_id="fixture-counter"
        ),
        store=store or MemoryRunStore(),
        durable=durable,
        artifacts=artifacts,
    )


def adapter(bound):
    engine = NativeHarnessEngine(
        scope=bound.scope,
        configuration=bound.configuration,
        model=bound.model,
        tools=bound.tools,
        verifier=bound.verifier,
        store=bound.store,
        context_planner=bound.context_planner,
    )
    return NativeRuntimeAdapter(
        bound.spec,
        engine=engine,
        scope=bound.scope,
        configuration=bound.configuration,
        store=bound.store,
        durable=bound.durable,
    )


def request(**changes):
    return StartRequest(
        **{
            "input": "Do the fixture.",
            "user_id": "user",
            "session_id": "session",
            "agent_id": "agent",
            **changes,
        }
    )


async def start(runtime, **changes):
    with pytest.warns(UserWarning, match="legacy defaults"):
        return await runtime.start(request(**changes))


def reduce_events(events):
    reducer = StreamReducer()
    for event in events:
        reducer.apply(parse_runtime_event(dump_runtime_event(event)))
    return reducer


@pytest.mark.asyncio
async def test_runtime_registry_keeps_harness_name_and_canonical_ledger_projection(tmp_path):
    bound = bindings()
    factory = HarnessExecutionEngineFactory(
        settings=NativeHarnessSettings(native_harness_enabled=True, engine_kind="native"),
        native_bindings=bound,
    )
    registry = RuntimeRegistry()
    registry.register("harness", factory)
    runtime = registry.create(RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path))
    assert runtime.runtime.runtime_type == "harness"
    handle = await start(runtime)
    assert handle.runtime_type == "harness" and handle.native_ref["engine_kind"] == "native"
    events = [e async for e in runtime.stream(handle)]
    projection = reduce_events(events).snapshot()
    assert projection.status == "completed"
    assert len(projection.output_refs) == 1
    assert (
        next(e for e in events if isinstance(e, RunCompleted)).source.metadata["outcome"]
        == "unverified"
    )
    assert [dump_runtime_event(e) for e in runtime.replay(handle)] == [
        dump_runtime_event(e) for e in events
    ]
    assert runtime.projection(handle) == projection
    assert [e async for e in runtime.stream(handle)] == []
    await runtime.close(handle)
    final_snapshot = bound.store.load(SCOPE, handle.run_id)
    assert final_snapshot.phase.terminal
    assert final_snapshot.workspace_observation_status == "captured"
    assert final_snapshot.workspace_revision.startswith("sha256:")


@pytest.mark.asyncio
async def test_native_adapter_close_all_releases_activation_owned_handles():
    runtime = adapter(bindings())
    first = await start(runtime, session_id="session-one")
    second = await runtime.start(request(session_id="session-two"))

    await runtime.close_all()

    assert not runtime.is_handle_attached(first)
    assert not runtime.is_handle_attached(second)
    await runtime.close_all()


@pytest.mark.asyncio
async def test_native_factory_installs_session_progress_tool_in_model_contract(tmp_path):
    bound = bindings()
    runtime = HarnessExecutionEngineFactory(
        settings=NativeHarnessSettings(native_harness_enabled=True, engine_kind="native"),
        native_bindings=bound,
    )(RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path))
    handle = await start(runtime)
    await _drain(runtime, handle)
    assert bound.model.requests[0].tools[-1] == TODO_WRITE_DEFINITION


@pytest.mark.asyncio
async def test_native_factory_installs_artifact_reader_only_with_host_store(tmp_path):
    bound = bindings(artifacts=Artifacts())
    runtime = HarnessExecutionEngineFactory(
        settings=NativeHarnessSettings(native_harness_enabled=True, engine_kind="native"),
        native_bindings=bound,
    )(RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path))
    handle = await start(runtime)
    await _drain(runtime, handle)
    assert bound.model.requests[0].tools[-2:] == (
        TODO_WRITE_DEFINITION,
        ARTIFACT_READ_DEFINITION,
    )


@pytest.mark.asyncio
async def test_native_usage_deltas_accumulate_once_and_replay_does_not_double_count():
    tool = ToolDefinition(name="read", description="fixture", parameters={"type": "object"})
    model = Model(
        {
            "tool_calls": (ModelToolCall(call_id="call", name="read", arguments={}),),
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
        {"text": "answer", "usage": {"input_tokens": 20, "output_tokens": 3}},
    )
    runtime = adapter(bindings(model=model, definitions=(tool,)))
    handle = await start(runtime)
    events = [e async for e in runtime.stream(handle)]
    usage = [e for e in events if isinstance(e, UsageReported)]
    assert [(e.input_tokens, e.output_tokens) for e in usage] == [(10, 2), (30, 5)]
    if "mode" in UsageReported.model_fields:
        assert all(e.mode == "cumulative" for e in usage)
    reducer = reduce_events(events)
    for e in events:
        reducer.apply(e)
    assert reducer.snapshot().usage.total_tokens == 35
    terminal = next(e for e in events if isinstance(e, RunCompleted))
    assert terminal.source.metadata["usage_completeness"] == "complete"
    assert terminal.source.metadata["usage_unavailable_attempts"] == 0


@pytest.mark.asyncio
async def test_read_only_callback_exception_returns_to_model_for_repair():
    definition = ToolDefinition(
        name="read", description="fixture", parameters={"type": "object"}, read_only=True
    )
    attempts = []

    async def callback(_request):
        attempts.append("read")
        raise OSError("private-path-placeholder")

    async def authorize(_request):
        return "allow"

    model = Model(
        {"tool_calls": (ModelToolCall(call_id="call", name="read", arguments={}),)},
        {"text": "The read failed; I cannot claim the file was inspected."},
    )
    tools = StrictToolExecutor(
        scope=SCOPE,
        tools=(ToolBinding(definition, callback),),
        authorize=authorize,
    )
    runtime = adapter(bindings(model=model, tools=tools, definitions=(definition,)))
    handle = await start(runtime)
    events = [event async for event in runtime.stream(handle)]

    assert attempts == ["read"]
    assert len(model.requests) == 2
    assert "callback_failed" in str(model.requests[1].messages)
    assert "private-path-placeholder" not in str(model.requests[1].messages)
    assert sum(isinstance(event, RunCompleted) for event in events) == 1
    assert runtime.replay(handle) == tuple(events)


@pytest.mark.asyncio
async def test_missing_provider_usage_is_explicitly_partial_on_terminal():
    runtime = adapter(bindings(model=Model({"text": "answer"})))
    handle = await start(runtime)
    events = [event async for event in runtime.stream(handle)]

    terminal = next(event for event in events if isinstance(event, RunCompleted))
    assert terminal.source.metadata["usage_completeness"] == "partial"
    assert terminal.source.metadata["usage_unavailable_attempts"] == 1
    assert not any(isinstance(event, UsageReported) for event in events)


@pytest.mark.asyncio
async def test_mixed_known_and_missing_usage_keeps_known_total_and_partial_marker():
    tool = ToolDefinition(name="read", description="fixture", parameters={"type": "object"})
    model = Model(
        {"tool_calls": (ModelToolCall(call_id="call", name="read", arguments={}),)},
        {"text": "answer", "usage": {"input_tokens": 5, "output_tokens": 1}},
    )
    runtime = adapter(bindings(model=model, definitions=(tool,)))
    handle = await start(runtime)
    events = [event async for event in runtime.stream(handle)]

    usage = [event for event in events if isinstance(event, UsageReported)]
    assert [(event.input_tokens, event.output_tokens) for event in usage] == [(5, 1)]
    terminal = next(event for event in events if isinstance(event, RunCompleted))
    assert terminal.source.metadata["usage_completeness"] == "partial"
    assert terminal.source.metadata["usage_unavailable_attempts"] == 1
    assert runtime.replay(handle) == tuple(events)


@pytest.mark.asyncio
async def test_provisional_text_has_distinct_commentary_identity_and_final_is_host_committed():
    class Streaming(Model):
        async def stream(self, req):
            yield ModelStreamEvent(
                kind="text_delta",
                request_id=req.request_id,
                attempt_id=req.attempt_id,
                seq=1,
                text="draft",
            )
            yield ModelStreamEvent(
                kind="final",
                request_id=req.request_id,
                attempt_id=req.attempt_id,
                seq=2,
                result=normalize_response(
                    request_id=req.request_id, attempt_id=req.attempt_id, text="delivered"
                ),
            )

    runtime = adapter(bindings(model=Streaming(), streaming=True))
    handle = await start(runtime)
    events = [e async for e in runtime.stream(handle)]
    projection = reduce_events(events).snapshot()
    messages = [item for item in projection.items if item.item_kind == "message"]
    assert [(m.phase, m.parts[0].text) for m in messages] == [
        ("commentary", ""),
        ("final_answer", "delivered"),
    ]
    assert messages[0].item_id != messages[1].item_id
    assert projection.output_refs[0].item_id == messages[1].item_id
    native = runtime.snapshot(handle)
    assert native.final_text == "delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": "foreign"},
        {"agent_id": "foreign"},
        {"metadata": {"tenant_id": "foreign", "workspace_id": "foreign"}},
        {"config": {"engine_kind": "legacy"}},
        {"model": "different-model"},
    ],
)
async def test_request_cannot_override_host_identity_or_engine(changes):
    runtime = adapter(bindings())
    with pytest.warns(UserWarning):
        with pytest.raises((ValueError, RuntimeError)):
            await runtime.start(request(**changes))


@pytest.mark.asyncio
async def test_follow_up_is_new_terminal_turn_with_native_session_history():
    model = Model({"text": "first answer"}, {"text": "second answer"})
    bound = bindings(model=model)
    runtime = adapter(bound)
    second = await start(runtime)
    await _drain(runtime, second)
    third = await runtime.follow_up(second, "Continue the ordinary task.")
    await _drain(runtime, third)
    assert second.session_id == third.session_id and second.run_id != third.run_id
    assert bound.store.load(SCOPE, third.run_id).parent_run_id == second.run_id
    assert any(
        m["role"] == "assistant" and m["content"] == "first answer"
        for m in model.requests[-1].messages
    )
    assert model.requests[-1].messages[-1]["content"] == "Continue the ordinary task."


@pytest.mark.asyncio
async def test_host_contract_compiler_uses_each_turn_goal_and_prior_contract(tmp_path):
    seen: list[TaskContractInput] = []

    async def compile_contract(facts: TaskContractInput) -> TaskContract:
        seen.append(facts)
        return TaskContract(
            revision=f"turn-{len(seen)}",
            items=(
                AcceptanceItem(
                    acceptance_id=f"goal-{len(seen)}",
                    verifier_ref="fixture://acceptance",
                    verifier_version="1",
                    verification_method="deterministic",
                    description=facts.goal,
                ),
            ),
        )

    bound = replace(
        bindings(model=Model({"text": "first answer"}, {"text": "second answer"})),
        task_contract_factory=compile_contract,
    )
    runtime = HarnessExecutionEngineFactory(
        settings=NativeHarnessSettings(native_harness_enabled=True, engine_kind="native"),
        native_bindings=bound,
    )(RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path))

    first = await start(runtime, input="Create the file.")
    await _drain(runtime, first)
    second = await runtime.follow_up(first, "Now test the file.")
    await _drain(runtime, second)

    first_state = bound.store.load(SCOPE, first.run_id)
    second_state = bound.store.load(SCOPE, second.run_id)
    assert [item.goal for item in seen] == ["Create the file.", "Now test the file."]
    assert seen[0].parent_contract is None
    assert seen[1].parent_run_id == first.run_id
    assert seen[1].parent_goal == "Create the file."
    assert seen[1].parent_contract == TaskContract.model_validate(first_state.task_contract)
    assert first_state.task_contract["revision"] == "turn-1"
    assert second_state.task_contract["revision"] == "turn-2"
    assert first_state.contract_digest != second_state.contract_digest
    assert first_state.configuration_digest == second_state.configuration_digest
    assert second_state.parent_run_id == first.run_id


@pytest.mark.asyncio
async def test_contract_compiler_runs_only_after_start_identity_admission(tmp_path):
    called = False

    def compile_contract(_facts: TaskContractInput) -> TaskContract:
        nonlocal called
        called = True
        return TaskContract(revision="1", items=())

    bound = replace(bindings(), task_contract_factory=compile_contract)
    runtime = HarnessExecutionEngineFactory(
        settings=NativeHarnessSettings(native_harness_enabled=True, engine_kind="native"),
        native_bindings=bound,
    )(RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path))
    with pytest.warns(UserWarning):
        with pytest.raises((ValueError, RuntimeError), match="scope"):
            await runtime.start(request(user_id="foreign"))
    assert called is False
    assert bound.store.session_head(SCOPE, "session") is None


@pytest.mark.asyncio
async def test_contract_compilation_rejects_changed_session_head(tmp_path):
    bound = bindings(model=Model({"text": "competing answer"}))
    competing = adapter(bound)
    competing_run_ids: list[str] = []

    async def compile_contract(_facts: TaskContractInput) -> TaskContract:
        other = await competing.start(request())
        competing_run_ids.append(other.run_id)
        await _drain(competing, other)
        return TaskContract(revision="stale", items=())

    runtime = HarnessExecutionEngineFactory(
        settings=NativeHarnessSettings(native_harness_enabled=True, engine_kind="native"),
        native_bindings=replace(bound, task_contract_factory=compile_contract),
    )(RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path))
    with pytest.warns(UserWarning):
        with pytest.raises((ValueError, RuntimeError), match="session changed"):
            await runtime.start(request())
    assert bound.store.session_head(SCOPE, "session").run_id == competing_run_ids[0]


async def _drain(runtime, handle):
    return [e async for e in runtime.stream(handle)]


@pytest.mark.asyncio
async def test_running_follow_up_or_engine_handle_switch_is_rejected():
    entered = asyncio.Event()

    class Blocked(Model):
        async def complete(self, req):
            entered.set()
            await asyncio.Event().wait()

    runtime = adapter(bindings(model=Blocked()))
    handle = await start(runtime)
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises((ValueError, RuntimeError)):
        await runtime.follow_up(handle, "must be separate")
    altered = handle.model_copy(deep=True)
    altered.native_ref["engine_kind"] = "legacy"
    with pytest.raises((ValueError, RuntimeError)):
        await runtime.cancel(altered)
    assert await runtime.cancel(handle) == CancelResult.INTERRUPTED_ACTIVE_TURN


@pytest.mark.asyncio
async def test_approval_roundtrip_and_snapshot_use_same_ledger():
    tool = ToolDefinition(name="read", description="fixture", parameters={"type": "object"})

    class ApprovalTools:
        async def execute(self, req):
            if req.interaction is None:
                return ToolResult(status="approval_required", interaction_id="host-approval")
            return ToolResult(status="ok", content="allowed")

    runtime = adapter(
        bindings(
            model=Model(
                {"tool_calls": (ModelToolCall(call_id="call", name="read", arguments={}),)},
                {"text": "done"},
            ),
            tools=ApprovalTools(),
            definitions=(tool,),
        )
    )
    handle = await start(runtime)
    events = await _drain(runtime, handle)
    approval = next(e for e in events if isinstance(e, InteractionRequested))
    assert runtime.snapshot(handle).pending_interaction == approval.interaction_id
    resumed = await runtime.resume(
        handle,
        ResumeTarget(kind="invocation_id", id=handle.run_id),
        ResumePayload(
            kind="approval_decision",
            call_id="call",
            data={"approval_id": approval.interaction_id, "decision": "approved"},
        ),
    )
    assert resumed == handle
    events.extend(await _drain(runtime, resumed))
    projection = reduce_events(events).snapshot()
    assert projection.interactions[0].status == "resolved"
    assert projection.status == "completed"


@pytest.mark.asyncio
async def test_durable_attach_replays_exact_ids_and_rejects_foreign_scope(tmp_path):
    path = tmp_path / "runs.sqlite"
    bound = bindings(store=SQLiteRunStore(path), durable=True)
    runtime = adapter(bound)
    handle = await start(runtime)
    expected = await _drain(runtime, handle)
    bound.store.close()
    restored_bound = bindings(store=SQLiteRunStore(path), durable=True)
    restored = adapter(restored_bound)
    with pytest.warns(UserWarning):
        same = await restored.attach(handle)
    actual = await _drain(restored, same)
    assert [dump_runtime_event(e) for e in actual] == [dump_runtime_event(e) for e in expected]
    foreign_bound = bindings(
        scope=SCOPE.model_copy(update={"tenant_id": "other"}),
        store=restored_bound.store,
        durable=True,
    )
    with pytest.raises((ValueError, RuntimeError)):
        await adapter(foreign_bound).attach(handle)
    restored_bound.store.close()


@pytest.mark.asyncio
async def test_steering_uses_goal_revision_and_does_not_create_new_turn():
    entered = asyncio.Event()
    release = asyncio.Event()

    class Blocking(Model):
        async def complete(self, req):
            entered.set()
            await release.wait()
            return await super().complete(req)

    model = Blocking({"text": "old"}, {"text": "new"})
    runtime = adapter(bindings(model=model))
    handle = await start(runtime)
    await asyncio.wait_for(entered.wait(), 2)
    await runtime.steer(
        handle, SteerPayload(content="Add a concise explanation.", run_id=handle.run_id)
    )
    release.set()
    await _drain(runtime, handle)
    assert runtime.snapshot(handle).goal_revision == 1
    assert runtime.snapshot(handle).run_id == handle.run_id


@pytest.mark.asyncio
async def test_two_live_steering_commands_keep_submission_order():
    entered = asyncio.Event()
    release = asyncio.Event()

    class Blocking(Model):
        async def complete(self, req):
            entered.set()
            await release.wait()
            return await super().complete(req)

    runtime = adapter(bindings(model=Blocking({"text": "old"}, {"text": "new"})))
    handle = await start(runtime)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.steer(handle, SteerPayload(content="First correction", run_id=handle.run_id))
        await runtime.steer(handle, SteerPayload(content="Second correction", run_id=handle.run_id))
        release.set()
        await _drain(runtime, handle)
        snapshot = runtime.snapshot(handle)
        assert snapshot.goal_revision == 2
        assert [m["content"] for m in snapshot.messages if m.get("role") == "user"][-2:] == [
            "First correction", "Second correction",
        ]
    finally:
        release.set()


def test_default_factory_is_lazy_legacy_and_native_missing_bindings_never_falls_back(tmp_path):
    called = []
    legacy = adapter(bindings())
    factory = HarnessExecutionEngineFactory(legacy_factory=lambda ctx: called.append(ctx) or legacy)
    ctx = RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path)
    assert factory(ctx) is legacy and len(called) == 1
    with pytest.raises((ValueError, RuntimeError), match="bindings"):
        HarnessExecutionEngineFactory(
            settings=NativeHarnessSettings(native_harness_enabled=True, engine_kind="native"),
            legacy_factory=lambda ctx: pytest.fail("must not fallback"),
        )(ctx)


def test_local_host_can_select_native_default_with_complete_bindings(tmp_path):
    factory = HarnessExecutionEngineFactory(
        settings=NativeHarnessSettings(
            native_harness_enabled=True,
            native_harness_default=True,
        ),
        native_bindings=bindings(),
    )
    runtime = factory(RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path))
    assert isinstance(runtime, NativeRuntimeAdapter)


@pytest.mark.asyncio
async def test_native_activation_closes_external_transports_once(tmp_path):
    calls = []

    async def close_transport():
        calls.append("closed")

    factory = HarnessExecutionEngineFactory(
        settings=NativeHarnessSettings(native_harness_enabled=True, engine_kind="native"),
        native_bindings=replace(bindings(), close_callbacks=(close_transport,)),
    )
    runtime = factory(RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path))
    await runtime.close_all()
    await runtime.close_all()
    assert calls == ["closed"]


def test_local_native_default_fails_closed_without_bindings(tmp_path):
    factory = HarnessExecutionEngineFactory(
        settings=NativeHarnessSettings(
            native_harness_enabled=True,
            native_harness_default=True,
        ),
        legacy_factory=lambda ctx: pytest.fail("must not fallback to LangGraph"),
    )
    with pytest.raises(ValueError, match="native_host_bindings_unavailable"):
        factory(RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path))


def test_local_native_default_does_not_enable_cloud_execution(tmp_path):
    factory = HarnessExecutionEngineFactory(
        settings=NativeHarnessSettings(
            native_harness_enabled=True,
            native_harness_default=True,
        ),
        native_bindings=bindings(),
    )
    with pytest.raises(ValueError, match="native_cloud_execution_unavailable"):
        factory(
            RuntimeLaunchContext(
                runtime_type="harness",
                project_dir=tmp_path,
                deployment_mode="ksadk_managed_cloud",
            )
        )


@pytest.mark.asyncio
async def test_studio_default_registry_accepts_explicit_native_harness_factory(tmp_path):
    bound = bindings()
    factory = HarnessExecutionEngineFactory(
        settings=NativeHarnessSettings(native_harness_enabled=True, engine_kind="native"),
        native_bindings=bound,
    )
    service = StudioService(tmp_path / "studio", harness_runtime_factory=factory)
    runtime = service.runtime_executor.create_adapter(
        RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path)
    )
    assert isinstance(runtime, NativeRuntimeAdapter)
    handle = await start(runtime)
    await _drain(runtime, handle)
    assert runtime.snapshot(handle).phase.terminal
    assert handle.runtime_type == "harness"
    assert build_default_runtime_registry().get("harness") is not factory


@pytest.mark.parametrize(
    "settings",
    [
        NativeHarnessSettings(engine_kind="native"),
        NativeHarnessSettings(native_harness_enabled=True, native_harness_shadow=True),
    ],
)
def test_factory_refuses_disabled_native_and_effectful_shadow(settings, tmp_path):
    with pytest.raises((ValueError, RuntimeError)):
        HarnessExecutionEngineFactory(settings=settings, native_bindings=bindings())(
            RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path)
        )


@pytest.mark.asyncio
async def test_checkpoint_is_explicitly_unavailable_and_close_does_not_close_host_store():
    bound = bindings()
    runtime = adapter(bound)
    handle = await start(runtime)
    await _drain(runtime, handle)
    with pytest.raises(UnsupportedControlError):
        await runtime.checkpoint(handle)
    await runtime.close(handle)
    assert bound.store.load(SCOPE, handle.run_id).phase.terminal


def test_import_native_adapter_and_factory_does_not_load_legacy_frameworks():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import ksadk.harness.native_runtime; import ksadk.harness.native_factory; "
            "assert not any(n.startswith(('langgraph', 'google.adk')) for n in sys.modules)",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.asyncio
async def test_reconnect_between_projected_mutations_preserves_remaining_events():
    runtime = adapter(bindings())
    handle = await start(runtime)
    # Let the short scripted run commit its full ledger, then stop precisely
    # between item.started and item.completed from one native event.
    while not runtime.snapshot(handle).phase.terminal:
        await asyncio.sleep(0)
    stream = runtime.stream(handle)
    seen = []
    while True:
        event = await anext(stream)
        seen.append(event)
        if isinstance(event, ItemStarted):
            break
    await stream.aclose()
    seen.extend(await _drain(runtime, handle))
    assert [dump_runtime_event(e) for e in seen] == [
        dump_runtime_event(e) for e in runtime.replay(handle)
    ]
    assert reduce_events(seen).snapshot().status == "completed"


@pytest.mark.parametrize("missing", ["model", "tools", "verifier", "context_planner", "store"])
def test_native_factory_requires_all_explicit_host_bindings(missing, tmp_path):
    bound = replace(bindings(), **{missing: None})
    factory = HarnessExecutionEngineFactory(
        settings=NativeHarnessSettings(native_harness_enabled=True, engine_kind="native"),
        native_bindings=bound,
        legacy_factory=lambda ctx: pytest.fail("native must not silently downgrade"),
    )
    with pytest.raises(ValueError, match="unavailable"):
        factory(RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path))
