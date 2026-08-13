from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.runners.base_runner import BaseRunner
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
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.tools.gateway import ToolGateway, ToolPolicy


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


class _GatewayApprovalRunner(BaseRunner):
    """Emit the same gateway approval result as a synchronous built-in tool."""

    def __init__(self, *, serialize_tool_result: bool = False) -> None:
        super().__init__(detection_result=None, project_dir=".")
        self.calls: list[dict] = []
        self.serialize_tool_result = serialize_tool_result

    def load_agent(self) -> None:
        return None

    async def invoke(self, _input_data: dict) -> dict:
        return {"output": "unused"}

    async def stream(self, input_data: dict) -> AsyncIterator[dict]:
        self.calls.append(input_data)
        if (
            isinstance(input_data.get("input"), dict)
            and input_data["input"].get("type") == "function_call_output"
        ):
            yield {"type": "final", "output": "file written"}
            return
        yield {
            "type": "tool_call",
            "call_id": "call-write",
            "tool_name": "write_workspace_file",
            "tool_args": {"path": "hello.md", "content": "hello"},
        }
        approval_result = {
            "ok": False,
            "type": "approval_required",
            "approval_request": {
                "id": "appr-write",
                "tool_name": "write_workspace_file",
                "risk_level": "medium",
                "side_effects": ["workspace_write"],
            },
        }
        yield {
            "type": "tool_result",
            "call_id": "call-write",
            "tool_name": "write_workspace_file",
            "tool_args": {"path": "hello.md", "content": "hello"},
            "tool_output": (
                json.dumps(approval_result) if self.serialize_tool_result else approval_result
            ),
        }
        yield {"type": "final", "output": "must not be streamed before approval"}


class _GatewayPolicyRunner(BaseRunner):
    """Exercise the tool gateway under the RuntimeAdapter request context."""

    def __init__(self) -> None:
        super().__init__(detection_result=None, project_dir=".")

    def load_agent(self) -> None:
        return None

    async def invoke(self, _input_data: dict) -> dict:
        return {"output": "unused"}

    async def stream(self, _input_data: dict) -> AsyncIterator[dict]:
        result = ToolGateway({"write_workspace_file": ToolPolicy(risk_level="medium")}).invoke(
            "write_workspace_file", lambda: {"ok": True}
        )
        yield {
            "type": "tool_call",
            "call_id": "call-write",
            "tool_name": "write_workspace_file",
            "tool_args": {"path": "hello.md", "content": "hello"},
        }
        yield {
            "type": "tool_result",
            "call_id": "call-write",
            "tool_name": "write_workspace_file",
            "tool_args": {"path": "hello.md", "content": "hello"},
            "tool_output": result,
        }
        if result.get("ok"):
            yield {"type": "final", "output": "file written"}


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


@pytest.mark.asyncio
async def test_runtime_gateway_approval_result_pauses_responses_stream_with_approval_item():
    """Risk-mode built-in writes must become a resumable approval, not a text reply."""

    from ksadk.conversations.runtime_streaming import (
        stream_runtime_responses_conversation_turn,
    )

    service = InMemorySessionService()
    registry = RuntimeRegistry()
    registry.register(
        "fixture",
        lambda _context: RunnerRuntimeAdapter(_GatewayApprovalRunner(), runtime_type="fixture"),
    )
    chunks = [
        chunk
        async for chunk in stream_runtime_responses_conversation_turn(
            executor=RuntimeExecutor(registry),
            launch_context=RuntimeLaunchContext(runtime_type="fixture", project_dir="."),
            agent_id="agent-1",
            user_id="user-1",
            messages=[{"role": "user", "content": "write hello.md"}],
            session_id=None,
            model="fixture-model",
            session_service_provider=lambda: service,
        )
    ]

    events = _decode_sse(chunks)
    approval_item = next(
        data["item"]
        for name, data in events
        if name == "response.output_item.done"
        and data.get("item", {}).get("type") == "mcp_approval_request"
    )

    assert approval_item["id"] == "appr-write"
    assert approval_item["name"] == "write_workspace_file"
    assert "response.incomplete" in [name for name, _data in events]
    assert "response.completed" not in [name for name, _data in events]


