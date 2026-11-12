"""A process stop must not replay a potentially executed native tool."""

import asyncio

import pytest
from langgraph.graph import END, START, StateGraph

from ksadk.interaction.langgraph import request_input
from ksadk.kernel.state import RunState
from tests.agui.test_kernel_native_input import body, first_wait, stack, until
from tests.interaction.test_native_confirmation_pipeline import State


@pytest.mark.parametrize("after_resolution", [False, True])
async def test_shutdown_and_restore_preserve_unknown_consumption_without_start_replay(
    tmp_path,
    monkeypatch,
    after_resolution,
):
    before, tools = [], []
    reached = asyncio.Event()
    release = asyncio.Event()

    def initialize(_):
        before.append(1)
        return {}

    async def wait(_):
        answer = request_input("Choose a value", {"type": ["integer", "null"]})
        tools.append(answer)
        if not after_resolution:
            reached.set()
            await release.wait()
        return {"answer": "done"}

    builder = StateGraph(State)
    builder.add_node("initialize", initialize)
    builder.add_node("wait", wait)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "wait")
    builder.add_edge("wait", END)
    async with stack(tmp_path, monkeypatch, before, tools, graph_builder=builder) as s:
        record = await first_wait(s)
        if after_resolution:
            resolve = s.runtime.kernel_store.resolve

            async def commit_then_pause(*args, **kwargs):
                receipt = await resolve(*args, **kwargs)
                reached.set()
                await release.wait()
                return receipt

            monkeypatch.setattr(s.runtime.kernel_store, "resolve", commit_then_pause)
        response = await s.client.post(
            "/agentengine/api/v1/SubmitInteraction", json=body(record, 0)
        )
        assert response.status_code == 202
        async with asyncio.timeout(8):
            await reached.wait()
        execution = s.runtime.worker.execution_for(record.run_id)
        assert not execution.stream_task.done()
        assert (await s.runtime.kernel_store.load_run(record.run_id)).state is RunState.RUNNING
    assert execution.stream_task.done()
    async with stack(tmp_path, monkeypatch, before, tools, graph_builder=builder) as s:

        async def recovered():
            run = await s.runtime.kernel_store.load_run(record.run_id)
            return run.state is RunState.WAITING and bool(
                run.metadata.get("native_reconciliation_required")
            )

        await until(recovered)
        assert before == [1] and tools == [0]
        current = await s.runtime.kernel_store.get(record.interaction_id)
        assert current.status == ("resolved" if after_resolution else "resolving")
        assert current.native_target == record.native_target
