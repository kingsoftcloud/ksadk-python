from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    StreamResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
)
from fastapi import FastAPI

from ksadk.a2a import A2ARuntimeTaskAdapter, add_a2a_protocol_routes
from ksadk.a2a.executor import A2ARuntimeExecutor
from ksadk.a2a.langgraph import (
    _extract_artifact_events,
    stream_a2a_agent,
    stream_a2a_agent_to_writer,
)
from ksadk.a2a.routes import A2AConfig
from ksadk.cli.cmd_a2a import serve
from ksadk.conversations.runtime_streaming import stream_responses_conversation_turn
from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    ItemUpdated,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import TextContent
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime import RunHandle
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter
from ksadk.sessions.in_memory import InMemorySessionService


class _RecordingUpdater:
    def __init__(self) -> None:
        self.artifacts: list[dict[str, Any]] = []

    async def add_artifact(self, **kwargs: Any) -> None:
        self.artifacts.append(kwargs)


class _RuntimeTaskAdapter:
    async def stream_task(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        yield ItemUpdated(
            schema_version=2,
            event_id="evt-reasoning-1",
            seq=1,
            timestamp=1.0,
            run_id=handle.run_id,
            scope_id="scope-1",
            source=SourceRef(framework="ksadk"),
            item_id="reasoning-1",
            item_kind="reasoning",
            op="append",
            update=TextContent(part_id="text-0", text="先分析。"),
        )
        yield ItemCompleted(
            schema_version=2,
            event_id="evt-text-1",
            seq=2,
            timestamp=2.0,
            run_id=handle.run_id,
            scope_id="scope-1",
            source=SourceRef(framework="ksadk"),
            item_id="msg-1",
            item_kind="message",
            snapshot=ContentSnapshot(
                parts=(TextContent(part_id="text-0", text="最终答案。"),)
            ),
        )

    def was_cancel_accepted(self, *_args: Any) -> bool:
        return False


class _AlternatingRunner:
    async def invoke(self, _runner_input: dict[str, Any]) -> dict[str, str]:
        return {"output": "第一段。第二段。"}

    async def stream(self, _runner_input: dict[str, Any]) -> AsyncIterator[dict[str, str]]:
        yield {"type": "thinking", "delta": "先分析。"}
        yield {"type": "text", "delta": "第一段。"}
        yield {"type": "thinking", "delta": "再检查。"}
        yield {"type": "text", "delta": "第二段。"}
        yield {"type": "final", "output": "第一段。第二段。"}


class _ReplacingCustomStreamGraph:
    def __init__(self) -> None:
        self.stream_mode: Any = None

    async def astream_events(
        self,
        _state: Any,
        *,
        version: str = "v2",
        config: Any = None,
        stream_mode: Any = None,
    ) -> AsyncIterator[dict[str, Any]]:
        del version, config
        self.stream_mode = stream_mode
        yield {
            "event": "on_chain_stream",
            "data": {
                "chunk": (
                    "custom",
                    {"type": "text", "delta": "旧答", "replace": False},
                )
            },
        }
        yield {
            "event": "on_chain_stream",
            "data": {
                "chunk": (
                    "custom",
                    {"type": "text", "delta": "新答", "replace": True},
                )
            },
        }
        yield {
            "event": "on_chain_end",
            "data": {"output": {"messages": [SimpleNamespace(content="新答")]}},
        }

    def get_state(self, _config: Any) -> Any:
        return SimpleNamespace(values={}, next=())


def _replacing_langgraph_runner() -> LangGraphRunner:
    detection = SimpleNamespace(entry_point="agent.py", agent_variable="root_agent")
    runner = LangGraphRunner(detection, ".")
    runner._agent = _ReplacingCustomStreamGraph()  # noqa: SLF001
    return runner


def _sse_payloads(chunks: list[str], event_name: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    current_event = ""
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("event: "):
                current_event = line.removeprefix("event: ")
            elif line.startswith("data: ") and current_event == event_name:
                payloads.append(json.loads(line.removeprefix("data: ")))
    return payloads


def _artifact_event(
    *,
    artifact_id: str,
    name: str,
    text: str,
    append: bool,
    thought: bool = False,
    output_snapshot: bool = False,
) -> StreamResponse:
    metadata = {}
    if thought:
        metadata["adk_thought"] = True
    if output_snapshot:
        metadata["ksadk_output_snapshot"] = True
    return StreamResponse(
        artifact_update=TaskArtifactUpdateEvent(
            task_id="task-1",
            context_id="context-1",
            artifact=Artifact(
                artifact_id=artifact_id,
                name=name,
                parts=[Part(text=text, metadata=metadata or None)],
            ),
            append=append,
        )
    )


def test_reasoning_exposure_defaults_safe_for_managed_and_on_for_local_cli() -> None:
    assert A2AConfig().include_reasoning is False
    option = next(param for param in serve.params if param.name == "include_reasoning")
    assert option.default is True


@pytest.mark.asyncio
async def test_runtime_stream_preserves_reasoning_without_mixing_it_into_answer() -> None:
    executor = A2ARuntimeExecutor(
        task_adapter=_RuntimeTaskAdapter(),
        include_reasoning=True,
    )
    updater = _RecordingUpdater()
    handle = RunHandle(
        run_id="run-1",
        session_id="session-1",
        runtime_type="test",
        native_ref={},
    )

    output = await executor._run_runtime(  # noqa: SLF001
        SimpleNamespace(task_id="task-1"),
        updater,  # type: ignore[arg-type]
        handle,
    )

    assert output == "最终答案。"
    assert [(item["name"], item["parts"][0].text) for item in updater.artifacts] == [
        ("reasoning", "先分析。"),
        ("response", "最终答案。"),
    ]
    assert updater.artifacts[0]["parts"][0].metadata["adk_thought"] is True
    assert all(item["last_chunk"] is True for item in updater.artifacts)


@pytest.mark.asyncio
async def test_runtime_completed_text_replacement_is_authoritative_snapshot() -> None:
    class _ReplacingRuntimeTaskAdapter(_RuntimeTaskAdapter):
        async def stream_task(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
            # v1 TEXT_DELTA ("旧答") → canonical ItemUpdated op="append";
            # v1 TEXT_COMPLETED ("新答") → canonical ItemCompleted (authoritative snapshot).
            yield ItemUpdated(
                schema_version=2,
                event_id="evt-text-delta-1",
                seq=1,
                timestamp=1.0,
                run_id=handle.run_id,
                scope_id="scope-1",
                source=SourceRef(framework="ksadk"),
                item_id="msg-1",
                item_kind="message",
                op="append",
                update=TextContent(part_id="text-0", text="旧答"),
            )
            yield ItemCompleted(
                schema_version=2,
                event_id="evt-text-completed-1",
                seq=2,
                timestamp=2.0,
                run_id=handle.run_id,
                scope_id="scope-1",
                source=SourceRef(framework="ksadk"),
                item_id="msg-1",
                item_kind="message",
                snapshot=ContentSnapshot(
                    parts=(TextContent(part_id="text-0", text="新答"),)
                ),
            )

    executor = A2ARuntimeExecutor(task_adapter=_ReplacingRuntimeTaskAdapter())
    updater = _RecordingUpdater()
    handle = RunHandle(run_id="run-1", session_id="session-1", runtime_type="test")

    output = await executor._run_runtime(  # noqa: SLF001
        SimpleNamespace(task_id="task-1"),
        updater,  # type: ignore[arg-type]
        handle,
    )

    assert output == "新答"
    assert [item["parts"][0].text for item in updater.artifacts] == ["旧答", "新答"]
    assert updater.artifacts[-1]["append"] is False
    assert updater.artifacts[-1]["parts"][0].metadata["ksadk_output_snapshot"] is True


def test_artifact_extraction_keeps_reasoning_and_text_types() -> None:
    snapshots: dict[tuple[str, str], str] = {}
    wire_events = [
        _artifact_event(
            artifact_id="reasoning-1",
            name="reasoning",
            text="先分析。",
            append=False,
            thought=True,
        ),
        _artifact_event(
            artifact_id="response-1",
            name="response",
            text="第一段。",
            append=False,
        ),
        _artifact_event(
            artifact_id="reasoning-1",
            name="reasoning",
            text="再检查。",
            append=True,
            thought=True,
        ),
        _artifact_event(
            artifact_id="response-1",
            name="response",
            text="第二段。",
            append=True,
        ),
    ]

    extracted = [
        event
        for wire_event in wire_events
        for event in _extract_artifact_events(wire_event, snapshots)
    ]

    assert extracted == [
        {"type": "thinking", "delta": "先分析。", "replace": False},
        {"type": "text", "delta": "第一段。", "replace": False},
        {"type": "thinking", "delta": "再检查。", "replace": False},
        {"type": "text", "delta": "第二段。", "replace": False},
    ]


def test_task_and_message_fallbacks_are_extracted_as_text() -> None:
    task_event = StreamResponse(
        task=Task(
            id="task-1",
            context_id="context-1",
            status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            artifacts=[
                Artifact(
                    artifact_id="response-1",
                    name="response",
                    parts=[Part(text="任务结果")],
                )
            ],
        )
    )
    message_event = StreamResponse(
        message=Message(
            role=Role.ROLE_AGENT,
            parts=[Part(text="直接结果")],
            message_id="message-1",
        )
    )

    assert _extract_artifact_events(task_event, {}) == [
        {"type": "text", "delta": "任务结果", "replace": False}
    ]
    assert _extract_artifact_events(message_event, {}) == [
        {"type": "text", "delta": "直接结果", "replace": False}
    ]


def test_artifact_extraction_marks_non_prefix_snapshot_as_replacement() -> None:
    snapshots: dict[tuple[str, str], str] = {}
    first = _artifact_event(
        artifact_id="response-1",
        name="response",
        text="旧答",
        append=False,
    )
    replacement = _artifact_event(
        artifact_id="response-1",
        name="response",
        text="新答",
        append=False,
        output_snapshot=True,
    )

    assert _extract_artifact_events(first, snapshots) == [
        {"type": "text", "delta": "旧答", "replace": False}
    ]
    assert _extract_artifact_events(replacement, snapshots) == [
        {"type": "text", "delta": "新答", "replace": True}
    ]


@pytest.mark.asyncio
async def test_a2a_server_to_langgraph_writer_round_trip(tmp_path: Any) -> None:
    runner = _AlternatingRunner()
    app = FastAPI()
    add_a2a_protocol_routes(
        app,
        A2AConfig(
            enabled=True,
            base_url="http://testserver",
            agent_name="alternating-agent",
            task_store_dsn=f"sqlite+aiosqlite:///{tmp_path}/tasks.db",
            include_reasoning=True,
        ),
        task_adapter=A2ARuntimeTaskAdapter(
            RunnerRuntimeAdapter(runner, runtime_type="ksadk"),
            runtime_type="ksadk",
        ),
    )
    written: list[dict[str, Any]] = []
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        output = await stream_a2a_agent_to_writer(
            "http://testserver",
            "问题",
            writer=written.append,
            httpx_client=client,
        )

    assert output == "第一段。第二段。"
    # canonical switch: "final" chunk creates an additional text event with
    # replace=True carrying the authoritative final output.
    assert [(event["type"], event["delta"]) for event in written] == [
        ("thinking", "先分析。"),
        ("text", "第一段。"),
        ("thinking", "再检查。"),
        ("text", "第二段。"),
        ("text", "第一段。第二段。"),
    ]


@pytest.mark.xfail(reason="Test fixture uses v2 astream_events API (on_chain_stream/on_chain_end) but stream_canonical_events now uses v3 ProtocolEvent API; fixture needs rewrite to v3 format")
@pytest.mark.asyncio
async def test_a2a_server_round_trip_preserves_text_replacement(tmp_path: Any) -> None:
    runner = _replacing_langgraph_runner()
    app = FastAPI()
    add_a2a_protocol_routes(
        app,
        A2AConfig(
            enabled=True,
            base_url="http://testserver",
            agent_name="replacing-agent",
            task_store_dsn=f"sqlite+aiosqlite:///{tmp_path}/tasks.db",
        ),
        task_adapter=A2ARuntimeTaskAdapter(
            RunnerRuntimeAdapter(runner, runtime_type="ksadk"),
            runtime_type="ksadk",
        ),
    )
    written: list[dict[str, Any]] = []
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        output = await stream_a2a_agent_to_writer(
            "http://testserver",
            "问题",
            writer=written.append,
            httpx_client=client,
        )

    assert output == "新答"
    assert written == [
        {"type": "text", "delta": "旧答", "replace": False},
        {"type": "text", "delta": "新答", "replace": True},
    ]


@pytest.mark.asyncio
async def test_writer_bridge_forwards_typed_events_and_returns_only_final_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_events(*_args: Any, **_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "thinking", "delta": "先分析。", "replace": False}
        yield {"type": "text", "delta": "答案一。", "replace": False}
        yield {"type": "thinking", "delta": "再检查。", "replace": False}
        yield {"type": "text", "delta": "答案二。", "replace": False}

    monkeypatch.setattr("ksadk.a2a.langgraph.stream_a2a_agent_events", fake_events)
    written: list[dict[str, Any]] = []

    output = await stream_a2a_agent_to_writer(
        "http://127.0.0.1:8094",
        "问题",
        writer=written.append,
    )

    assert output == "答案一。答案二。"
    assert written == [
        {"type": "thinking", "delta": "先分析。", "replace": False},
        {"type": "text", "delta": "答案一。", "replace": False},
        {"type": "thinking", "delta": "再检查。", "replace": False},
        {"type": "text", "delta": "答案二。", "replace": False},
    ]


@pytest.mark.asyncio
async def test_writer_bridge_reconciles_authoritative_replacement_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_events(*_args: Any, **_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "text", "delta": "旧答", "replace": False}
        yield {"type": "text", "delta": "新答", "replace": True}

    monkeypatch.setattr("ksadk.a2a.langgraph.stream_a2a_agent_events", fake_events)
    written: list[dict[str, Any]] = []

    output = await stream_a2a_agent_to_writer(
        "http://127.0.0.1:8094",
        "问题",
        writer=written.append,
    )

    assert output == "新答"
    assert written[-1] == {"type": "text", "delta": "新答", "replace": True}


@pytest.mark.asyncio
async def test_langgraph_runner_preserves_custom_stream_replacement() -> None:
    runner = _replacing_langgraph_runner()

    chunks = [
        chunk
        async for chunk in runner.stream(
            {"session_id": "session-1", "input": "问题"},
        )
    ]

    assert chunks[:2] == [
        {"type": "text", "delta": "旧答"},
        {"type": "text", "delta": "新答", "replace": True},
    ]
    assert next(chunk for chunk in chunks if chunk["type"] == "final")["output"] == "新答"
    assert runner._agent.stream_mode == ["values", "custom"]  # noqa: SLF001


@pytest.mark.asyncio
async def test_adk_runner_preserves_final_thought_chunk_and_replacement(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # google-adk 1.x 的 A2A artifact→event 转换器不分离 adk_thought 为独立
    # thinking chunk(该行为是 2.x 语义),此测试断言的是 2.x 输出形态。
    from importlib.metadata import version as _pkg_version

    if int(_pkg_version("google-adk").split(".")[0]) < 2:
        pytest.skip("adk_thought 分离为 thinking chunk 依赖 google-adk >= 2.x")

    from google.adk.a2a.converters.to_adk_event import (
        convert_a2a_artifact_update_to_event,
    )
    from google.genai import types

    from ksadk.runners.adk_runner import ADKRunner

    def converted_event(
        *,
        artifact_id: str,
        name: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        return convert_a2a_artifact_update_to_event(
            TaskArtifactUpdateEvent(
                task_id="task-1",
                context_id="context-1",
                artifact=Artifact(
                    artifact_id=artifact_id,
                    name=name,
                    parts=[Part(text=text, metadata=metadata)],
                ),
                last_chunk=True,
            ),
            author="remote-agent",
        )

    remote_events = [
        converted_event(
            artifact_id="reasoning-1",
            name="reasoning",
            text="最后一段思考",
            metadata={"adk_thought": True},
        ),
        converted_event(
            artifact_id="response-1",
            name="response",
            text="旧答",
        ),
        converted_event(
            artifact_id="response-2",
            name="response",
            text="新答",
            metadata={"ksadk_output_snapshot": True},
        ),
        SimpleNamespace(
            author="remote-agent",
            partial=False,
            content=types.Content(role="model", parts=[types.Part(text="新答")]),
        ),
    ]

    class _FakeADKRunner:
        async def run_async(self, **_kwargs: Any) -> AsyncIterator[Any]:
            for event in remote_events:
                yield event

    detection = SimpleNamespace(
        entry_point="agent.py",
        agent_variable="root_agent",
        name="orchestrator",
    )
    runner = ADKRunner(detection, str(tmp_path))
    runner._agent = SimpleNamespace(name="orchestrator")  # noqa: SLF001
    runner._runner = _FakeADKRunner()  # noqa: SLF001

    async def fake_ensure_session(_external_session_id: Any = None) -> str:
        return "adk-session"

    monkeypatch.setattr(runner, "_ensure_session", fake_ensure_session)
    monkeypatch.setattr(
        runner,
        "_prepare_trace_metadata",
        lambda _session_id: ("", [], "", "orchestrator"),
    )

    chunks = [
        chunk
        async for chunk in runner.stream(
            {"session_id": "session-1", "input": "问题"},
        )
    ]

    assert chunks[:3] == [
        {"type": "thinking", "delta": "最后一段思考"},
        {"type": "text", "delta": "旧答"},
        {"type": "text", "delta": "新答", "replace": True},
    ]
    assert chunks[-1] == {"type": "final", "output": "新答"}


@pytest.mark.asyncio
async def test_adk_runner_does_not_repeat_terminal_thought_snapshot(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.genai import types

    from ksadk.runners.adk_runner import ADKRunner

    remote_events = [
        SimpleNamespace(
            author="remote-agent",
            partial=True,
            content=types.Content(
                role="model",
                parts=[types.Part(text="阶段一思考", thought=True)],
            ),
        ),
        SimpleNamespace(
            author="remote-agent",
            partial=True,
            content=types.Content(role="model", parts=[types.Part(text="阶段一正文")]),
        ),
        SimpleNamespace(
            author="remote-agent",
            partial=True,
            content=types.Content(
                role="model",
                parts=[types.Part(text="阶段二思考", thought=True)],
            ),
        ),
        SimpleNamespace(
            author="remote-agent",
            partial=True,
            content=types.Content(role="model", parts=[types.Part(text="阶段二正文")]),
        ),
        SimpleNamespace(
            author="remote-agent",
            partial=False,
            content=types.Content(
                role="model",
                # ADK may emit a replacement terminal snapshot after the final
                # response; it is not a new interleaved reasoning segment.
                parts=[types.Part(text="阶段二思考的终态快照", thought=True)],
            ),
        ),
    ]

    class _FakeADKRunner:
        async def run_async(self, **_kwargs: Any) -> AsyncIterator[Any]:
            for event in remote_events:
                yield event

    detection = SimpleNamespace(
        entry_point="agent.py",
        agent_variable="root_agent",
        name="orchestrator",
    )
    runner = ADKRunner(detection, str(tmp_path))
    runner._agent = SimpleNamespace(name="orchestrator")  # noqa: SLF001
    runner._runner = _FakeADKRunner()  # noqa: SLF001

    async def fake_ensure_session(_external_session_id: Any = None) -> str:
        return "adk-session"

    monkeypatch.setattr(runner, "_ensure_session", fake_ensure_session)
    monkeypatch.setattr(
        runner,
        "_prepare_trace_metadata",
        lambda _session_id: ("", [], "", "orchestrator"),
    )

    chunks = [
        chunk
        async for chunk in runner.stream(
            {"session_id": "session-1", "input": "问题"},
        )
    ]

    assert chunks == [
        {"type": "thinking", "delta": "阶段一思考"},
        {"type": "text", "delta": "阶段一正文"},
        {"type": "thinking", "delta": "阶段二思考"},
        {"type": "text", "delta": "阶段二正文"},
        {"type": "final", "output": "阶段一正文阶段二正文"},
    ]


@pytest.mark.asyncio
async def test_responses_stream_preserves_replacement_and_authoritative_final_text() -> None:
    runner = _replacing_langgraph_runner()
    service = InMemorySessionService()

    chunks = [
        chunk
        async for chunk in stream_responses_conversation_turn(
            runner=runner,
            agent_id="orchestrator",
            user_id="user-1",
            session_id=None,
            messages=[{"role": "user", "content": "问题"}],
            model=None,
            prepare_runner=lambda _runner, _model: None,
            session_service_provider=lambda: service,
        )
    ]

    deltas = _sse_payloads(chunks, "response.output_text.delta")
    assert [payload["delta"] for payload in deltas] == ["旧答", "新答"]
    assert "replace" not in deltas[0]
    assert deltas[1]["replace"] is True
    assert _sse_payloads(chunks, "response.output_text.done")[-1]["text"] == "新答"
    assert _sse_payloads(chunks, "response.completed")[-1]["output_text"] == "新答"


@pytest.mark.asyncio
async def test_legacy_text_helper_remains_backward_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_events(*_args: Any, **_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "thinking", "delta": "不进入正文", "replace": False}
        yield {"type": "text", "delta": "正文", "replace": False}

    monkeypatch.setattr("ksadk.a2a.langgraph.stream_a2a_agent_events", fake_events)

    chunks = [chunk async for chunk in stream_a2a_agent("http://127.0.0.1:8094", "问题")]

    assert chunks == ["正文"]