@pytest.mark.asyncio
async def test_runtime_serialized_gateway_approval_result_pauses_responses_stream():
    """LangGraph ToolMessage.content serializes gateway results before the adapter sees them."""

    from ksadk.conversations.runtime_streaming import (
        stream_runtime_responses_conversation_turn,
    )

    service = InMemorySessionService()
    registry = RuntimeRegistry()
    registry.register(
        "fixture",
        lambda _context: RunnerRuntimeAdapter(
            _GatewayApprovalRunner(serialize_tool_result=True), runtime_type="fixture"
        ),
    )
    chunks = [
        chunk
        async for chunk in stream_runtime_responses_conversation_turn(
            executor=RuntimeExecutor(registry),
            launch_context=RuntimeLaunchContext(runtime_type="fixture", project_dir="."),
            agent_id="agent-1",
            user_id="user-1",
            messages=[{"role": "user", "content": "write hello.md"}],
            session_id=None,
            model="fixture-model",
            session_service_provider=lambda: service,
        )
    ]

    events = _decode_sse(chunks)
    approval_item = next(
        data["item"]
        for name, data in events
        if name == "response.output_item.done"
        and data.get("item", {}).get("type") == "mcp_approval_request"
    )

    assert approval_item["id"] == "appr-write"
    assert "response.incomplete" in [name for name, _data in events]
    assert "response.completed" not in [name for name, _data in events]


@pytest.mark.asyncio
async def test_runtime_full_approval_mode_runs_medium_risk_gateway_tool_without_pause(monkeypatch):
    """The session-level full profile must override the process default for built-in tools."""

    from ksadk.conversations.runtime_streaming import (
        stream_runtime_responses_conversation_turn,
    )

    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", "risk")
    service = InMemorySessionService()
    registry = RuntimeRegistry()
    registry.register(
        "fixture",
        lambda _context: RunnerRuntimeAdapter(_GatewayPolicyRunner(), runtime_type="fixture"),
    )
    chunks = [
        chunk
        async for chunk in stream_runtime_responses_conversation_turn(
            executor=RuntimeExecutor(registry),
            launch_context=RuntimeLaunchContext(runtime_type="fixture", project_dir="."),
            agent_id="agent-1",
            user_id="user-1",
            messages=[{"role": "user", "content": "write hello.md"}],
            session_id=None,
            model="fixture-model",
            request_metadata={"tool_approval_mode": "full"},
            session_service_provider=lambda: service,
        )
    ]

    events = _decode_sse(chunks)

    assert "response.incomplete" not in [name for name, _data in events]
    assert "response.completed" in [name for name, _data in events]
    tool_result = next(data for name, data in events if name == "response.ksadk.tool_result")
    assert tool_result["output"] == {"ok": True}


@pytest.mark.asyncio
async def test_runtime_gateway_approval_resume_writes_file_with_original_call_id(
    monkeypatch, tmp_path
):
    """A gateway approval must persist enough context to execute its approved write."""

    from ksadk.conversations.runtime_streaming import (
        stream_runtime_responses_conversation_turn,
    )

    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))
    service = InMemorySessionService()
    runner = _GatewayApprovalRunner()
    registry = RuntimeRegistry()
    registry.register(
        "fixture",
        lambda _context: RunnerRuntimeAdapter(runner, runtime_type="fixture"),
    )
    executor = RuntimeExecutor(registry)
    launch_context = RuntimeLaunchContext(runtime_type="fixture", project_dir=".")
    first_chunks = [
        chunk
        async for chunk in stream_runtime_responses_conversation_turn(
            executor=executor,
            launch_context=launch_context,
            agent_id="agent-1",
            user_id="user-1",
            messages=[{"role": "user", "content": "write hello.md"}],
            session_id=None,
            model="fixture-model",
            session_service_provider=lambda: service,
        )
    ]
    first_events = _decode_sse(first_chunks)
    incomplete = next(data for name, data in first_events if name == "response.incomplete")
    session_id = incomplete["session_id"]

    persisted = await service.get_events(session_id)
    approval_event = next(
        event for event in persisted if event.event_type == EventType.APPROVAL_REQUESTED
    )
    assert approval_event.content["payload"]["detail"]["run_id"] == "call-write"

    resumed_chunks = [
        chunk
        async for chunk in stream_runtime_responses_conversation_turn(
            executor=executor,
            launch_context=launch_context,
            agent_id="agent-1",
            user_id="user-1",
            messages=[],
            session_id=session_id,
            model="fixture-model",
            resume_input={
                "type": "mcp_approval_response",
                "approval_request_id": "appr-write",
                "approve": True,
            },
            session_service_provider=lambda: service,
        )
    ]

    assert (tmp_path / "ui" / "workspace" / "hello.md").read_text(encoding="utf-8") == "hello"
    assert runner.calls[-1]["input"]["type"] == "function_call_output"
    assert runner.calls[-1]["input"]["call_id"] == "call-write"
    assert "response.completed" in [name for name, _data in _decode_sse(resumed_chunks)]


