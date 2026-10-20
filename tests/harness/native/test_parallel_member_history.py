"""Completed parallel members remain facts when a sibling interrupts the batch."""

import asyncio
import json

import pytest

from ksadk.harness.native.ports import ToolDefinition, ToolResourceUsage, ToolResult
from ksadk.harness.native.state import RunCommand, RunPhase
from ksadk.harness.stores.native_memory import MemoryRunStore
from ksadk.harness.stores.native_sqlite import SQLiteRunStore
from tests.harness.native.test_kernel import ScriptedModel, config, kernel


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("stop", ["cancel", "exception", "timeout"])
async def test_completed_member_survives_interrupted_batch(tmp_path, monkeypatch, backend, delegated, stop):
    if stop == "timeout":
        from ksadk.harness.native import effect_control

        class EffectClock:
            elapsed = 0.0

            def time(self):
                return self.elapsed

        clock = EffectClock()

        class EffectAsyncio:
            def get_running_loop(self):
                return clock

            def __getattr__(self, name):
                return getattr(asyncio, name)

        # Expire after member a is committed and b is running. Filesystem
        # latency before that boundary must not prevent b from ever starting.
        # Real asyncio waits and cancellation cleanup keep their original limits.
        monkeypatch.setattr(effect_control, "asyncio", EffectAsyncio())
    store = MemoryRunStore() if backend == "memory" else SQLiteRunStore(tmp_path / "runs.sqlite")
    fixture = tmp_path / "observed.txt"
    fixture.write_text("Actual completed observation.")
    entered, fail = asyncio.Event(), asyncio.Event()
    executed = []
    usage = ToolResourceUsage(
        model_calls=1, tool_calls=1, child_run_ids=("completed-child",),
        input_tokens=12, output_tokens=3, cached_input_tokens=0, reasoning_tokens=0,
    ) if delegated else ToolResourceUsage()
    expected = {"status": "ok", "content": {"text": fixture.read_text()}}

    class Tools:
        async def execute(self, request):
            executed.append(request.call.call_id)
            if request.call.call_id == "a":
                return ToolResult(status="ok", content=expected["content"], resource_usage=usage)
            assert request.call.call_id == "b"
            entered.set()
            await fail.wait()
            raise ValueError("Sibling failed after the first observation returned.")

    cfg = config(max_model_calls=6, max_tool_calls=4, max_parallel_tools=2,
                 effect_timeout_seconds=0.15 if stop == "timeout" else 3).model_copy(update={
        "tools": (
            ToolDefinition(name="inspect", description="Read a fixture", parameters={},
                           read_only=True, concurrent_safe=True, delegated_read_only=delegated),
            ToolDefinition(name="write", description="Queued write", parameters={}),
        ),
    })
    model = ScriptedModel({"tool_calls": [
        {"call_id": "a", "name": "inspect", "arguments": {}},
        {"call_id": "b", "name": "inspect", "arguments": {}},
        {"call_id": "c", "name": "write", "arguments": {}},
    ]}, {"text": "Report retained observations."})
    runtime = kernel(model, store=store, tools=Tools(), configuration=cfg)
    driver = None
    try:
        run = runtime.start(goal="Inspect two parts, then write.", session_id="member-history")
        driver = asyncio.create_task(runtime.run_until_idle(run.run_id))
        await asyncio.wait_for(entered.wait(), 2)
        assert executed == ["a", "b"]
        active = runtime.snapshot(run.run_id)
        parent_key = active.pending_intent
        actual_receipt = store.get_receipt(runtime.scope, run.run_id, parent_key + ":call:1")
        assert actual_receipt.status == "committed"
        assert actual_receipt.result["content"] == expected["content"]
        actual_message = [message for message in active.messages if message["role"] == "tool"]
        assert len(actual_message) == 1
        assert json.loads(actual_message[0]["content"]) == expected
        if stop == "cancel":
            runtime.submit(run.run_id, RunCommand(command_id="stop", kind="cancel"))
        elif stop == "exception":
            fail.set()
        elif stop == "timeout":
            clock.elapsed = cfg.limits.effect_timeout_seconds
        ended = await asyncio.wait_for(driver, 3)
        assert ended.phase == (RunPhase.CANCELED if stop == "cancel" else RunPhase.INTERRUPTED)
        assert ended.tool_calls == 2 and ended.model_calls == 1
        assert ended.descendant_model_calls == ended.descendant_tool_calls == int(delegated)
        assert ended.child_run_ids == (("completed-child",) if delegated else ())
        assert executed == ["a", "b"]
        assert [message for message in ended.messages if message["role"] == "tool"] == (
            actual_message
        )
        assert store.get_receipt(runtime.scope, run.run_id, parent_key + ":call:1") == (
            actual_receipt
        )
        assert [call.call_id for call in ended.tool_batch_interruption.calls] == ["b", "c"]
        assert len([event for event in runtime.events(run.run_id) if event.kind in {
            "run.completed", "run.failed", "run.canceled", "run.interrupted",
        }]) == 1
        original_json = ended.model_dump_json()

        following = runtime.start(goal="Report the previous facts.", session_id="member-history")
        await runtime.run_until_idle(following.run_id)
        request = model.requests[-1]
        assistant = next(message for message in request.messages if message.get("tool_calls"))
        ids = [call["id"] for call in assistant["tool_calls"]]
        observations = {message["tool_call_id"]: json.loads(message["content"])
                        for message in request.messages if message["role"] == "tool"}
        assert set(observations) == set(ids)
        assert observations[ids[0]] == expected
        assert observations[ids[1]]["execution_status"] == "unknown"
        assert observations[ids[2]]["execution_status"] == "not_dispatched"
        assert executed == ["a", "b"]
        assert store.load(runtime.scope, run.run_id).model_dump_json() == original_json
        from ksadk.harness.native_projection import NativeEventProjection

        projection = NativeEventProjection(runtime.scope, run.run_id)
        projected = [item for event in runtime.events(run.run_id)
                     for item in projection.consume(event)]
        tools = [item for item in projected if item.event_type == "item.completed"
                 and item.item_kind == "tool_call"]
        assert {item.source.metadata["call_id"]: item.source.metadata["tool_status"]
                for item in tools} == {"a": "ok", "b": "uncertain"}
        assert len(tools) == 2
        branch = runtime.fork(source_run_id=run.run_id, expected_source_revision=ended.revision,
                              session_id="member-fork", goal="Inspect retained facts.").snapshot
        branch_results = [json.loads(message["content"]) for message in branch.messages
                          if message["role"] == "tool"]
        assert branch_results[0] == expected
        assert [result.get("execution_status") for result in branch_results] == [
            None, "unknown", "not_dispatched",
        ]
        if backend == "sqlite":
            from ksadk.harness.native.history import project_session_history

            runtime.release(branch.run_id)
            store.close()
            store = SQLiteRunStore(tmp_path / "runs.sqlite")
            assert store.load(runtime.scope, run.run_id) == ended
            assert project_session_history(store.load(runtime.scope, run.run_id)) == (
                project_session_history(ended)
            )
    finally:
        if driver is not None and not driver.done():
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.parametrize("first_status", ["ok", "error"])
async def test_reverse_completion_is_sealed_once_without_double_accounting(
    tmp_path, monkeypatch, backend, delegated, first_status,
):
    store = MemoryRunStore() if backend == "memory" else SQLiteRunStore(tmp_path / "runs.sqlite")
    second_done = asyncio.Event()
    completed = []
    facts = {}

    class Tools:
        async def execute(self, request):
            identity = request.call.call_id
            if identity == "a":
                await second_done.wait()
            result = ToolResult(
                status=first_status if identity == "a" else "ok",
                content={"observed": identity},
                resource_usage=ToolResourceUsage(
                    model_calls=1, tool_calls=1, child_run_ids=("child-" + identity,),
                    input_tokens=12, output_tokens=3, cached_input_tokens=0, reasoning_tokens=0,
                ) if delegated else ToolResourceUsage(),
            )
            completed.append(identity)
            facts[identity] = result
            return result

    cfg = config(max_model_calls=8, max_tool_calls=8, max_parallel_tools=2).model_copy(update={
        "tools": (ToolDefinition(name="inspect", description="Read fixture", parameters={},
                                 read_only=True, concurrent_safe=True,
                                 delegated_read_only=delegated),),
    })
    model = ScriptedModel({"tool_calls": [
        {"call_id": identity, "name": "inspect", "arguments": {}} for identity in ("a", "b")
    ]}, {"text": "Observations retained."})
    runtime = kernel(model, store=store, tools=Tools(), configuration=cfg)
    callback = runtime._commit_parallel_child

    async def repeat(*args):
        await callback(*args)
        saved = runtime.snapshot(args[0])
        await callback(*args)
        assert runtime.snapshot(args[0]) == saved
        with pytest.raises(ValueError, match="receipt changed"):
            await callback(*args[:-1], args[-1].model_copy(update={"content": "different"}))
        assert runtime.snapshot(args[0]) == saved
        second_done.set()

    monkeypatch.setattr(runtime, "_commit_parallel_child", repeat)
    try:
        run = runtime.start(goal="Read both parts.", session_id="reverse-member")
        ended = await runtime.run_until_idle(run.run_id)
        assert completed == ["b", "a"]
        assert ended.tool_calls == 2 and ended.model_calls == 2
        assert ended.descendant_model_calls == ended.descendant_tool_calls == 2 * int(delegated)
        assert set(ended.child_run_ids) == ({"child-a", "child-b"} if delegated else set())
        rows = [message for message in ended.messages if message["role"] == "tool"]
        assert [row["tool_call_id"] for row in rows] == ["a", "b"]
        assert [row["receipt_tool_call"] for row in rows] == [1, 2]
        assert [json.loads(row["content"]) for row in rows] == [
            {"status": facts[identity].status, "content": facts[identity].content}
            for identity in ("a", "b")
        ]
        assert [message for message in model.requests[1].messages
                if message["role"] == "tool"] == rows
        events = runtime.events(run.run_id)
        assert len([event for event in events if event.kind in {
            "tool.member_settled", "tool.child_settled",
        }]) == 2
        assert len([event for event in events if event.kind == "usage.reported"
                    and event.payload.get("source") == "descendant_runs"]) == 2 * int(delegated)
        from ksadk.harness.native_projection import NativeEventProjection

        projection = NativeEventProjection(runtime.scope, run.run_id)
        tools = [item for event in events for item in projection.consume(event)
                 if item.event_type == "item.completed" and item.item_kind == "tool_call"]
        assert len(tools) == 2
        assert {item.source.metadata["call_id"]: item.source.metadata["tool_status"]
                for item in tools} == {"a": first_status, "b": "ok"}
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("delegated", [False, True])
async def test_late_member_cannot_publish_after_cancellation(delegated):
    entered, release, late = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Tools:
        async def execute(self, request):
            if request.call.call_id == "a":
                return ToolResult(status="ok", content="Completed before cancel.")
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            late.set()
            return ToolResult(status="ok", content="Late result must not reopen the run.")

    cfg = config(max_model_calls=6, max_tool_calls=4, max_parallel_tools=2).model_copy(update={
        "tools": (ToolDefinition(name="inspect", description="Read fixture", parameters={},
                                 read_only=True, concurrent_safe=True,
                                 delegated_read_only=delegated),),
    })
    runtime = kernel(ScriptedModel({"tool_calls": [
        {"call_id": identity, "name": "inspect", "arguments": {}} for identity in ("a", "b")
    ]}), tools=Tools(), configuration=cfg)
    run = runtime.start(goal="Inspect both parts.", session_id="late-member")
    driver = asyncio.create_task(runtime.run_until_idle(run.run_id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        runtime.submit(run.run_id, RunCommand(command_id="stop", kind="cancel"))
        ended = await asyncio.wait_for(driver, 3)
        events = runtime.events(run.run_id)
        assert ended.phase == RunPhase.CANCELED and ended.tool_calls == 2
        assert len([message for message in ended.messages if message["role"] == "tool"]) == 1
        release.set()
        await asyncio.wait_for(late.wait(), 2)
        await asyncio.sleep(0)
        assert runtime.snapshot(run.run_id) == ended
        assert runtime.events(run.run_id) == events
    finally:
        release.set()
        if not driver.done():
            driver.cancel()
        await asyncio.gather(driver, return_exceptions=True)


@pytest.mark.parametrize("observation_flag", [None, False, True])
def test_projection_preserves_legacy_accounting_and_completes_new_member_once(observation_flag):
    from ksadk.harness.native.state import NativeEvent
    from ksadk.harness.native_projection import NativeEventProjection
    from tests.harness.native.test_kernel import SCOPE

    payload = {"call_id": "a", "status": "ok", "parent_operation_key": "batch"}
    if observation_flag is not None:
        payload["model_observation_committed"] = observation_flag
    rows = [
        ("run.started", {}),
        ("effect.prepared", {"operation_key": "batch", "kind": "tool", "tool_calls": [
            {"call_id": identity, "name": "inspect"} for identity in ("a", "b")
        ]}),
        ("tool.child_settled", payload),
        ("effect.received", {"operation_key": "batch", "kind": "tool", "status": "committed",
                             "tool_results": [{"call_id": identity, "status": "ok"}
                                              for identity in ("a", "b")]}),
    ]
    projection = NativeEventProjection(SCOPE, "projection")
    results = []
    for index, (kind, data) in enumerate(rows, 1):
        projected = projection.consume(NativeEvent(event_id=str(index), run_id="projection",
                                                   turn_id="turn", seq=index, kind=kind,
                                                   payload=data))
        completed = [item for item in projected if item.event_type == "item.completed"
                     and item.item_kind == "tool_call"]
        if index == 3:
            assert len(completed) == int(observation_flag is True)
        results.extend(completed)
    assert len(results) == 2
    assert {item.source.metadata["call_id"] for item in results} == {"a", "b"}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("changed_source", [False, True])
async def test_partial_member_keeps_canonical_source_and_budgeted_artifact_view(
    tmp_path, monkeypatch, backend, changed_source,
):
    from ksadk.harness.native.engine import NativeKernel
    from tests.harness.native.test_artifact_continuity import MemoryArtifacts
    from tests.harness.native.test_captured_source_artifact import raw_result
    from tests.harness.native.test_kernel import SCOPE

    store = MemoryRunStore() if backend == "memory" else SQLiteRunStore(tmp_path / "runs.sqlite")
    entered = asyncio.Event()
    second_done = asyncio.Event()
    original = raw_result()

    class Tools:
        async def execute(self, request):
            if request.call.call_id == "a2":
                await second_done.wait()
            if request.call.call_id in {"a", "a2", "b2"}:
                if changed_source and request.call.call_id != "a":
                    # Distinct bytes must retain distinct member archives; an
                    # identical immutable source may reuse its verified locator.
                    import copy

                    content = copy.deepcopy(original.content)
                    content["requested_source"]["content"] += " " + request.call.call_id
                    return original.model_copy(update={"content": content})
                return original
            entered.set()
            await asyncio.Event().wait()

    cfg = config(max_model_calls=4, max_tool_calls=4, max_parallel_tools=2).model_copy(update={
        "tools": (ToolDefinition(name="source", description="Read source", parameters={},
                                 read_only=True, concurrent_safe=True,
                                 context_role="workspace_read",
                                 result_archive_policy="archived-model-view/v5"),),
    })
    model = ScriptedModel({"tool_calls": [
        {"call_id": identity, "name": "source",
         "arguments": {"path": "notes.txt" if identity == "a" else "b.txt"}}
        for identity in ("a", "b")
    ]}, {"tool_calls": [
        {"call_id": identity, "name": "source", "arguments": {"path": "notes.txt"}}
        for identity in ("a2", "b2")
    ]}, {"text": "Source retained."})
    runtime = NativeKernel(scope=SCOPE, store=store, configuration=cfg, model=model,
                           tools=Tools(), artifacts=MemoryArtifacts())
    callback = runtime._commit_parallel_child

    async def recorded(*args):
        await callback(*args)
        if args[3]["call_id"] == "b2":
            second_done.set()

    monkeypatch.setattr(runtime, "_commit_parallel_child", recorded)
    run = runtime.start(goal="Inspect sources.", session_id="source-member")
    driver = asyncio.create_task(runtime.run_until_idle(run.run_id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        runtime.submit(run.run_id, RunCommand(command_id="stop", kind="cancel"))
        ended = await asyncio.wait_for(driver, 3)
        actual = next(message for message in ended.messages if message["role"] == "tool")
        assert json.loads(actual["content"])["content"] == original.content
        assert actual["model_view"]["version"] == "canonical-tool-model-view/v2"
        assert len(ended.artifact_refs) == 1
        assert actual["artifact_refs"] == list(ended.artifact_refs)
        original_json = ended.model_dump_json()
        following = runtime.start(goal="Report retained sources.", session_id="source-member")
        assert following.artifact_refs == ended.artifact_refs
        refreshed = await runtime.run_until_idle(following.run_id)
        assert [ref["uri"] for ref in refreshed.artifact_refs] == [
            "artifact://fixture/a",
            *(["artifact://fixture/a2", "artifact://fixture/b2"] if changed_source else []),
        ]
        members = [message for message in refreshed.messages
                   if message.get("tool_call_id") in {"a2", "b2"}]
        assert [message["tool_call_id"] for message in members] == ["a2", "b2"]
        for message in members:
            identity = message["tool_call_id"]
            expected_uri = "artifact://fixture/" + (identity if changed_source else "a")
            assert [ref["uri"] for ref in message["artifact_refs"]] == [expected_uri]
            content = json.loads(message["content"])["content"]
            expected_source = original.content["requested_source"]["content"]
            assert content["requested_source"]["content"] == (
                expected_source + (" " + identity if changed_source else "")
            )
        new_events = [event.payload["call_id"] for event in runtime.events(following.run_id)
                      if event.kind == "tool.member_settled"]
        assert new_events == ["b2", "a2"]
        assert [message["tool_call_id"] for message in model.requests[-1].messages
                if message["role"] == "tool" and message["tool_call_id"] in {"a2", "b2"}] == (
                    ["a2", "b2"]
                )
        projected = next(message for message in model.requests[-1].messages
                         if message["role"] == "tool" and message.get("kind") != (
                             "incomplete_tool_observation"
                         ))
        assert "unfamiliar_audit_field" not in str(projected["content"])
        assert "artifact://fixture/a" in str(projected["content"])
        assert store.load(SCOPE, run.run_id).model_dump_json() == original_json
    finally:
        if not driver.done():
            driver.cancel()
        await asyncio.gather(driver, return_exceptions=True)
        store.close()
