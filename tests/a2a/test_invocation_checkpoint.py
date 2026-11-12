import asyncio
import os
import sys
import time
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from ksadk.a2a.invocation_checkpoint import (
    A2AInvocationCheckpoint,
    InvocationCheckpointBusy,
    PostgresA2AInvocationCheckpointStore,
)
from tests.a2a.test_invocation_service_r85 import request


def dsn():
    value = os.getenv("KSADK_A2A_TEST_DATABASE_URL", "")
    if not value:
        pytest.skip("real PostgreSQL required")
    assert value.startswith("postgresql+asyncpg://")
    return value


def checkpoint(run_id):
    initial = request()
    handle = initial.handle.model_copy(update={"run_id": run_id})
    initial = initial.model_copy(update={"handle": handle})
    return A2AInvocationCheckpoint(request=initial, handle=handle)


@pytest.mark.asyncio
async def test_postgres_exclusive_lease_commit_owner_isolation_and_monotonic_receipt():
    engine = create_async_engine(dsn())
    store = PostgresA2AInvocationCheckpointStore(
        engine, account_id="test-account", tenant_id="test-tenant", runtime_id="test-runtime"
    )
    isolated = PostgresA2AInvocationCheckpointStore(
        engine, account_id="other-account", tenant_id="test-tenant", runtime_id="test-runtime"
    )
    state = checkpoint(str(uuid.uuid4()))
    try:
        await store.initialize()
        async with store.lease(state.handle.scope_id) as lease:
            assert await lease.load() is None
            await lease.save(state)
            with pytest.raises(InvocationCheckpointBusy):
                async with store.lease(state.handle.scope_id):
                    pytest.fail("second lease acquired")
            async with isolated.lease(state.handle.scope_id) as other:
                assert await other.load() is None
            await lease.save(state.model_copy(update={"send_entered": True}))
            with pytest.raises(ValueError, match="SEND_REGRESSION"):
                await lease.save(state)
        with pytest.raises(RuntimeError, match="LEASE_CLOSED"):
            await lease.save(state)
        async with store.lease(state.handle.scope_id) as recovered:
            assert (await recovered.load()).send_entered
            changed = state.model_copy(
                update={
                    "send_entered": True,
                    "request": state.request.model_copy(update={"idempotency_token": "different"}),
                }
            )
            with pytest.raises(ValueError, match="REQUEST_CONFLICT"):
                await recovered.save(changed)
            await recovered._connection.invalidate()
            with pytest.raises(RuntimeError, match="LEASE_CLOSED"):
                await recovered.save(state)
    finally:
        await engine.dispose()


WORKER = """
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine
from ksadk.a2a.invocation_checkpoint import PostgresA2AInvocationCheckpointStore
from tests.a2a.test_invocation_checkpoint import checkpoint
async def main():
    engine = create_async_engine(os.environ["KSADK_A2A_TEST_DATABASE_URL"])
    store = PostgresA2AInvocationCheckpointStore(
        engine, account_id="crash-account", tenant_id="test-tenant", runtime_id="test-runtime")
    await store.initialize()
    state = checkpoint(os.environ["R85_CHECKPOINT_TEST_RUN"])
    async with store.lease(state.handle.scope_id) as lease:
        await lease.save(state.model_copy(update={
            "send_entered": True, "state": {"marker": "committed"}}))
        print("READY", flush=True)
        await asyncio.Event().wait()
asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_process_kill_releases_lease_without_rolling_back_send_receipt():
    engine = create_async_engine(dsn())
    store = PostgresA2AInvocationCheckpointStore(
        engine, account_id="crash-account", tenant_id="test-tenant", runtime_id="test-runtime"
    )
    state = checkpoint(str(uuid.uuid4()))
    environment = {**os.environ, "R85_CHECKPOINT_TEST_RUN": state.handle.run_id}
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        WORKER,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        assert await asyncio.wait_for(worker.stdout.readline(), 20) == b"READY\n"
        with pytest.raises(InvocationCheckpointBusy):
            async with store.lease(state.handle.scope_id):
                pytest.fail("live worker lost its lease")
        worker.kill()
        await asyncio.wait_for(worker.wait(), 10)
        async with store.lease(state.handle.scope_id) as lease:
            recovered = await lease.load()
            assert recovered.send_entered
            assert recovered.state == {"marker": "committed"}
    finally:
        if worker.returncode is None:
            worker.kill()
            await worker.wait()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["release", "timeout", "cancel"])
async def test_recovery_wait_preserves_owner_and_original_receipt(outcome):
    engine = create_async_engine(dsn())
    store = PostgresA2AInvocationCheckpointStore(
        engine, account_id=str(uuid.uuid4()), tenant_id="tenant", runtime_id="runtime"
    )
    saved = checkpoint(str(uuid.uuid4()))
    saved = saved.model_copy(update={"request": saved.request.model_copy(update={
        "deadline_unix_seconds": time.time() + (0.7 if outcome == "timeout" else 10),
    })})
    entered = asyncio.Event()

    async def recover():
        entered.set()
        async with store.lease(saved.handle.scope_id, wait_for_recovery=True) as lease:
            return await lease.load()

    pending = None
    try:
        await store.initialize()
        async with store.lease(saved.handle.scope_id) as owner:
            await owner.save(saved)
            pending = asyncio.create_task(recover())
            await entered.wait()
            await asyncio.sleep(0.15)
            assert not pending.done()
            if outcome == "cancel":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            elif outcome == "timeout":
                with pytest.raises(TimeoutError, match="RECOVERY_TIMEOUT"):
                    await asyncio.wait_for(pending, 2)
            else:
                # A waiting reader must reload after acquiring, not restore
                # the earlier unlocked snapshot used to compute its budget.
                await owner.save(saved.model_copy(update={"send_entered": True}))
            with pytest.raises(InvocationCheckpointBusy):
                async with store.lease(saved.handle.scope_id):
                    pytest.fail("healthy owner was displaced")
        if outcome == "release":
            recovered = await asyncio.wait_for(pending, 2)
            assert recovered.send_entered
            assert recovered.request == saved.request
        async with store.lease(saved.handle.scope_id) as lease:
            assert (await lease.load()).request == saved.request
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await engine.dispose()