@pytest.mark.asyncio
async def test_langgraph_gateway_approval_resume_emits_a_follow_up_answer(monkeypatch, tmp_path):
    """A gateway approval is not a native graph interrupt, so it must restart a turn.

    A tool can return the ToolGateway ``approval_required`` payload after the
    LangGraph graph has already finished.  Approving it executes the built-in
    tool in KsADK, but ``Command(resume=...)`` against that terminal graph is a
    no-op.  The resumed request must therefore be a fresh semantic turn, so a
    user-visible final answer follows the real tool result.
    """

    from langgraph.types import Command

    from ksadk.conversations.runtime_streaming import (
        stream_runtime_responses_conversation_turn,
    )
    from ksadk.runners.langgraph_runner import LangGraphRunner

    class _Chunk:
        def __init__(self, content: str) -> None:
            self.content = content
            self.additional_kwargs: dict[str, object] = {}

    class _GatewayApprovalGraph:
        def __init__(self) -> None:
            self.inputs: list[object] = []

        def get_state(self, _config):
            return SimpleNamespace(tasks=[], values={})

        async def astream_events(self, state, version="v2", config=None, stream_mode=None):
            del version, config, stream_mode
            self.inputs.append(state)
            if len(self.inputs) == 1:
                yield {
                    "event": "on_tool_start",
                    "name": "write_workspace_file",
                    "run_id": "call-write",
                    "data": {"input": {"path": "hello.md", "content": "hello"}},
                }
                yield {
                    "event": "on_tool_end",
                    "name": "write_workspace_file",
                    "run_id": "call-write",
                    "data": {
                        "input": {"path": "hello.md", "content": "hello"},
                        "output": json.dumps(
                            {
                                "ok": False,
                                "type": "approval_required",
                                "approval_request": {
                                    "id": "appr-write",
                                    "tool_name": "write_workspace_file",
                                    "risk_level": "medium",
                                    "side_effects": ["workspace_write"],
                                },
                            }
                        ),
                    },
                }
                # This is the stale prose the first turn produces before the
                # UI shows the approval card.  The Responses serializer stops
                # it at the approval boundary.
                yield {
                    "event": "on_chat_model_stream",
                    "data": {"chunk": _Chunk("请批准后再写入。")},
                }
                return
            if isinstance(state, Command):
                # The pre-fix behavior: a completed graph receives Command
                # resume and has no path back to its model node.
                yield {
                    "event": "on_tool_start",
                    "name": "resume_noop",
                    "run_id": "resume-noop",
                    "data": {"input": {}},
                }
                return
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": _Chunk("已批准，hello.md 已写入工作区。")},
            }

    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))
    graph = _GatewayApprovalGraph()
    runner = LangGraphRunner(
        SimpleNamespace(entry_point="agent.py", agent_variable="root_agent"), "."
    )
    runner._agent = graph
    service = InMemorySessionService()
    registry = RuntimeRegistry()
    registry.register(
        "langgraph",
        lambda _context: RunnerRuntimeAdapter(runner, runtime_type="langgraph"),
    )
    executor = RuntimeExecutor(registry)
    launch_context = RuntimeLaunchContext(runtime_type="langgraph", project_dir=".")

    first_events = _decode_sse(
        [
            chunk
            async for chunk in stream_runtime_responses_conversation_turn(
                executor=executor,
                launch_context=launch_context,
                agent_id="agent-1",
                user_id="user-1",
                messages=[{"role": "user", "content": "write hello.md"}],
                session_id=None,
                model=None,
                session_service_provider=lambda: service,
            )
        ]
    )
    session_id = next(data for name, data in first_events if name == "response.incomplete")[
        "session_id"
    ]

    resumed_events = _decode_sse(
        [
            chunk
            async for chunk in stream_runtime_responses_conversation_turn(
                executor=executor,
                launch_context=launch_context,
                agent_id="agent-1",
                user_id="user-1",
                messages=[],
                session_id=session_id,
                model=None,
                resume_input={
                    "type": "mcp_approval_response",
                    "approval_request_id": "appr-write",
                    "approve": True,
                },
                session_service_provider=lambda: service,
            )
        ]
    )

    assert (tmp_path / "ui" / "workspace" / "hello.md").read_text(encoding="utf-8") == "hello"
    assert not isinstance(graph.inputs[-1], Command)
    completed = next(data for name, data in resumed_events if name == "response.completed")
    assert completed["output_text"] == "已批准，hello.md 已写入工作区。"
