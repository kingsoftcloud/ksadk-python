from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.runtime import (
    BaseRuntime,
    CancelResult,
    CheckpointCapability,
    CheckpointDescriptor,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    RuntimeExecutor,
    RuntimeRegistry,
    StartRequest,
)

EventStreamFactory = Callable[[StartRequest, RunHandle], AsyncIterator[RuntimeEvent]]


class _FixtureRuntime(BaseRuntime):
    def __init__(self, runtime_type: str) -> None:
        self.runtime_type = runtime_type

    def native_capabilities(self) -> dict[str, Any]:
        return {"Framework": self.runtime_type, "cancel": "thread"}


class RecordingRuntimeAdapter(RuntimeAdapter):
    def __init__(self, fixture: RuntimeFixture, runtime_type: str) -> None:
        super().__init__(_FixtureRuntime(runtime_type))
        self.fixture = fixture
        self.runtime_type = runtime_type
        self.requests: dict[str, StartRequest] = {}

    async def start(self, request: StartRequest) -> RunHandle:
        self.fixture.start_requests.append(request)
        run_id = f"fixture-{self.runtime_type}-{len(self.fixture.start_requests)}"
        self.requests[run_id] = request
        return RunHandle(
            run_id=run_id,
            session_id=request.session_id,
            runtime_type=self.runtime_type,
            native_ref={"thread_id": run_id},
        )

    def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        return self.fixture.event_stream(self.requests[handle.run_id], handle)

    async def cancel(self, handle: RunHandle) -> CancelResult:
        self.fixture.cancelled.append(handle)
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        return handle

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        return CheckpointDescriptor(
            checkpoint_id=handle.run_id,
            invocation_id=handle.run_id,
            capability=CheckpointCapability(
                supported=False,
                granularity="none",
                rollback_scope="none",
                fork_supported=False,
                durable=False,
                shared_across_pods=False,
            ),
        )

    async def close(self, handle: RunHandle) -> None:
        self.fixture.closed.append(handle)


class RuntimeFixture:
    def __init__(
        self,
        event_stream: EventStreamFactory,
        *,
        runtime_types: tuple[str, ...] = ("codex",),
    ) -> None:
        self.event_stream = event_stream
        self.start_requests: list[StartRequest] = []
        self.cancelled: list[RunHandle] = []
        self.closed: list[RunHandle] = []
        self.adapters: list[RecordingRuntimeAdapter] = []
        registry = RuntimeRegistry()
        for runtime_type in runtime_types:
            registry.register(
                runtime_type,
                lambda _context, selected=runtime_type: self._create_adapter(selected),
            )
        self.executor = RuntimeExecutor(registry)

    def _create_adapter(self, runtime_type: str) -> RuntimeAdapter:
        adapter = RecordingRuntimeAdapter(self, runtime_type)
        self.adapters.append(adapter)
        return adapter


async def standard_codex_events(
    request: StartRequest,
    handle: RunHandle,
) -> AsyncIterator[RuntimeEvent]:
    common = {
        "agent_id": request.agent_id or "agent",
        "user_id": request.user_id,
        "session_id": request.session_id,
        "invocation_id": str(request.metadata["invocation_id"]),
        "trace_id": str(request.metadata["trace_id"]),
    }
    yield RuntimeEvent.create(
        EventType.RUN_STARTED,
        **common,
        seq_id=1,
        payload={"status": "in_progress"},
    )
    yield RuntimeEvent.create(
        EventType.REASONING_DELTA,
        **common,
        seq_id=2,
        phase="commentary",
        payload={"text": "读取文件"},
    )
    yield RuntimeEvent.create(
        EventType.TOOL_CALL_BEGIN,
        **common,
        seq_id=3,
        payload={
            "call_id": "cmd-1",
            "name": "codex.command",
            "args": {
                "command": "sed -n '1,80p' src/demo.py",
                "cwd": str(request.config.get("cwd") or ""),
                "command_actions": [{"type": "read", "path": "src/demo.py"}],
            },
        },
    )
    yield RuntimeEvent.create(
        EventType.TOOL_CALL_END,
        **common,
        seq_id=4,
        payload={
            "call_id": "cmd-1",
            "name": "codex.command",
            "result": {"status": "completed", "exit_code": 0, "duration_ms": 10},
        },
    )
    yield RuntimeEvent.create(
        EventType.TEXT_DELTA,
        **common,
        seq_id=5,
        phase="final_answer",
        payload={"text": "发现除零风险。"},
    )
    yield RuntimeEvent.create(
        EventType.TEXT_COMPLETED,
        **common,
        seq_id=6,
        phase="final_answer",
        payload={"text": "发现除零风险。请先检查空列表。"},
    )
    yield RuntimeEvent.create(
        EventType.RUN_COMPLETED,
        **common,
        seq_id=7,
        payload={"status": "completed", "duration_ms": 25},
    )


__all__ = ["RuntimeFixture", "standard_codex_events"]
