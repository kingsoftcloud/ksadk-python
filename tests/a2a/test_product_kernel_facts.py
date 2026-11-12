from unittest.mock import AsyncMock

import pytest

from ksadk.a2a.product_kernel_facts import SCHEMA, ProductKernelFactSink
from ksadk.a2a.task_event_dispatcher import A2ATaskEventDispatcher
from ksadk.a2a.task_event_outbox import SQLiteA2ATaskEventOutbox


@pytest.mark.asyncio
async def test_product_fact_retry_survives_outbox_reconstruction(tmp_path):
    path = tmp_path / "facts.sqlite3"
    control = AsyncMock()
    control.append_product_kernel_facts.side_effect = TimeoutError
    first = A2ATaskEventDispatcher(SQLiteA2ATaskEventOutbox(path), ProductKernelFactSink(control))
    binding = {
        "target_version_id": "version",
        "remote_task_id": "remote",
        "remote_context_id": "ctx",
    }
    event = {"schema": SCHEMA, "binding": binding, "fact": {"event_id": "fact-1"}}
    await first.enqueue(platform_task_id="task", events=[event])
    await first.drain(raise_on_error=False)
    assert len(await first.outbox.pending()) == 1
    second_control = AsyncMock()
    second = A2ATaskEventDispatcher(
        SQLiteA2ATaskEventOutbox(path), ProductKernelFactSink(second_control)
    )
    assert await second.drain(raise_on_error=True) == 1
    second_control.append_product_kernel_facts.assert_awaited_once_with(
        platform_task_id="task",
        **binding,
        events=[{"event_id": "fact-1"}],
    )
    assert not await second.outbox.pending()
    second_control.append_task_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_sink_batch_is_unchanged():
    control = AsyncMock()
    events = [{"event_id": "old", "event_type": "run.completed"}]
    await ProductKernelFactSink(control).append_task_events(platform_task_id="task", events=events)
    control.append_task_events.assert_awaited_once_with(platform_task_id="task", events=events)
    control.append_product_kernel_facts.assert_not_awaited()


