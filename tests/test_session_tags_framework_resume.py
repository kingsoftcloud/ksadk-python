"""Real durable framework interrupt/resume with a new admitted tag snapshot."""

import asyncio
import json

import pytest

from ksadk.events.canonical import InteractionRequested, ItemCompleted
from ksadk.runtime.adapter import ResumePayload, ResumeTarget, StartRequest
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from tests.e2e.session_tags_runtime import build_runner


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["langgraph", "langchain"])
async def test_real_checkpoint_resume_uses_new_snapshot(tmp_path, kind):
    pytest.importorskip("langchain")
    pytest.importorskip("langgraph.checkpoint.sqlite.aio")
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoint.db")) as saver:
        runner = build_runner(kind, interrupting=True)
        runner._agent.checkpointer = saver
        adapter = LangGraphRuntimeAdapter(runner)
        handle = await adapter.start(
            StartRequest(
                input="read tags",
                session_id="resume-tags",
                user_id="user",
                agent_id=kind,
                metadata={
                    "session_context": {
                        "schema_version": 1,
                        "revision": 1,
                        "tags": {"scene": "before"},
                    }
                },
            )
        )
        interrupted = [event async for event in adapter.stream(handle)]
        approval = next(e for e in interrupted if isinstance(e, InteractionRequested))
        checkpoint = await adapter.checkpoint(handle)
        await adapter.resume(
            handle,
            ResumeTarget(kind="checkpoint_id", id=checkpoint.checkpoint_id),
            ResumePayload(
                kind="approval_decision",
                call_id=approval.request.call_id,
                data={"decisions": [{"type": "approve"}]} if kind == "langchain" else True,
                session_context={"schema_version": 1, "revision": 2, "tags": {"scene": "after"}},
            ),
        )
        resumed = [event async for event in adapter.stream(handle)]
        text = json.dumps(
            [e.model_dump(mode="json") for e in resumed if isinstance(e, ItemCompleted)]
        )
        assert "after" in text, text
        assert "before" not in text, text
        assert "session_context" not in text, text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["langgraph", "langchain", "adk"])
async def test_kernel_worker_with_real_framework_reads_admitted_tags(kind):
    pytest.importorskip("google.adk" if kind == "adk" else "langchain")
    from ksadk.kernel.worker import AgentKernelWorker
    from ksadk.runtime.framework_adapters import ADKRuntimeAdapter
    from tests.kernel.control_harness import AGENT, command, kernel_stack

    runner = build_runner(kind)
    adapter = ADKRuntimeAdapter(runner) if kind == "adk" else LangGraphRuntimeAdapter(runner)
    stack = await kernel_stack(adapter=adapter)
    lease = await stack.lease()
    accepted = await stack.kernel.submit(
        command(
            payload={
                "content": "read tags",
                "session_context": {
                    "schema_version": 1,
                    "revision": 4,
                    "tags": {"scene": "kernel-worker"},
                },
            }
        ),
        permit=stack.permit("enqueue"),
    )
    assert accepted.status == "accepted"
    worker = AgentKernelWorker(
        stack.store, adapter_factory=lambda: adapter, session_events=stack.events
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed", result

    async def wait_for_terminal():
        for _ in range(300):
            run = await stack.store.load_run(result.run_id)
            if run.state.value in {"completed", "failed", "canceled"}:
                assert run.state.value == "completed", run.metadata
                return
            await asyncio.sleep(0.01)
        raise AssertionError("framework run did not complete")

    await wait_for_terminal()
    events = await stack.events.read("s1", 0, 1000)
    payload = json.dumps([e.model_dump(mode="json") for e in events])
    assert "kernel-worker" in payload, payload
