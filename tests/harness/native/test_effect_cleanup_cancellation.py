"""Caller cancellation during wake cleanup must still stop the owned effect."""

import asyncio

import pytest

from ksadk.harness.native.engine import NativeKernel
from ksadk.harness.native.state import RunCommand
from ksadk.harness.stores.native_memory import MemoryRunStore
from tests.harness.native.test_kernel import SCOPE, config


@pytest.mark.asyncio
async def test_caller_cancel_during_wake_cleanup_stops_owned_model():
    entered, canceled = asyncio.Event(), asyncio.Event()
    driver, fired = None, False

    class Model:
        async def complete(self, request):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                canceled.set()
                raise

    class Wake(asyncio.Event):
        async def wait(self):
            nonlocal fired
            try:
                return await super().wait()
            finally:
                if not fired:
                    fired = True
                    asyncio.get_running_loop().call_soon(driver.cancel)

    kernel = NativeKernel(scope=SCOPE, store=MemoryRunStore(), configuration=config(), model=Model())
    run = kernel.start(goal="Test cancellation boundary.", session_id="cancel-window")
    kernel._wakes[run.run_id] = Wake()
    driver = asyncio.create_task(kernel.run_until_idle(run.run_id))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(driver), 1)
        assert fired and canceled.is_set()
        assert kernel.snapshot(run.run_id).model_calls == 0
    finally:
        if not driver.done():
            kernel.submit(run.run_id, RunCommand(command_id="cleanup", kind="cancel"))
        await asyncio.wait_for(asyncio.gather(driver, return_exceptions=True), 2)
