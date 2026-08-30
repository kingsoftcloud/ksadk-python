from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, Optional

import pytest
from ag_ui.core import Context, RunAgentInput, Tool, UserMessage
from ag_ui_a2ui_toolkit import A2UI_SCHEMA_CONTEXT_DESCRIPTION
from fastapi import FastAPI

import ksadk.runtime as runtime_api
from ksadk.agui.a2ui_projection import project_a2ui_operations
from ksadk.agui.agent import KsadkAGUIAgent
from ksadk.agui.config import AGUIConfig
from ksadk.agui.routes import add_ksadk_agui_endpoint
from ksadk.conversations.message_projection import project_session_messages
from ksadk.events.canonical import (
    ApprovalRequest,
    ContentSnapshot,
    ErrorInfo,
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunStarted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import (
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.events.store import RuntimeEventStore, runtime_event_to_session_event
from ksadk.runtime.adapter import (
    BaseRuntime,
    CancelResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    RuntimeLaunchContext,
    RuntimeRegistry,
    StartRequest,
)
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter
from ksadk.sessions.in_memory import InMemorySessionService


class _Runtime(BaseRuntime):
    runtime_type = "fake"

    def native_capabilities(self):
        return {}


class _Adapter(RuntimeAdapter):
    def __init__(self):
        super().__init__(_Runtime())
        self.started: list[StartRequest] = []
        self.handles: dict[str, RunHandle] = {}
        self.streams: dict[str, list[RuntimeEvent]] = {}
        self.resumed: list[tuple[RunHandle, ResumeTarget, Optional[ResumePayload]]] = []
        self.cancelled: list[RunHandle] = []
        self.closed: list[RunHandle] = []
        self.resume_error: Optional[Exception] = None

    async def start(self, request: StartRequest) -> RunHandle:
        self.started.append(request)
        handle = RunHandle(
            run_id=str(request.metadata["invocation_id"]),
            session_id=request.session_id,
            runtime_type="fake",
            native_ref={
                "checkpoint_id": "checkpoint-1",
                "known_checkpoint_ids": ["checkpoint-1"],
            },
        )
        self.handles[request.session_id] = handle
        return handle

    def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        async def generate():
            for event in self.streams[handle.session_id]:
                yield event

        return generate()

    async def cancel(self, handle: RunHandle) -> CancelResult:
        self.cancelled.append(handle)
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    async def resume(self, handle, target, payload):
        if self.resume_error is not None:
            raise self.resume_error
        self.resumed.append((handle, target, payload))
        return handle

    async def checkpoint(self, handle):
        raise NotImplementedError

    async def close(self, handle):
        self.closed.append(handle)


def _executor_for(adapter: RuntimeAdapter):
    registry = RuntimeRegistry()
    registry.register("fake", lambda _context: adapter)
    return (
        runtime_api.RuntimeExecutor(registry),
        RuntimeLaunchContext(runtime_type="fake", project_dir="."),
    )


def _agent_for(adapter: RuntimeAdapter, *, name: str = "agent", **kwargs):
    executor, launch_context = _executor_for(adapter)
    return KsadkAGUIAgent(
        name=name,
        executor=executor,
        launch_context=launch_context,
        **kwargs,
    )


def test_agui_endpoint_uses_provided_runtime_adapter_without_runner_wrapping(
    monkeypatch,
) -> None:
    """防止 AG-UI 入口重新按框架选择 Runner 包装器。"""

    mounted: list[object] = []
    monkeypatch.setattr("ksadk.agui.routes.require_agui_dependencies", lambda: None)
    monkeypatch.setattr(
        "ksadk.agui.routes._fastapi_endpoint_helper",
        lambda: lambda _app, agent, *, path: mounted.append((agent, path)),
    )
    adapter = _Adapter()
    executor, launch_context = _executor_for(adapter)

    agent = add_ksadk_agui_endpoint(
        FastAPI(),
        executor,
        launch_context,
        AGUIConfig(enabled=True, agent_name="agent"),
    )

    assert agent._shared.executor is executor
    assert agent._shared.launch_context is launch_context
    assert mounted == [(agent, "/agentengine/agui")]


# ---- canonical event helpers ----

_RUN_ID = "run-1"
_SCOPE_ID = "scope-1"
_SESSION_ID = "thread-1"


def _source(**extra) -> SourceRef:
    return SourceRef(framework="ksadk", **extra)


def _common(seq: int, **extra) -> dict[str, Any]:
    kw = {
        "schema_version": 2,
        "event_id": f"evt-{seq}",
        "seq": seq,
        "timestamp": 1.0,
        "run_id": _RUN_ID,
        "scope_id": _SCOPE_ID,
        "source": _source(),
    }
    kw.update(extra)
    return kw


def _events(*items) -> list[RuntimeEvent]:
    """Flatten a mix of single events and lists into a flat list."""
    result: list[RuntimeEvent] = []
    for item in items:
        if isinstance(item, list):
            result.extend(item)
        else:
            result.append(item)
    return result


def _run_started(seq: int = 1) -> RunStarted:
    return RunStarted(status="running", **_common(seq))


def _run_completed(seq: int = 99) -> RunCompleted:
    return RunCompleted(
        status="completed",
        output_refs=(),
        **_common(seq),
    )


def _run_interrupted(seq: int = 98, reason: str = "input_required") -> RunInterrupted:
    return RunInterrupted(
        status="interrupted",
        reason=reason,
        **_common(seq),
    )


def _reasoning_delta(text: str, seq: int) -> ItemUpdated:
    return ItemUpdated(
        item_id="reasoning-1",
        item_kind="reasoning",
        op="append",
        update=TextContent(part_id=f"text-{seq}", text=text),
        **_common(seq),
    )


def _text_delta(text: str, seq: int) -> ItemUpdated:
    return ItemUpdated(
        item_id="msg-1",
        item_kind="message",
        op="append",
        update=TextContent(part_id="text-0", text=text),
        **_common(seq),
    )


def _text_completed(text: str, seq: int) -> ItemCompleted:
    return ItemCompleted(
        item_id="msg-1",
        item_kind="message",
        snapshot=ContentSnapshot(parts=(TextContent(part_id="text-0", text=text),)),
        **_common(seq),
    )


def _tool_call_begin(call_id: str, name: str, args: dict, seq: int) -> ItemStarted:
    return ItemStarted(
        item_id=f"tool-{call_id}",
        item_kind="tool_call",
        initial=ContentSnapshot(
            parts=(ToolCallContent(part_id=call_id, call_id=call_id, name=name, arguments=args),)
        ),
        **_common(seq),
    )


def _tool_call_end(call_id: str, name: str, result: Any, seq: int) -> list[RuntimeEvent]:
    """TOOL_CALL_END maps to TWO canonical events: ItemCompleted(tool_call) + ItemCompleted(tool_result)."""
    return [
        ItemCompleted(
            item_id=f"tool-{call_id}",
            item_kind="tool_call",
            snapshot=ContentSnapshot(
                parts=(ToolCallContent(part_id=call_id, call_id=call_id, name=name, arguments={}),)
            ),
            **_common(seq),
        ),
        ItemCompleted(
            item_id=f"tool-{call_id}-result",
            item_kind="tool_result",
            snapshot=ContentSnapshot(
                parts=(ToolResultContent(part_id=call_id, call_id=call_id, result=result),)
            ),
            **_common(seq + 1),
        ),
    ]


def _approval_requested(
    approval_id: str,
    call_id: str,
    kind: str,
    seq: int,
    *,
    detail: Any = None,
) -> InteractionRequested:
    return InteractionRequested(
        interaction_id=approval_id,
        interaction_kind="approval",
        request=ApprovalRequest(
            call_id=call_id,
            kind=kind,
            detail=detail or {},
        ),
        **_common(seq),
    )


def _a2ui_surface_begin(surface_id: str, surface_data: dict, seq: int) -> ItemStarted:
    return ItemStarted(
        item_id=f"a2ui-{surface_id}",
        item_kind="data",
        initial=ContentSnapshot(
            parts=(DataContent(part_id="a2ui-surface", data=surface_data),)
        ),
        source=SourceRef(
            framework="ksadk",
            protocol="a2ui",
            metadata={"surface_id": surface_id},
        ),
        **{k: v for k, v in _common(seq).items() if k != "source"},
    )


def _a2ui_surface_update(surface_id: str, operations: list, seq: int) -> ItemUpdated:
    return ItemUpdated(
        item_id=f"a2ui-{surface_id}",
        item_kind="data",
        op="replace",
        update=DataContent(part_id="a2ui-surface", data=operations),
        source=SourceRef(
            framework="ksadk",
            protocol="a2ui",
            metadata={"surface_id": surface_id},
        ),
        **{k: v for k, v in _common(seq).items() if k != "source"},
    )


def _a2ui_surface_end(surface_id: str, seq: int) -> ItemCompleted:
    return ItemCompleted(
        item_id=f"a2ui-{surface_id}",
        item_kind="data",
        snapshot=ContentSnapshot(parts=()),
        source=SourceRef(
            framework="ksadk",
            protocol="a2ui",
            metadata={"surface_id": surface_id},
        ),
        **{k: v for k, v in _common(seq).items() if k != "source"},
    )


def _input(*, run_id="run-1", resume=None):
    return RunAgentInput(
        threadId="thread-1",
        runId=run_id,
        state={},
        messages=[UserMessage(id="u1", content="hello")],
        tools=[],
        context=[],
        forwardedProps={"userId": "user-1"},
        resume=resume,
    )


@pytest.mark.asyncio
async def test_runner_runtime_adapter_emits_reasoning_tool_and_terminal_contract():
    class _ChunkRunner:
        def __init__(self):
            self.received = []

        async def stream(self, input_data):
            self.received.append(input_data)
            yield {"type": "thinking", "delta": "plan"}
            yield {
                "type": "tool_call",
                "run_id": "tool-1",
                "tool_name": "search",
                "tool_args": {"q": "x"},
            }
            yield {
                "type": "tool_result",
                "run_id": "tool-1",
                "tool_name": "search",
                "tool_output": {"ok": True},
            }
            yield {"type": "graph_update", "node": "gate", "output": {"message": object()}}
            yield {"type": "final", "output": "done"}

    runner = _ChunkRunner()
    adapter = RunnerRuntimeAdapter(runner, runtime_type="ksadk")
    handle = await adapter.start(
        StartRequest(
            input="go",
            user_id="u",
            session_id="s",
            config={"ag-ui": {"inject_a2ui_tool": True}},
        )
    )
    events = [event async for event in adapter.stream(handle)]

    # canonical event types from dict-chunk adapter
    # auto-close: open reasoning item is closed before final_answer starts
    assert [event.event_type for event in events] == [
        "run.started",
        "item.started",       # reasoning (ensure_started)
        "item.updated",        # reasoning delta
        "item.started",       # tool_call
        "item.completed",     # tool_call
        "item.started",       # tool_result
        "item.completed",     # tool_result
        "run.progress",       # graph_update
        "item.completed",     # auto-close reasoning before final_answer
        "item.started",       # message (final_answer, ensure_started)
        "item.completed",     # message (final)
        "run.completed",
    ]
    assert runner.received[0]["ag-ui"] == {"inject_a2ui_tool": True}
    for event in events:
        json.dumps(event.model_dump(mode="json", by_alias=True, exclude_none=True))


@pytest.mark.asyncio
async def test_runner_runtime_adapter_prefers_canonical_runtime_event_stream():
    """A native Runtime must not be flattened to dict chunks and parsed again."""

    class _NativeEventRunner:
        def stream(self, _input_data):
            raise AssertionError("legacy chunk stream must not be used")

        async def stream_canonical_events(self, _input_data):
            common = {
                "schema_version": 2,
                "timestamp": 1.0,
                "run_id": "native-run",
                "scope_id": "scope-native-run",
                "source": SourceRef(framework="ksadk"),
            }
            yield ItemCompleted(
                event_id="e1",
                seq=1,
                item_id="msg-1",
                item_kind="message",
                snapshot=ContentSnapshot(
                    parts=(TextContent(part_id="text-0", text="done"),)
                ),
                **{k: v for k, v in common.items() if k != "source"},
                source=SourceRef(framework="ksadk"),
            )
            yield RunCompleted(
                event_id="e2",
                seq=2,
                status="completed",
                output_refs=(),
                source=SourceRef(framework="ksadk", metadata={"duration_ms": 42}),
                **{k: v for k, v in common.items() if k != "source"},
            )

    adapter = RunnerRuntimeAdapter(_NativeEventRunner(), runtime_type="ksadk")
    handle = await adapter.start(
        StartRequest(
            input="go",
            user_id="outer-user",
            session_id="outer-session",
            agent_id="outer-agent",
            metadata={"invocation_id": "outer-run"},
        )
    )
    events = [event async for event in adapter.stream(handle)]

    # adapter emits its own RunStarted (suppressed from native stream),
    # forwards the ItemCompleted and RunCompleted from the native runner.
    assert [event.event_type for event in events] == [
        "run.started",       # adapter's own
        "item.completed",    # forwarded from native (text completed)
        "run.completed",     # forwarded from native
    ]
    assert events[0].run_id == "outer-run"  # adapter's RunStarted uses handle run_id
    # forwarded events keep their own run_id from the native runner
    assert events[1].run_id == "native-run"
    assert events[2].run_id == "native-run"
    assert events[2].source.metadata.get("duration_ms") == 42


@pytest.mark.asyncio
async def test_runner_runtime_adapter_projects_a2ui_tool_envelope_as_canonical_surface_event():
    class _ChunkRunner:
        async def stream(self, _input_data):
            yield {
                "type": "tool_result",
                "run_id": "a2ui-call",
                "tool_name": "generate_a2ui",
                "tool_output": json.dumps(
                    {
                        "a2ui_operations": [
                            {
                                "version": "v0.9",
                                "createSurface": {
                                    "surfaceId": "component-status",
                                    "catalogId": "catalog-1",
                                },
                            },
                            {
                                "version": "v0.9",
                                "updateComponents": {
                                    "surfaceId": "component-status",
                                    "components": [
                                        {"id": "root", "component": "Text", "text": "ready"}
                                    ],
                                },
                            },
                        ]
                    }
                ),
            }
            yield {"type": "final", "output": "done"}

    adapter = RunnerRuntimeAdapter(_ChunkRunner(), runtime_type="ksadk")
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
    events = [event async for event in adapter.stream(handle)]

    surface = next(
        event for event in events
        if isinstance(event, ItemStarted) and event.item_kind == "data"
    )
    assert surface.source.protocol == "a2ui"
    assert surface.source.metadata["surface_id"] == "component-status"
    operations = surface.initial.parts[0].data
    assert operations[1]["updateComponents"]["components"][0]["id"] == "root"


@pytest.mark.asyncio
async def test_projects_text_reasoning_tools_and_terminal_with_stable_ids():
    adapter = _Adapter()
    adapter.streams["thread-1"] = _events(
        _run_started(seq=1),
        _reasoning_delta("think", seq=2),
        _tool_call_begin("tool-1", "search", {"q": "x"}, seq=3),
        _tool_call_end("tool-1", "search", {"ok": True}, seq=4),
        _text_completed("done", seq=6),
        _run_completed(seq=7),
    )
    agent = _agent_for(adapter)

    events = [event async for event in agent.run(_input())]
    types = [event.type.value for event in events]

    assert types[0] == "RUN_STARTED"
    assert types[-1] == "RUN_FINISHED"
    assert "REASONING_MESSAGE_CONTENT" in types
    assert "TOOL_CALL_START" in types
    assert "TOOL_CALL_RESULT" in types
    assert (
        types.index("TOOL_CALL_START")
        < types.index("TOOL_CALL_END")
        < types.index("TOOL_CALL_RESULT")
    )
    assert "TEXT_MESSAGE_CONTENT" in types
    assert adapter.started[0].session_id == "thread-1"
    assert adapter.started[0].metadata["invocation_id"] == "run-1"
    assert adapter.started[0].user_id == "user-1"
    assert adapter.closed == [adapter.handles["thread-1"]]


@pytest.mark.asyncio
async def test_final_text_snapshot_does_not_duplicate_streamed_delta():
    adapter = _Adapter()
    adapter.streams["thread-1"] = _events(
        _text_delta("OK", seq=1),
        _text_completed("OK", seq=2),
        _run_completed(seq=3),
    )
    agent = _agent_for(adapter)

    events = [event async for event in agent.run(_input())]
    deltas = [event.delta for event in events if event.type.value == "TEXT_MESSAGE_CONTENT"]

    assert deltas == ["OK"]


@pytest.mark.asyncio
async def test_agui_first_user_turn_primes_session_title_metadata():
    service = InMemorySessionService()
    await service.create_session("agent", "user-1", "thread-1")
    adapter = _Adapter()
    adapter.streams["thread-1"] = _events(_run_completed(seq=1))
    agent = _agent_for(
        adapter,
        event_store_factory=lambda: RuntimeEventStore(service),
        session_service_factory=lambda: service,
    )

    _ = [event async for event in agent.run(_input())]

    session = await service.get_session("thread-1")
    assert session is not None
    assert session.first_prompt == "hello"
    assert session.last_prompt == "hello"
    assert session.title == "hello"
    assert session.title_source == "fallback_first_prompt"


@pytest.mark.asyncio
async def test_agui_interrupt_exposes_tool_context_for_an_actionable_card():
    adapter = _Adapter()
    adapter.streams["thread-1"] = _events(
        _approval_requested(
            "approval-1",
            "approval-1",
            "tool",
            seq=1,
            detail={
                "approval_requests": {
                    "action_requests": [
                        {
                            "name": "run_command",
                            "args": {"command": "pwd"},
                            "description": "Elevated sandbox command approval",
                            "approval_level": "elevated",
                        }
                    ]
                }
            },
        ),
        _run_interrupted(seq=2),
    )
    agent = _agent_for(adapter)

    events = [event async for event in agent.run(_input())]
    interrupt = events[-1].outcome.interrupts[0]

    assert interrupt.tool_call_id == "approval-1"
    assert interrupt.message == "Elevated sandbox command approval"
    assert interrupt.metadata == {
        "tool_name": "run_command",
        "arguments": {"command": "pwd"},
        "approval_level": "elevated",
    }


@pytest.mark.asyncio
async def test_catalog_tools_and_injection_flag_reach_the_existing_runner_state():
    adapter = _Adapter()
    adapter.streams["thread-1"] = _events(_run_completed(seq=1))
    agent = _agent_for(adapter)
    input_data = _input().model_copy(
        update={
            "tools": [Tool(name="frontend_action", description="action", parameters={})],
            "context": [
                Context(description=A2UI_SCHEMA_CONTEXT_DESCRIPTION, value='{"Button": {}}'),
                Context(description="tenant", value="acme"),
            ],
            "forwarded_props": {"injectA2UITool": True},
        }
    )

    _ = [event async for event in agent.run(input_data)]
    config = adapter.started[0].config

    assert config["ag-ui"] == {
        "tools": [{"name": "frontend_action", "description": "action", "parameters": {}}],
        "context": [{"description": "tenant", "value": "acme"}],
        "a2ui_schema": '{"Button": {}}',
        "inject_a2ui_tool": True,
    }
    assert config["copilotkit"]["actions"][0]["name"] == "frontend_action"


@pytest.mark.asyncio
async def test_resume_finds_original_handle_and_preserves_falsy_payload():
    adapter = _Adapter()
    adapter.streams["thread-1"] = _events(
        _approval_requested(
            "interrupt-1",
            "interrupt-1",
            "tool",
            seq=1,
            detail={"message": "approve?"},
        ),
        _run_interrupted(seq=2),
    )
    agent = _agent_for(adapter)
    first = [event async for event in agent.run(_input())]
    original = adapter.handles["thread-1"]
    assert first[-1].type.value == "RUN_FINISHED"
    assert first[-1].outcome.type == "interrupt"

    adapter.streams["thread-1"] = _events(
        _text_completed("resumed", seq=3),
        _run_completed(seq=4),
    )
    resumed_input = _input(
        run_id="run-2",
        resume=[{"interruptId": "interrupt-1", "status": "resolved", "payload": False}],
    )
    resumed = [event async for event in agent.run(resumed_input)]

    assert resumed[-1].type.value == "RUN_FINISHED"
    handle, target, payload = adapter.resumed[0]
    assert handle is original
    assert target == ResumeTarget(kind="checkpoint_id", id="checkpoint-1")
    assert payload is not None
    assert payload.call_id == "interrupt-1"
    assert payload.data is False


@pytest.mark.asyncio
async def test_unknown_or_incomplete_resume_is_rejected_without_corrupting_handle():
    adapter = _Adapter()
    adapter.streams["thread-1"] = _events(
        _approval_requested("interrupt-1", "interrupt-1", "tool", seq=1),
        _run_interrupted(seq=2),
    )
    agent = _agent_for(adapter)
    _ = [event async for event in agent.run(_input())]

    invalid = _input(
        run_id="run-2",
        resume=[{"interruptId": "unknown", "status": "resolved", "payload": "yes"}],
    )
    events = [event async for event in agent.run(invalid)]

    assert events[-1].type.value == "RUN_ERROR"
    assert not adapter.resumed
    assert adapter.handles["thread-1"] not in adapter.closed


@pytest.mark.asyncio
async def test_failed_resume_does_not_consume_interrupt_and_can_be_retried():
    adapter = _Adapter()
    adapter.streams["thread-1"] = _events(
        _approval_requested("interrupt-1", "interrupt-1", "tool", seq=1),
        _run_interrupted(seq=2),
    )
    agent = _agent_for(adapter)
    _ = [event async for event in agent.run(_input())]
    resume_input = _input(
        run_id="run-2",
        resume=[{"interruptId": "interrupt-1", "status": "resolved", "payload": 0}],
    )

    adapter.resume_error = RuntimeError("database url contains secret")
    failed = [event async for event in agent.run(resume_input)]
    assert failed[-1].type.value == "RUN_ERROR"
    assert "secret" not in failed[-1].message

    adapter.resume_error = None
    adapter.streams["thread-1"] = _events(_run_completed(seq=3))
    retried = [event async for event in agent.run(resume_input)]
    assert retried[-1].type.value == "RUN_FINISHED"
    assert adapter.resumed[-1][2].data == 0


@pytest.mark.asyncio
async def test_durable_replay_restores_pending_interrupt_and_resumes_once():
    class _AttachableAdapter(_Adapter):
        def __init__(self):
            super().__init__()
            self.attached: list[RunHandle] = []

        def is_handle_attached(self, handle: RunHandle) -> bool:
            return handle in self.attached

        async def attach(self, handle: RunHandle) -> RunHandle:
            self.attached.append(handle)
            return handle

    service = InMemorySessionService()
    await service.create_session("agent", "user-1", "thread-1")
    store = RuntimeEventStore(service)
    first_adapter = _Adapter()
    first_adapter.streams["thread-1"] = _events(
        _approval_requested(
            "interrupt-1", "interrupt-1", "tool", seq=1, detail={"message": "approve?"}
        ),
        _run_interrupted(seq=2),
    )
    first_agent = _agent_for(
        first_adapter,
        event_store_factory=lambda: store,
    )
    _ = [event async for event in first_agent.run(_input())]

    restarted_adapter = _AttachableAdapter()
    restarted_adapter.streams["thread-1"] = _events(
        _text_completed("resumed", seq=3),
        _run_completed(seq=4),
    )
    restarted_agent = _agent_for(
        restarted_adapter,
        event_store_factory=lambda: RuntimeEventStore(service),
    )
    resume_input = _input(
        run_id="run-2",
        resume=[
            {
                "interruptId": "interrupt-1",
                "status": "resolved",
                "payload": {"approve": True},
            }
        ],
    )

    resumed = [event async for event in restarted_agent.run(resume_input)]
    duplicate_adapter = _AttachableAdapter()
    duplicate_agent = _agent_for(
        duplicate_adapter,
        event_store_factory=lambda: RuntimeEventStore(service),
    )
    duplicate = [event async for event in duplicate_agent.run(resume_input)]

    assert resumed[-1].outcome.type == "success"
    assert len(restarted_adapter.attached) == 1
    assert len(restarted_adapter.resumed) == 1
    assert duplicate[-1].result == {"status": "already_resumed"}
    assert not duplicate_adapter.resumed


@pytest.mark.asyncio
async def test_agui_runtime_events_project_to_refreshable_history():
    events = [
        RunStarted(
            schema_version=2,
            event_id="input-1",
            seq=1,
            timestamp=1.0,
            run_id="run-1",
            scope_id="scope-run-1",
            source=SourceRef(
                framework="ksadk",
                metadata={"input": "hello", "source": "ag-ui"},
            ),
            status="running",
        ),
        _approval_requested(
            "interrupt-1",
            "interrupt-1",
            "tool",
            seq=2,
            detail={"tool_name": "shell", "arguments": {"cmd": "echo ok"}},
        ).model_copy(
            update={
                "source": SourceRef(
                    framework="ksadk",
                    metadata={"protocol": "ag-ui"},
                )
            }
        ),
        _run_interrupted(seq=3),
        InteractionResolved(
            schema_version=2,
            event_id="evt-4",
            seq=4,
            timestamp=1.0,
            run_id="run-2",
            scope_id="scope-run-2",
            source=SourceRef(framework="ksadk"),
            interaction_id="interrupt-1",
            interaction_kind="approval",
            response={"response_type": "approval", "decision": "approved", "data": {"decision": "approve"}},
        ),
        _text_completed("done", seq=5),
        _a2ui_surface_begin(
            "surface-1",
            {
                "surface_id": "surface-1",
                "catalog_id": "catalog-1",
                "components": [
                    {
                        "version": "v0.9",
                        "createSurface": {
                            "surfaceId": "surface-1",
                            "catalogId": "catalog-1",
                        },
                    }
                ],
            },
            seq=6,
        ),
    ]
    serialized = []
    for event in events:
        stored = runtime_event_to_session_event("thread-1", event)
        serialized.append(
            {
                "EventId": stored.id,
                "EventType": stored.event_type,
                "Content": stored.content,
                "Metadata": stored.metadata,
                "Timestamp": stored.timestamp,
                "SeqId": stored.seq_id,
                "InvocationId": stored.invocation_id,
            }
        )

    messages = project_session_messages(serialized, include_tool_events=True)

    assert [(message["Role"], message["Content"]["text"]) for message in messages] == [
        ("user", "hello"),
        ("assistant", ""),
        ("assistant", "done"),
    ]
    approval = messages[1]["ToolEvents"][0]
    assert approval == {
        "SeqId": 2,
        "Type": "approval",
        "Protocol": "ag-ui",
        "Name": "shell",
        "Status": "approved",
        "ApprovalRequestId": "interrupt-1",
        "Args": {"cmd": "echo ok"},
    }
    assert messages[2]["Activities"][0]["Content"] == {
        "surfaceId": "surface-1",
        "a2ui_operations": [
            {
                "version": "v0.9",
                "createSurface": {"surfaceId": "surface-1", "catalogId": "catalog-1"},
            }
        ],
    }


@pytest.mark.asyncio
async def test_successful_resume_persists_approval_resolved_for_replay():
    class _Store:
        def __init__(self):
            self.events = []

        async def append_one(self, session_id, event):
            self.events.append(event)
            return event

    store = _Store()
    adapter = _Adapter()
    adapter.streams["thread-1"] = _events(
        _approval_requested("interrupt-1", "interrupt-1", "tool", seq=1),
        _run_interrupted(seq=2),
    )
    agent = _agent_for(
        adapter,
        event_store_factory=lambda: store,
    )
    _ = [event async for event in agent.run(_input())]
    requested = [
        event for event in store.events if isinstance(event, InteractionRequested)
    ]
    assert requested[0].source.metadata.get("protocol") == "ag-ui"
    adapter.streams["thread-1"] = _events(_run_completed(seq=3))

    _ = [
        event
        async for event in agent.run(
            _input(
                run_id="run-2",
                resume=[
                    {
                        "interruptId": "interrupt-1",
                        "status": "resolved",
                        "payload": {"decision": "approve"},
                    }
                ],
            )
        )
    ]

    resolved = [event for event in store.events if isinstance(event, InteractionResolved)]
    assert len(resolved) == 1
    assert resolved[0].interaction_id == "interrupt-1"
    assert resolved[0].interaction_kind == "approval"
    assert resolved[0].response.decision == "approved"
    assert resolved[0].source.metadata.get("protocol") == "ag-ui"


@pytest.mark.asyncio
async def test_projects_standard_runtime_a2ui_events_as_official_agui_activities():
    class _Store:
        def __init__(self):
            self.events = []

        async def append_one(self, session_id, event):
            self.events.append(event)
            return event

    store = _Store()
    adapter = _Adapter()
    surface_data = {
        "surface_id": "surface-1",
        "catalog_id": "catalog-1",
        "components": [
            {
                "component_id": "root",
                "type": "Text",
                "props": {"text": "hello"},
                "children": [],
            }
        ],
        "data_model": {"ready": True},
    }
    adapter.streams["thread-1"] = _events(
        _a2ui_surface_begin("surface-1", surface_data, seq=1),
        _a2ui_surface_update(
            "surface-1",
            [
                {
                    "version": "v0.9",
                    "updateDataModel": {
                        "surfaceId": "surface-1",
                        "path": "/",
                        "value": {"ready": False},
                    },
                }
            ],
            seq=2,
        ),
        _a2ui_surface_end("surface-1", seq=3),
        _run_completed(seq=4),
    )
    agent = _agent_for(adapter, event_store_factory=lambda: store)

    events = [event async for event in agent.run(_input())]
    activities = [event for event in events if event.type.value == "ACTIVITY_SNAPSHOT"]
    assert len(activities) == 3
    assert activities[0].activity_type == "a2ui-surface"
    assert activities[0].content["surfaceId"] == "surface-1"
    operations = activities[0].content["a2ui_operations"]
    assert operations[0]["createSurface"] == {
        "surfaceId": "surface-1",
        "catalogId": "catalog-1",
    }
    assert operations[1]["updateComponents"]["components"][0]["id"] == "root"
    assert activities[1].content["a2ui_operations"][0]["updateDataModel"]["value"] == {
        "ready": False
    }
    assert activities[2].content["a2ui_operations"][0]["deleteSurface"] == {
        "surfaceId": "surface-1"
    }
    # store received canonical events (RunStarted from user input + A2UI items + RunCompleted)
    assert [event.event_type for event in store.events] == [
        "run.started",
        "item.started",
        "item.updated",
        "item.completed",
        "run.completed",
    ]


def test_a2ui_projection_defaults_to_the_official_basic_catalog_id():
    operations = project_a2ui_operations(
        "a2ui.surface.begin",
        {"surface_id": "surface-1", "components": []},
    )

    assert operations == [
        {
            "version": "v0.9",
            "createSurface": {
                "surfaceId": "surface-1",
                "catalogId": "https://a2ui.org/specification/v0_9/basic_catalog.json",
            },
        }
    ]


@pytest.mark.asyncio
async def test_clone_shares_adapter_but_two_threads_are_isolated():
    adapter = _Adapter()
    adapter.streams["thread-1"] = _events(_run_completed(seq=1))
    agent = _agent_for(adapter)
    clone = agent.clone()
    second = _input(run_id="run-2").model_copy(update={"thread_id": "thread-2"})
    adapter.streams["thread-2"] = [
        RunCompleted(
            schema_version=2,
            event_id="evt-run2-completed",
            seq=1,
            timestamp=1.0,
            run_id="run-2",
            scope_id="scope-run-2",
            source=SourceRef(framework="ksadk"),
            status="completed",
            output_refs=(),
        )
    ]

    await asyncio.gather(
        *[
            _collect(agent.run(_input())),
            _collect(clone.run(second)),
        ]
    )

    assert {request.session_id for request in adapter.started} == {"thread-1", "thread-2"}


@pytest.mark.asyncio
async def test_disconnect_cancels_and_closes_the_same_handle():
    class _BlockingAdapter(_Adapter):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        def stream(self, handle):
            async def generate():
                self.entered.set()
                await self.release.wait()
                if False:
                    yield None

            return generate()

    adapter = _BlockingAdapter()
    agent = _agent_for(adapter)
    consume = asyncio.create_task(_collect(agent.run(_input())))
    await asyncio.wait_for(adapter.entered.wait(), timeout=1)

    consume.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consume

    handle = adapter.handles["thread-1"]
    assert adapter.cancelled == [handle]
    assert adapter.closed == [handle]


async def _collect(events):
    return [event async for event in events]


# ------------------------- typed capability matrix (agent-kernel Task 5) ----


@pytest.mark.asyncio
async def test_adapter_capability_matrix_defaults_to_honest_unavailable():
    adapter = _Adapter()
    matrix = adapter.capabilities()
    for name in (
        "cancel",
        "pause",
        "resume",
        "submit_interaction",
        "attach",
        "steer",
        "inject",
        "checkpoint",
        "durable_restore",
    ):
        capability = getattr(matrix, name)
        assert capability.supported is False, name
        assert capability.mode == "unavailable", name
        expected_reason = "not_implemented"
        if name == "steer":
            expected_reason = "runtime_no_native_steer"
        elif name == "inject":
            expected_reason = "runtime_no_native_inject"
        assert capability.reason == expected_reason, name


@pytest.mark.asyncio
async def test_adapter_unsupported_control_verbs_fail_closed():
    from ksadk.kernel.errors import UnsupportedControlError

    adapter = _Adapter()
    handle = RunHandle(run_id="r", session_id="s", runtime_type="fake")
    with pytest.raises(UnsupportedControlError):
        await adapter.submit(handle, ResumePayload(kind="free_text"))
    with pytest.raises(UnsupportedControlError):
        await adapter.attach(handle)
    with pytest.raises(UnsupportedControlError):
        await adapter.steer(handle, {"text": "more"})
    with pytest.raises(UnsupportedControlError):
        await adapter.inject(handle, {"text": "more"})
    with pytest.raises(UnsupportedControlError):
        await adapter.durable_restore(handle)
    # 旧 API 是 matrix 的单向投影(未覆写 capabilities 的 adapter 全 unavailable)。
    legacy = adapter.native_capabilities()
    assert legacy["cancel"] is False
    assert legacy["steer"] is False
