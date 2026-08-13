from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.runtime import (
    BaseRuntime,
    CancelResult,
    RunHandle,
    RuntimeAdapter,
    RuntimeExecutor,
    RuntimeLaunchContext,
    RuntimeRegistry,
    StartRequest,
)
from ksadk.sessions.in_memory import InMemorySessionService


class _Runtime(BaseRuntime):
    runtime_type = "fixture"

    def native_capabilities(self) -> dict[str, object]:
        return {}


class _Adapter(RuntimeAdapter):
    def __init__(self) -> None:
        super().__init__(_Runtime())

    async def start(self, request: StartRequest) -> RunHandle:
        return RunHandle(
            run_id=str(request.metadata["invocation_id"]),
            session_id=request.session_id,
            runtime_type="fixture",
        )

    async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        common = {
            "agent_id": "agent-1",
            "user_id": "user-1",
            "session_id": handle.session_id,
            "invocation_id": handle.run_id,
        }
        yield RuntimeEvent.create(
            EventType.RUN_STARTED,
            seq_id=1,
            payload={"status": "in_progress"},
            **common,
        )
        yield RuntimeEvent.create(
            EventType.TEXT_DELTA,
            seq_id=2,
            phase="final_answer",
            payload={"text": "hel"},
            **common,
        )
        yield RuntimeEvent.create(
            EventType.TEXT_COMPLETED,
            seq_id=3,
            phase="final_answer",
            payload={"text": "hello"},
            **common,
        )
        yield RuntimeEvent.create(
            EventType.USAGE_REPORTED,
            seq_id=4,
            payload={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
            **common,
        )
        yield RuntimeEvent.create(
            EventType.RUN_COMPLETED,
            seq_id=5,
            payload={"status": "completed", "duration_ms": 25},
            **common,
        )

    async def cancel(self, _handle):
        return CancelResult.NOT_RUNNING

    async def resume(self, handle, _target, _payload):
        return handle

    async def checkpoint(self, _handle):
        raise NotImplementedError

    async def close(self, _handle):
        return None


def _decode_sse(chunks: list[str]) -> list[tuple[str, dict]]:
    decoded = []
    for chunk in chunks:
        lines = chunk.strip().splitlines()
        event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        decoded.append((event, json.loads(data)))
    return decoded


@pytest.mark.asyncio
async def test_runtime_events_use_the_existing_responses_serializer_without_duplicate_text():
    from ksadk.conversations.runtime_streaming import (
        stream_runtime_responses_conversation_turn,
    )

    service = InMemorySessionService()
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _context: _Adapter())
    chunks = [
        chunk
        async for chunk in stream_runtime_responses_conversation_turn(
            executor=RuntimeExecutor(registry),
            launch_context=RuntimeLaunchContext(runtime_type="fixture", project_dir="."),
            agent_id="agent-1",
            user_id="user-1",
            messages=[{"role": "user", "content": "hi"}],
            session_id=None,
            model="fixture-model",
            session_service_provider=lambda: service,
        )
    ]

    events = _decode_sse(chunks)
    deltas = [data["delta"] for name, data in events if name == "response.output_text.delta"]
    completed = next(data for name, data in events if name == "response.completed")

    assert deltas == ["hel", "lo"]
    assert completed["output_text"] == "hello"
    assert completed["usage"] == {
        "input_tokens": 7,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 2,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 9,
    }
