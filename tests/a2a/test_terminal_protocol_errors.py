"""Wire error codes for terminal tasks, across both advertised bindings."""

import httpx
import pytest
from a2a.server.context import ServerCallContext
from a2a.types import Task, TaskState, TaskStatus

from tests.a2a.test_a2a_protocol_e2e import _build_app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_CANCELED],
)
async def test_terminal_task_error_codes_are_binding_specific(tmp_path, state):
    app, server = _build_app(f"sqlite+aiosqlite:///{tmp_path}/tasks.db")
    await server.task_store.save(
        Task(id="terminal", context_id="ctx", status=TaskStatus(state=state)), ServerCallContext()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"A2A-Version": "1.0"},
    ) as client:
        cancel = await client.post("/a2a/v1/tasks/terminal:cancel", json={})
        assert cancel.status_code == 409
        assert cancel.json()["error"]["code"] == 409
        assert cancel.json()["error"]["details"][0]["reason"] == "TASK_NOT_CANCELABLE"
        subscribe = await client.post(
            "/a2a/jsonrpc",
            json={
                "jsonrpc": "2.0",
                "id": "sub",
                "method": "SubscribeToTask",
                "params": {"id": "terminal"},
            },
        )
        assert subscribe.json()["error"]["code"] == -32004
        missing = await client.post("/a2a/v1/tasks/missing:cancel", json={})
        assert missing.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledged,concurrent_state", [
    (False, TaskState.TASK_STATE_WORKING),
    (True, TaskState.TASK_STATE_WORKING),
    (True, TaskState.TASK_STATE_COMPLETED),
])
async def test_cancel_queue_repair_requires_fresh_confirmation_and_preserves_terminal(
    tmp_path, monkeypatch, acknowledged, concurrent_state,
):
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.types import CancelTaskRequest

    from ksadk.a2a.protocol_errors import _CONFIRMED_KERNEL_CANCEL

    _, server = _build_app(f"sqlite+aiosqlite:///{tmp_path}/cancel.db")
    context = ServerCallContext()
    task = Task(id="cancel-task", context_id="ctx",
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
    await server.task_store.save(task, context)
    # A marker left over from another invocation must not authorize a transition.
    confirmation = (task.id, TaskStatus(state=TaskState.TASK_STATE_CANCELED))
    context.state[_CONFIRMED_KERNEL_CANCEL] = confirmation

    async def upstream(handler, params, current_context):
        assert _CONFIRMED_KERNEL_CANCEL not in current_context.state
        if acknowledged:
            current_context.state[_CONFIRMED_KERNEL_CANCEL] = confirmation
        updated = Task()
        updated.CopyFrom(task)
        updated.status.state = concurrent_state
        await handler.task_store.save(updated, current_context)
        return task  # Simulate the stale cached task from the closed event queue.

    monkeypatch.setattr(DefaultRequestHandler, "on_cancel_task", upstream)
    result = await server.request_handler.on_cancel_task(CancelTaskRequest(id=task.id), context)
    expected = (TaskState.TASK_STATE_CANCELED
                if acknowledged and concurrent_state == TaskState.TASK_STATE_WORKING
                else concurrent_state)
    assert result.status.state == expected
    assert (await server.task_store.get(task.id, context)).status.state == expected
    assert _CONFIRMED_KERNEL_CANCEL not in context.state


@pytest.mark.asyncio
@pytest.mark.parametrize("storage", ["sqlite", "postgres"])
async def test_cancel_repair_does_not_overwrite_terminal_committed_after_read(
    tmp_path, monkeypatch, storage,
):
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.types import CancelTaskRequest

    from ksadk.a2a.protocol_errors import _CONFIRMED_KERNEL_CANCEL
    from ksadk.a2a.task_store import build_a2a_task_store

    dsn = f"sqlite+aiosqlite:///{tmp_path}/race.db"
    _, server = _build_app(dsn)
    if storage == "postgres":
        import os
        from uuid import uuid4

        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        pg_dsn = os.environ.get("A2A_TEST_POSTGRES_DSN")
        if not pg_dsn:
            pytest.skip("A2A_TEST_POSTGRES_DSN required")
        schema = "cancel_race_" + uuid4().hex
        admin = create_async_engine(pg_dsn)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engines = [create_async_engine(pg_dsn, connect_args={
            "server_settings": {"search_path": schema}}) for _ in range(2)]
        server.request_handler.task_store = build_a2a_task_store(engine=engines[0])
        server.task_store = server.request_handler.task_store
        other = build_a2a_task_store(engine=engines[1])
    else:
        other = build_a2a_task_store(dsn)
    context = ServerCallContext()
    task = Task(id="race", context_id="ctx", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
    await server.task_store.save(task, context)
    original_get = server.task_store.get
    reads = 0

    async def interleaved_get(task_id, ctx):
        nonlocal reads
        stale = await original_get(task_id, ctx)
        reads += 1
        if reads == 2:
            completed = Task()
            completed.CopyFrom(task)
            completed.status.state = TaskState.TASK_STATE_COMPLETED
            completed.artifacts.add(artifact_id="final").parts.add(text="committed result")
            await other.save(completed, ctx)
        return stale

    async def upstream(handler, params, ctx):
        ctx.state[_CONFIRMED_KERNEL_CANCEL] = (
            task.id, TaskStatus(state=TaskState.TASK_STATE_CANCELED))
        return task

    monkeypatch.setattr(server.task_store, "get", interleaved_get)
    monkeypatch.setattr(DefaultRequestHandler, "on_cancel_task", upstream)
    try:
        result = await server.request_handler.on_cancel_task(CancelTaskRequest(id=task.id), context)
        assert result.status.state == TaskState.TASK_STATE_COMPLETED
        saved = await other.get(task.id, context)
        assert saved.status.state == TaskState.TASK_STATE_COMPLETED
        assert saved.artifacts[0].parts[0].text == "committed result"
    finally:
        if storage == "postgres":
            for engine in engines:
                await engine.dispose()
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


@pytest.mark.asyncio
async def test_conditional_cancel_repair_keeps_owner_boundary(tmp_path):
    from ksadk.a2a.task_store import build_a2a_task_store

    store = build_a2a_task_store(f"sqlite+aiosqlite:///{tmp_path}/owner.db")
    owner = ServerCallContext(state={"account_id": "owner", "runtime_id": "runtime"})
    other = ServerCallContext(state={"account_id": "other", "runtime_id": "runtime"})
    task = Task(id="owned", context_id="ctx", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
    await store.save(task, owner)
    assert await store.save_confirmed_cancellation(
        task, TaskStatus(state=TaskState.TASK_STATE_CANCELED), other) is None
    assert (await store.get(task.id, owner)).status.state == TaskState.TASK_STATE_WORKING
    with pytest.raises(ValueError, match="Only confirmed cancellation"):
        await store.save_confirmed_cancellation(
            task, TaskStatus(state=TaskState.TASK_STATE_COMPLETED), owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_terminal", [True, False])
@pytest.mark.parametrize("terminal", [TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED])
async def test_pg_cancel_rechecks_state_after_row_lock(commit_terminal, terminal):
    import asyncio
    import os
    from uuid import uuid4

    from sqlalchemy import text, update
    from sqlalchemy.ext.asyncio import create_async_engine

    from ksadk.a2a.task_store import build_a2a_task_store

    dsn = os.environ.get("A2A_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("A2A_TEST_POSTGRES_DSN required")
    schema = "cancel_lock_" + uuid4().hex
    app_name = schema + "_repair"
    admin = create_async_engine(dsn)
    engines = []
    repairing = None
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engines = [create_async_engine(dsn, connect_args={"server_settings": {
            "search_path": schema, "application_name": name}})
            for name in (schema + "_writer", app_name)]
        writer, repair = [build_a2a_task_store(engine=engine) for engine in engines]
        owner = ServerCallContext(state={"account_id": "fixture", "runtime_id": "runtime"})
        task = Task(id="locked", context_id="ctx",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
        await writer.save(task, owner)
        await repair.get(task.id, owner)  # Finish lazy initialization before acquiring row lock.
        task.status.state = terminal
        task.artifacts.add(artifact_id="final").parts.add(text="terminal result")
        row = writer._to_orm(task, writer.owner_resolver(owner))
        stale = Task(id=task.id, context_id=task.context_id,
                     status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
        async with engines[0].connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(update(writer.task_model).where(
                    writer.task_model.id == task.id,
                    writer.task_model.owner == writer.owner_resolver(owner),
                ).values(status=row.status, artifacts=row.artifacts))
                repairing = asyncio.create_task(repair.save_confirmed_cancellation(
                    stale, TaskStatus(state=TaskState.TASK_STATE_CANCELED), owner))
                # Observe the actual database lock wait, not a timing assumption.
                async with asyncio.timeout(5):
                    while True:
                        async with admin.connect() as observer:
                            waiting = await observer.scalar(text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE application_name=:name AND wait_event_type='Lock'"
                            ), {"name": app_name})
                        if waiting:
                            break
                        assert not repairing.done()
                        await asyncio.sleep(0.01)
                if commit_terminal:
                    await transaction.commit()
                else:
                    await transaction.rollback()
            finally:
                if transaction.is_active:
                    await transaction.rollback()
        result = await asyncio.wait_for(repairing, 5)
        expected = terminal if commit_terminal else TaskState.TASK_STATE_CANCELED
        assert result.status.state == expected
        persisted = await writer.get(task.id, owner)
        assert persisted.status.state == expected
        if commit_terminal:
            assert persisted.artifacts[0].parts[0].text == "terminal result"
        else:
            assert not persisted.artifacts
    finally:
        if repairing is not None and not repairing.done():
            repairing.cancel()
            await asyncio.gather(repairing, return_exceptions=True)
        for engine in engines:
            await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "subscribe"])
@pytest.mark.parametrize("owned", [False, True])
async def test_crash_reconciliation_authorizes_before_get_or_subscribe(tmp_path, operation, owned):
    import asyncio

    from a2a.types import GetTaskRequest, SubscribeToTaskRequest
    from a2a.utils.errors import TaskNotFoundError, UnsupportedOperationError

    _, server = _build_app(f"sqlite+aiosqlite:///{tmp_path}/reconcile.db")
    owner = ServerCallContext(state={"account_id": "owner", "runtime_id": "runtime"})
    caller = owner if owned else ServerCallContext(
        state={"account_id": "other", "runtime_id": "runtime"})
    task = Task(id="crashed", context_id="ctx",
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
    await server.task_store.save(task, owner)
    calls = []

    async def reconcile(current, context, store):
        calls.append(current.id)
        return await store.save_confirmed_terminal(
            current, TaskStatus(state=TaskState.TASK_STATE_FAILED), context)

    server.request_handler.terminal_task_reconciler = reconcile

    async def request():
        if operation == "get":
            return await server.request_handler.on_get_task(GetTaskRequest(id=task.id), caller)
        return [event async for event in server.request_handler.on_subscribe_to_task(
            SubscribeToTaskRequest(id=task.id), caller)]

    if not owned:
        with pytest.raises(TaskNotFoundError):
            await asyncio.wait_for(request(), 2)
        assert calls == []
    elif operation == "subscribe":
        with pytest.raises(UnsupportedOperationError):
            await asyncio.wait_for(request(), 2)
        assert calls == [task.id]
    else:
        assert (await request()).status.state == TaskState.TASK_STATE_FAILED
        assert calls == [task.id]
    stored = await server.task_store.get(task.id, owner)
    assert stored.status.state == (TaskState.TASK_STATE_FAILED if owned
                                   else TaskState.TASK_STATE_WORKING)
