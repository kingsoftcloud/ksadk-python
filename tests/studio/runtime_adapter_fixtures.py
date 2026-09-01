from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCompleted,
    RunStarted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import TextContent, ToolCallContent, ToolResultContent
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
        "schema_version": 2,
        "timestamp": 1.0,
        "run_id": handle.run_id,
        "scope_id": f"scope-{handle.run_id}",
    }
    # Canonical RuntimeEvent identity is session-scoped.  A fixture may be
    # reused for several turns in the same session, so model the real adapters
    # (whose stable ids include the execution scope) instead of reusing e1-e7.
    def event_id(ordinal: int) -> str:
        return f"{handle.run_id}:e{ordinal}"
    codex_source = SourceRef(framework="codex")
    yield RunStarted(
        event_id=event_id(1),
        seq=1,
        status="running",
        source=codex_source,
        **common,
    )
    yield ItemUpdated(
        event_id=event_id(2),
        seq=2,
        item_id="reasoning-1",
        item_kind="reasoning",
        op="append",
        update=TextContent(part_id="text-0", text="读取文件"),
        source=codex_source,
        **common,
    )
    tool_args = {
        "command": "sed -n '1,80p' src/demo.py",
        "cwd": str(request.config.get("cwd") or ""),
        "command_actions": [{"type": "read", "path": "src/demo.py"}],
    }
    yield ItemStarted(
        event_id=event_id(3),
        seq=3,
        item_id="tool-cmd-1",
        item_kind="tool_call",
        initial=ContentSnapshot(
            parts=(
                ToolCallContent(
                    part_id="tool-0",
                    call_id="cmd-1",
                    name="codex.command",
                    arguments=tool_args,
                ),
            )
        ),
        source=codex_source,
        **common,
    )
    yield ItemCompleted(
        event_id=event_id(4),
        seq=4,
        item_id="tool-cmd-1",
        item_kind="tool_call",
        snapshot=ContentSnapshot(
            parts=(
                ToolCallContent(
                    part_id="tool-0",
                    call_id="cmd-1",
                    name="codex.command",
                    arguments=tool_args,
                ),
                ToolResultContent(
                    part_id="tool-0",
                    call_id="cmd-1",
                    result={"status": "completed", "exit_code": 0, "duration_ms": 10},
                ),
            )
        ),
        source=codex_source,
        **common,
    )
    yield ItemUpdated(
        event_id=event_id(5),
        seq=5,
        item_id="msg-1",
        item_kind="message",
        op="append",
        update=TextContent(part_id="text-0", text="发现除零风险。"),
        source=codex_source,
        **common,
    )
    yield ItemCompleted(
        event_id=event_id(6),
        seq=6,
        item_id="msg-1",
        item_kind="message",
        snapshot=ContentSnapshot(
            parts=(
                TextContent(
                    part_id="text-0",
                    text="发现除零风险。请先检查空列表。",
                ),
            )
        ),
        source=codex_source,
        **common,
    )
    yield RunCompleted(
        event_id=event_id(7),
        seq=7,
        status="completed",
        output_refs=(
            OutputRef(
                scope_id=common["scope_id"],
                item_id="msg-1",
                part_id="text-0",
            ),
        ),
        source=SourceRef(framework="codex", metadata={"duration_ms": 25}),
        **common,
    )


__all__ = ["RuntimeFixture", "standard_codex_events"]