@pytest.mark.asyncio
async def test_control_client_sends_callee_task_sink_action(tmp_path):
    import json

    import httpx

    from ksadk.a2a.control_plane import FileWorkloadTokenProvider, InternalA2AControlPlaneClient
    from tests.a2a.test_control_plane import _write_token

    _write_token(tmp_path, "a2a-task-sink")
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.path.endswith("/AppendA2AProductKernelFacts")
        assert json.loads(request.content) == {
            "A2ATaskId": "task",
            "TargetVersionId": "version",
            "RemoteTaskId": "remote",
            "RemoteContextId": "ctx",
            "Events": [],
        }
        return httpx.Response(200, json={"Code": 0, "Data": {"Accepted": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = InternalA2AControlPlaneClient(
            "https://control.example",
            token_provider=FileWorkloadTokenProvider(tmp_path),
            httpx_client=http,
        )
        result = await client.append_product_kernel_facts(
            platform_task_id="task",
            target_version_id="version",
            remote_task_id="remote",
            remote_context_id="ctx",
            events=[],
        )
        assert result == {"Accepted": 1}
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["completed", "interrupted"])
async def test_reconciler_recovers_without_subscriber_and_retries_after_restart(terminal):
    from types import SimpleNamespace

    from ksadk.a2a.product_kernel_facts import ProductKernelReconciler

    control = AsyncMock()
    item = {
        "A2ATaskId": "task",
        "TargetVersionId": "version",
        "RemoteTaskId": "remote",
        "RemoteContextId": "ctx",
        "SessionId": "a2a-kernel-test",
        "InitialCommandId": "cmd",
        "AfterSeq": 0,
    }
    control.list_product_kernel_tasks.return_value = {"Items": [item], "NextCursor": ""}
    final = {"seq": 1002, "state": terminal}

    class Events:
        async def subscribe(self, session_id, after_seq):
            for seq in range(after_seq + 1, 1003):
                yield SimpleNamespace(
                    seq=seq,
                    session_id=session_id,
                    family="control" if seq == 1002 else "runtime",
                    event_type="control.run_transition" if seq == 1002 else "item.updated",
                    causation_id="cmd",
                    payload={"state": terminal},
                    model_dump=lambda **kw: final,
                )

    first = ProductKernelReconciler(control, Events(), scan_timeout=1)
    await first.reconcile_once()
    control.append_product_kernel_facts.assert_not_awaited()
    control.append_product_kernel_facts.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await first.reconcile_once()
    control.append_product_kernel_facts.side_effect = None
    restarted = ProductKernelReconciler(control, Events(), scan_timeout=1)
    await restarted.reconcile_once()
    await restarted.reconcile_once()
    assert control.append_product_kernel_facts.await_count == 2
    assert control.append_product_kernel_facts.await_args.kwargs["events"] == [final]


@pytest.mark.asyncio
async def test_reconciler_empty_stream_never_invents_terminal_state():
    from ksadk.a2a.product_kernel_facts import ProductKernelReconciler

    control = AsyncMock()
    control.list_product_kernel_tasks.return_value = {
        "Items": [
            {
                "A2ATaskId": "task",
                "TargetVersionId": "version",
                "RemoteTaskId": "remote",
                "RemoteContextId": "ctx",
                "SessionId": "a2a-kernel-test",
                "InitialCommandId": "cmd",
                "AfterSeq": 0,
            }
        ],
        "NextCursor": "",
    }

    class Events:
        async def subscribe(self, *args):
            if False:
                yield

    await ProductKernelReconciler(control, Events()).reconcile_once()
    control.append_product_kernel_facts.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_recovery_item_does_not_starve_other_tasks():
    from ksadk.a2a.product_kernel_facts import ProductKernelReconciler

    control = AsyncMock()
    good = {
        "A2ATaskId": "good",
        "TargetVersionId": "version",
        "RemoteTaskId": "remote",
        "RemoteContextId": "ctx",
        "SessionId": "a2a-kernel-good",
        "InitialCommandId": "cmd",
        "AfterSeq": 0,
    }
    control.list_product_kernel_tasks.return_value = {"Items": [{}, good], "NextCursor": "good"}
    observed = []

    class Events:
        async def subscribe(self, session_id, after_seq):
            observed.append(session_id)
            if False:
                yield

    reconciler = ProductKernelReconciler(control, Events())
    with pytest.raises(ValueError):
        await reconciler.reconcile_once()
    assert observed == ["a2a-kernel-good"]
    assert reconciler._cursor == "good"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_seq", [0, -1, True, "1"])
async def test_invalid_sequence_closes_stream_without_advancing_recovery(bad_seq):
    from types import SimpleNamespace

    from ksadk.a2a.product_kernel_facts import ProductKernelReconciler

    control = AsyncMock()
    control.list_product_kernel_tasks.return_value = {
        "Items": [
            {
                "A2ATaskId": "task",
                "TargetVersionId": "version",
                "RemoteTaskId": "remote",
                "RemoteContextId": "ctx",
                "SessionId": "a2a-kernel-test",
                "InitialCommandId": "cmd",
                "AfterSeq": 0,
            }
        ],
        "NextCursor": "",
    }
    closed = []

    class Events:
        async def subscribe(self, *args):
            try:
                yield SimpleNamespace(seq=bad_seq)
                pytest.fail("invalid sequence must stop the scan immediately")
            finally:
                closed.append(True)

    reconciler = ProductKernelReconciler(control, Events())
    with pytest.raises(ValueError, match="sequence must increase"):
        await reconciler.reconcile_once()
    assert closed == [True]
    assert not reconciler._scan_cursors
    control.append_product_kernel_facts.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_progress_survives_more_than_1024_active_tasks_and_is_reclaimed():
    from types import SimpleNamespace

    from ksadk.a2a.product_kernel_facts import ProductKernelReconciler

    active = list(range(1025))
    accepted = set()

    class Control:
        async def list_product_kernel_tasks(self, *, after_task_id, limit):
            remaining = [i for i in active if not after_task_id or i > int(after_task_id)]
            page = remaining[:limit]
            return {
                "Items": [
                    dict(
                        A2ATaskId=str(i),
                        TargetVersionId="version",
                        RemoteTaskId=str(i),
                        RemoteContextId="ctx",
                        SessionId=f"a2a-kernel-{i}",
                        InitialCommandId="cmd",
                        AfterSeq=0,
                    )
                    for i in page
                ],
                "NextCursor": str(page[-1]) if len(remaining) > limit else "",
            }

        async def append_product_kernel_facts(self, *, platform_task_id, **kwargs):
            accepted.add(platform_task_id)

    class Events:
        async def subscribe(self, session_id, after_seq):
            for seq in range(after_seq + 1, 1002):
                yield SimpleNamespace(
                    seq=seq,
                    session_id=session_id,
                    family="control" if seq == 1001 else "runtime",
                    event_type="control.run_transition" if seq == 1001 else "item.updated",
                    causation_id="cmd",
                    payload={"state": "completed"},
                    model_dump=lambda **kw: {"seq": 1001, "state": "completed"},
                )

    reconciler = ProductKernelReconciler(Control(), Events(), scan_timeout=10)
    for _ in range(2):
        for _ in range(41):
            await reconciler.reconcile_once()
    assert len(accepted) == 1025
    assert len(reconciler._scan_cursors) == 1025
    active.clear()
    await reconciler.reconcile_once()
    assert reconciler._scan_cursors == {}
    assert reconciler._sweep_tasks == set()


@pytest.mark.asyncio
async def test_product_http_rejection_is_dead_lettered_without_blocking_next_task(tmp_path):
    import json
    import sqlite3

    import httpx

    from ksadk.a2a.control_plane import FileWorkloadTokenProvider, InternalA2AControlPlaneClient
    from tests.a2a.test_control_plane import _write_token

    _write_token(tmp_path, "a2a-task-sink")
    seen = []

    def handler(request):
        task = json.loads(request.content)["A2ATaskId"]
        seen.append(task)
        if task == "bad":
            return httpx.Response(
                409, json={"Code": 409, "Data": {"ErrorCode": "A2A_KERNEL_FACT_INVALID"}}
            )
        return httpx.Response(200, json={"Code": 0, "Data": {"Accepted": 1}})

    path = tmp_path / "outbox.sqlite3"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        control = InternalA2AControlPlaneClient(
            "https://control.example",
            token_provider=FileWorkloadTokenProvider(tmp_path),
            httpx_client=http,
        )
        dispatcher = A2ATaskEventDispatcher(
            SQLiteA2ATaskEventOutbox(path), ProductKernelFactSink(control)
        )
        for task in ("bad", "good"):
            await dispatcher.enqueue(
                platform_task_id=task,
                events=[
                    {
                        "schema": SCHEMA,
                        "binding": {
                            "target_version_id": "version",
                            "remote_task_id": task,
                            "remote_context_id": "ctx",
                        },
                        "fact": {"event_id": task},
                    }
                ],
            )
        assert await dispatcher.drain(raise_on_error=False) == 1
        assert await dispatcher.outbox.pending() == []
    assert seen == ["bad", "good"]
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT platform_task_id, last_error FROM a2a_task_event_dead_letters"
        ).fetchall() == [("bad", "A2A_KERNEL_FACT_INVALID")]


@pytest.mark.asyncio
async def test_actual_cold_recovery_interruption_survives_product_outbox_restart(tmp_path):
    from types import SimpleNamespace

    from ksadk.a2a.product_kernel_facts import enqueue_product_kernel_fact
    from tests.kernel.test_recovery import AGENT, SESSION, CrashPoint, RecoveryHarness

    harness = RecoveryHarness()
    old = await harness.acquire("old")
    await harness.submit_enqueue()
    with pytest.raises(CrashPoint):
        await harness.drive(old, crash_point="after_takeover")
    await harness.kernel_store.release_activation(
        old.activation_id, expected_fence=old.fencing_token
    )
    new = await harness.acquire("new")
    report = await harness.coordinator().recover(AGENT, new)
    assert report.outcome == "interrupted"
    facts = await harness.event_store.read(SESSION, 0, 100)
    interrupted = [fact for fact in facts if fact.event_type == "control.run_transition"
                   and fact.payload.get("state") == "interrupted"]
    assert len(interrupted) == 1
    binding = dict(platform_task_id="task", target_version_id="version",
                   remote_task_id="remote", remote_context_id="context")
    handle = SimpleNamespace(native_ref={"product_kernel_binding": binding})
    path = tmp_path / "interrupted.sqlite"
    control = AsyncMock()
    dispatcher = A2ATaskEventDispatcher(
        SQLiteA2ATaskEventOutbox(path), ProductKernelFactSink(control)
    )
    await enqueue_product_kernel_fact(dispatcher, handle, interrupted[0])
    restarted = A2ATaskEventDispatcher(
        SQLiteA2ATaskEventOutbox(path), ProductKernelFactSink(control)
    )
    assert await restarted.drain(raise_on_error=True) == 1
    control.append_product_kernel_facts.assert_awaited_once()
    assert control.append_product_kernel_facts.await_args.kwargs["events"] == [
        interrupted[0].model_dump(mode="json")]
    assert not await restarted.outbox.pending()
