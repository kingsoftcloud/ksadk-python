from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Optional

import pytest
from ag_ui.core import Context, RunAgentInput, Tool, UserMessage
from ag_ui_a2ui_toolkit import A2UI_SCHEMA_CONTEXT_DESCRIPTION

from ksadk.agui.a2ui_projection import project_a2ui_operations
from ksadk.agui.agent import KsadkAGUIAgent
from ksadk.conversations.message_projection import project_session_messages
from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.events.store import RuntimeEventStore, runtime_event_to_session_event
from ksadk.runtime.adapter import (
    BaseRuntime,
    CancelResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
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


def _runtime_event(event_type: str, payload: dict, *, seq: int) -> RuntimeEvent:
    phase = (
        "commentary" if event_type in {EventType.TEXT_DELTA, EventType.REASONING_DELTA} else None
    )
    if event_type == EventType.TEXT_COMPLETED:
        phase = "final_answer"
    return RuntimeEvent.create(
        event_type,
        agent_id="agent",
        user_id="user",
        session_id="thread-1",
        invocation_id="run-1",
        seq_id=seq,
        payload=payload,
        phase=phase,
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
    adapter = RunnerRuntimeAdapter(runner, runtime_type="fixture")
    handle = await adapter.start(
        StartRequest(
            input="go",
            user_id="u",
            session_id="s",
            config={"ag-ui": {"inject_a2ui_tool": True}},
        )
    )
    events = [event async for event in adapter.stream(handle)]

    assert [event.event_type for event in events] == [
        EventType.RUN_STARTED,
        EventType.REASONING_DELTA,
        EventType.TOOL_CALL_BEGIN,
        EventType.TOOL_CALL_END,
        EventType.RUN_PROGRESS,
        EventType.TEXT_COMPLETED,
        EventType.RUN_COMPLETED,
    ]
    assert runner.received[0]["ag-ui"] == {"inject_a2ui_tool": True}
    for event in events:
        json.dumps(event.to_dict())


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

    adapter = RunnerRuntimeAdapter(_ChunkRunner(), runtime_type="fixture")
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
    events = [event async for event in adapter.stream(handle)]

    surface = next(event for event in events if event.event_type == EventType.A2UI_SURFACE_BEGIN)
    assert surface.payload["surface_id"] == "component-status"
    assert surface.payload["operations"][1]["updateComponents"]["components"][0]["id"] == "root"


@pytest.mark.asyncio
async def test_projects_text_reasoning_tools_and_terminal_with_stable_ids():
    adapter = _Adapter()
    adapter.streams["thread-1"] = [
        _runtime_event(EventType.RUN_STARTED, {"status": "in_progress"}, seq=1),
        _runtime_event(EventType.REASONING_DELTA, {"text": "think"}, seq=2),
        _runtime_event(
            EventType.TOOL_CALL_BEGIN,
            {"call_id": "tool-1", "name": "search", "args": {"q": "x"}},
            seq=3,
        ),
        _runtime_event(
            EventType.TOOL_CALL_END,
            {"call_id": "tool-1", "name": "search", "result": {"ok": True}},
            seq=4,
        ),
        _runtime_event(EventType.TEXT_COMPLETED, {"text": "done"}, seq=5),
        _runtime_event(EventType.RUN_COMPLETED, {"status": "completed"}, seq=6),
    ]
    agent = KsadkAGUIAgent(name="agent", adapter=adapter)

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
    adapter.streams["thread-1"] = [
        _runtime_event(EventType.TEXT_DELTA, {"text": "OK"}, seq=1),
        _runtime_event(EventType.TEXT_COMPLETED, {"text": "OK"}, seq=2),
        _runtime_event(EventType.RUN_COMPLETED, {"status": "completed"}, seq=3),
    ]
    agent = KsadkAGUIAgent(name="agent", adapter=adapter)

    events = [event async for event in agent.run(_input())]
    deltas = [event.delta for event in events if event.type.value == "TEXT_MESSAGE_CONTENT"]

    assert deltas == ["OK"]


@pytest.mark.asyncio
async def test_agui_first_user_turn_primes_session_title_metadata():
    service = InMemorySessionService()
    await service.create_session("agent", "user-1", "thread-1")
    adapter = _Adapter()
    adapter.streams["thread-1"] = [
        _runtime_event(EventType.RUN_COMPLETED, {"status": "completed"}, seq=1)
    ]
    agent = KsadkAGUIAgent(
        name="agent",
        adapter=adapter,
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
    adapter.streams["thread-1"] = [
        _runtime_event(
            EventType.APPROVAL_REQUESTED,
            {
                "approval_id": "approval-1",
                "call_id": "approval-1",
                "kind": "tool",
                "detail": {
                    "approval_requests": {
                        "action_requests": [
                            {
                                "name": "run_command",
                                "args": {"command": "pwd"},
                                "description": "Elevated sandbox command approval",
                                "approval_level": "elevated",
                            }
                        ]
                    },
                },
            },
            seq=1,
        ),
        _runtime_event(EventType.RUN_INTERRUPTED, {"status": "input_required"}, seq=2),
    ]
    agent = KsadkAGUIAgent(name="agent", adapter=adapter)

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
    adapter.streams["thread-1"] = [
        _runtime_event(EventType.RUN_COMPLETED, {"status": "completed"}, seq=1)
    ]
    agent = KsadkAGUIAgent(name="agent", adapter=adapter)
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
    adapter.streams["thread-1"] = [
        _runtime_event(
            EventType.APPROVAL_REQUESTED,
            {
                "approval_id": "interrupt-1",
                "call_id": "interrupt-1",
                "kind": "tool",
                "detail": {"message": "approve?"},
            },
            seq=1,
        ),
        _runtime_event(EventType.RUN_INTERRUPTED, {"status": "input_required"}, seq=2),
    ]
    agent = KsadkAGUIAgent(name="agent", adapter=adapter)
    first = [event async for event in agent.run(_input())]
    original = adapter.handles["thread-1"]
    assert first[-1].type.value == "RUN_FINISHED"
    assert first[-1].outcome.type == "interrupt"

    adapter.streams["thread-1"] = [
        _runtime_event(EventType.TEXT_COMPLETED, {"text": "resumed"}, seq=3),
        _runtime_event(EventType.RUN_COMPLETED, {"status": "completed"}, seq=4),
    ]
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
    adapter.streams["thread-1"] = [
        _runtime_event(
            EventType.APPROVAL_REQUESTED,
            {
                "approval_id": "interrupt-1",
                "call_id": "interrupt-1",
                "kind": "tool",
            },
            seq=1,
        ),
        _runtime_event(EventType.RUN_INTERRUPTED, {"status": "input_required"}, seq=2),
    ]
    agent = KsadkAGUIAgent(name="agent", adapter=adapter)
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
    adapter.streams["thread-1"] = [
        _runtime_event(
            EventType.APPROVAL_REQUESTED,
            {"approval_id": "interrupt-1", "call_id": "interrupt-1", "kind": "tool"},
            seq=1,
        ),
        _runtime_event(EventType.RUN_INTERRUPTED, {"status": "input_required"}, seq=2),
    ]
    agent = KsadkAGUIAgent(name="agent", adapter=adapter)
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
    adapter.streams["thread-1"] = [
        _runtime_event(EventType.RUN_COMPLETED, {"status": "completed"}, seq=3)
    ]
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
    first_adapter.streams["thread-1"] = [
        _runtime_event(
            EventType.APPROVAL_REQUESTED,
            {
                "approval_id": "interrupt-1",
                "call_id": "interrupt-1",
                "kind": "tool",
                "detail": {"message": "approve?"},
            },
            seq=1,
        ),
        _runtime_event(EventType.RUN_INTERRUPTED, {"status": "input_required"}, seq=2),
    ]
    first_agent = KsadkAGUIAgent(
        name="agent",
        adapter=first_adapter,
        event_store_factory=lambda: store,
    )
    _ = [event async for event in first_agent.run(_input())]

    restarted_adapter = _AttachableAdapter()
    restarted_adapter.streams["thread-1"] = [
        _runtime_event(EventType.TEXT_COMPLETED, {"text": "resumed"}, seq=3),
        _runtime_event(EventType.RUN_COMPLETED, {"status": "completed"}, seq=4),
    ]
    restarted_agent = KsadkAGUIAgent(
        name="agent",
        adapter=restarted_adapter,
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
    duplicate_agent = KsadkAGUIAgent(
        name="agent",
        adapter=_AttachableAdapter(),
        event_store_factory=lambda: RuntimeEventStore(service),
    )
    duplicate = [event async for event in duplicate_agent.run(resume_input)]

    assert resumed[-1].outcome.type == "success"
    assert len(restarted_adapter.attached) == 1
    assert len(restarted_adapter.resumed) == 1
    assert duplicate[-1].result == {"status": "already_resumed"}
    assert not duplicate_agent._shared.adapter.resumed


@pytest.mark.asyncio
async def test_agui_runtime_events_project_to_refreshable_history():
    events = [
        RuntimeEvent.create(
            EventType.RUN_STARTED,
            event_id="input-1",
            agent_id="agent",
            user_id="user-1",
            session_id="thread-1",
            invocation_id="run-1",
            seq_id=1,
            payload={"status": "in_progress", "input": "hello", "source": "ag-ui"},
        ),
        _runtime_event(
            EventType.APPROVAL_REQUESTED,
            {
                "approval_id": "interrupt-1",
                "call_id": "interrupt-1",
                "kind": "tool",
                "detail": {"tool_name": "shell", "arguments": {"cmd": "echo ok"}},
            },
            seq=2,
        ),
        _runtime_event(EventType.RUN_INTERRUPTED, {"status": "input_required"}, seq=3),
        RuntimeEvent.create(
            EventType.APPROVAL_RESOLVED,
            agent_id="agent",
            user_id="user-1",
            session_id="thread-1",
            invocation_id="run-2",
            seq_id=4,
            payload={
                "approval_id": "interrupt-1",
                "call_id": "interrupt-1",
                # AG-UI sends this payload shape when a UI approval is resumed.
                # History must remain readable for events already stored this way.
                "decision": {"decision": "approve"},
            },
        ),
        RuntimeEvent.create(
            EventType.TEXT_COMPLETED,
            agent_id="agent",
            user_id="user-1",
            session_id="thread-1",
            invocation_id="run-2",
            seq_id=5,
            phase="final_answer",
            payload={"text": "done"},
        ),
        RuntimeEvent.create(
            EventType.A2UI_SURFACE_BEGIN,
            agent_id="agent",
            user_id="user-1",
            session_id="thread-1",
            invocation_id="run-2",
            seq_id=6,
            payload={
                "surface_id": "surface-1",
                "operations": [
                    {
                        "version": "v0.9",
                        "createSurface": {
                            "surfaceId": "surface-1",
                            "catalogId": "catalog-1",
                        },
                    }
                ],
            },
        ),
    ]
    serialized = []
    for event in events:
        stored = runtime_event_to_session_event(event)
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

        async def append_one(self, event):
            self.events.append(event)
            return event

    store = _Store()
    adapter = _Adapter()
    adapter.streams["thread-1"] = [
        _runtime_event(
            EventType.APPROVAL_REQUESTED,
            {"approval_id": "interrupt-1", "call_id": "interrupt-1", "kind": "tool"},
            seq=1,
        ),
        _runtime_event(EventType.RUN_INTERRUPTED, {"status": "input_required"}, seq=2),
    ]
    agent = KsadkAGUIAgent(
        name="agent",
        adapter=adapter,
        event_store_factory=lambda: store,
    )
    _ = [event async for event in agent.run(_input())]
    requested = [
        event for event in store.events if event.event_type == EventType.APPROVAL_REQUESTED
    ]
    assert requested[0].payload["protocol"] == "ag-ui"
    adapter.streams["thread-1"] = [
        _runtime_event(EventType.RUN_COMPLETED, {"status": "completed"}, seq=3)
    ]

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

    resolved = [event for event in store.events if event.event_type == EventType.APPROVAL_RESOLVED]
    assert len(resolved) == 1
    assert resolved[0].payload | {"resume_fingerprint": "ignored"} == {
        "approval_id": "interrupt-1",
        "call_id": "interrupt-1",
        "decision": "approved",
        "resume_fingerprint": "ignored",
        "protocol": "ag-ui",
    }


@pytest.mark.asyncio
async def test_projects_standard_runtime_a2ui_events_as_official_agui_activities():
    class _Store:
        def __init__(self):
            self.events = []

        async def append_one(self, event):
            self.events.append(event)
            return event

    store = _Store()
    adapter = _Adapter()
    adapter.streams["thread-1"] = [
        _runtime_event(
            EventType.A2UI_SURFACE_BEGIN,
            {
                "surface_id": "surface-1",
                "catalog_id": "catalog-1",
                "surface": {
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
                },
            },
            seq=1,
        ),
        _runtime_event(
            EventType.A2UI_SURFACE_UPDATE,
            {
                "surface_id": "surface-1",
                "operations": [
                    {
                        "version": "v0.9",
                        "updateDataModel": {
                            "surfaceId": "surface-1",
                            "path": "/",
                            "value": {"ready": False},
                        },
                    }
                ],
            },
            seq=2,
        ),
        _runtime_event(EventType.A2UI_SURFACE_END, {"surface_id": "surface-1"}, seq=3),
        _runtime_event(EventType.RUN_COMPLETED, {"status": "completed"}, seq=4),
    ]
    agent = KsadkAGUIAgent(name="agent", adapter=adapter, event_store_factory=lambda: store)

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
    assert [event.event_type for event in store.events] == [
        EventType.RUN_STARTED,
        EventType.A2UI_SURFACE_BEGIN,
        EventType.A2UI_SURFACE_UPDATE,
        EventType.A2UI_SURFACE_END,
        EventType.RUN_COMPLETED,
    ]


def test_a2ui_projection_defaults_to_the_official_basic_catalog_id():
    operations = project_a2ui_operations(
        EventType.A2UI_SURFACE_BEGIN,
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
    adapter.streams["thread-1"] = [
        _runtime_event(EventType.RUN_COMPLETED, {"status": "completed"}, seq=1)
    ]
    agent = KsadkAGUIAgent(name="agent", adapter=adapter)
    clone = agent.clone()
    second = _input(run_id="run-2").model_copy(update={"thread_id": "thread-2"})
    adapter.streams["thread-2"] = [
        RuntimeEvent.create(
            EventType.RUN_COMPLETED,
            agent_id="agent",
            user_id="user",
            session_id="thread-2",
            invocation_id="run-2",
            seq_id=1,
            payload={"status": "completed"},
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
    agent = KsadkAGUIAgent(name="agent", adapter=adapter)
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
