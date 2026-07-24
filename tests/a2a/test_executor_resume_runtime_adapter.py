from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus
from google.protobuf.json_format import MessageToDict

from ksadk.a2a.executor import A2ARuntimeExecutor
from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter
from ksadk.events import EventPhase, EventType, RuntimeEvent
from ksadk.runtime import ResumePayload, ResumeTarget, RunHandle


class _FakeEventQueue:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)


class _ResumeContext:
    def __init__(self, answer: Any, *, metadata: dict[str, Any] | None = None) -> None:
        self.task_id = "task-1"
        self.context_id = "session-1"
        self.current_task: Task | None = Task(
            id=self.task_id,
            context_id=self.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
            metadata=metadata or _resume_metadata(),
        )
        self.metadata: dict[str, Any] = {}
        self.call_context = SimpleNamespace(tenant="trusted-tenant")
        self.message: Message | None = None
        self._answer = answer

    def get_user_input(self) -> Any:
        return self._answer


class _ForbiddenRunner:
    async def invoke(self, _input: dict[str, Any]) -> Any:
        raise AssertionError("resume must not call runner.invoke")

    async def stream(self, _input: dict[str, Any]) -> Any:
        raise AssertionError("resume must not call runner.stream")


class _RecordingRuntimeAdapter:
    def __init__(self) -> None:
        self.started_handle = RunHandle(
            run_id="task-1",
            session_id="session-1",
            runtime_type="test",
            native_ref={"runtime": "recording"},
        )
        self.start_calls: list[Any] = []
        self.attach_calls: list[RunHandle] = []
        self.resume_calls: list[tuple[RunHandle, ResumeTarget, ResumePayload | None]] = []
        self.stream_handles: list[RunHandle] = []

    async def start(self, request: Any) -> RunHandle:
        self.start_calls.append(request)
        return self.started_handle

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        self.resume_calls.append((handle, target, payload))
        return handle

    async def attach(self, handle: RunHandle) -> RunHandle:
        self.attach_calls.append(handle)
        return handle

    def stream(self, handle: RunHandle):  # noqa: ANN201
        self.stream_handles.append(handle)

        async def _events():
            if not self.resume_calls:
                yield RuntimeEvent.create(
                    EventType.APPROVAL_REQUESTED,
                    agent_id="agent-1",
                    user_id="user-1",
                    session_id=handle.session_id,
                    invocation_id=handle.run_id,
                    seq_id=1,
                    payload={
                        "approval_id": "call-1",
                        "call_id": "call-1",
                        "kind": "tool",
                        "detail": {"prompt": "approve tool call"},
                    },
                )
                yield RuntimeEvent.create(
                    EventType.CHECKPOINT_CREATED,
                    agent_id="agent-1",
                    user_id="user-1",
                    session_id=handle.session_id,
                    invocation_id=handle.run_id,
                    seq_id=2,
                    payload={
                        "checkpoint_id": "checkpoint-1",
                        "granularity": "snapshot",
                    },
                )
                return
            yield RuntimeEvent.create(
                EventType.TEXT_COMPLETED,
                agent_id="agent-1",
                user_id="user-1",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=1,
                phase=EventPhase.FINAL_ANSWER.value,
                payload={"text": "resumed"},
            )
            yield RuntimeEvent.create(
                EventType.RUN_COMPLETED,
                agent_id="agent-1",
                user_id="user-1",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=2,
                payload={"status": "completed"},
            )

        return _events()


def _resume_metadata(
    *,
    target_kind: str = "checkpoint_id",
    payload_kind: str = "approval_decision",
) -> dict[str, Any]:
    return {
        "run_handle": {
            "run_id": "run-1",
            "session_id": "session-1",
            "runtime_type": "test",
            "native_ref": {"checkpoint_id": "checkpoint-1"},
        },
        "resume_target": {"kind": target_kind, "id": "checkpoint-1"},
        "resume_payload": {"kind": payload_kind, "call_id": "call-1"},
    }


@pytest.mark.asyncio
async def test_input_required_status_metadata_roundtrips_to_runtime_resume() -> None:
    runtime_adapter = _RecordingRuntimeAdapter()
    task_adapter = A2ARuntimeTaskAdapter(runtime_adapter, runtime_type="test")  # type: ignore[arg-type]
    executor = A2ARuntimeExecutor(
        runner=_ForbiddenRunner(),
        task_adapter=task_adapter,
    )
    first_queue = _FakeEventQueue()
    first_context = _ResumeContext("start")
    first_context.current_task = None

    await executor.execute(first_context, first_queue)  # type: ignore[arg-type]

    status_event = next(
        event
        for event in first_queue.events
        if getattr(getattr(event, "status", None), "state", None)
        == TaskState.TASK_STATE_INPUT_REQUIRED
    )
    metadata = MessageToDict(status_event.metadata, preserving_proto_field_name=True)
    assert metadata["run_handle"]["run_id"] == runtime_adapter.started_handle.run_id
    assert metadata["resume_target"] == {
        "kind": "checkpoint_id",
        "id": "checkpoint-1",
    }
    assert metadata["resume_payload"]["kind"] == "approval_decision"

    resumed_queue = _FakeEventQueue()
    await executor.execute(_ResumeContext("approve", metadata=metadata), resumed_queue)  # type: ignore[arg-type]

    resume_handle = runtime_adapter.resume_calls[0][0]
    assert resume_handle == runtime_adapter.started_handle
    assert runtime_adapter.stream_handles == [runtime_adapter.started_handle, resume_handle]
    assert runtime_adapter.stream_handles[1] is resume_handle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected_decision"),
    [
        ("approve", {"type": "approve"}),
        (
            {"type": "edit", "args": {"city": "Beijing"}},
            {"type": "edit", "args": {"city": "Beijing"}},
        ),
        ("reject", {"type": "reject"}),
    ],
)
async def test_resume_approval_uses_runtime_adapter_and_streams_same_handle(
    answer: Any,
    expected_decision: dict[str, Any],
) -> None:
    runtime_adapter = _RecordingRuntimeAdapter()
    task_adapter = A2ARuntimeTaskAdapter(runtime_adapter, runtime_type="test")  # type: ignore[arg-type]
    executor = A2ARuntimeExecutor(
        runner=_ForbiddenRunner(),
        task_adapter=task_adapter,
    )
    queue = _FakeEventQueue()

    await executor.execute(_ResumeContext(answer), queue)  # type: ignore[arg-type]

    assert len(runtime_adapter.resume_calls) == 1
    resume_handle, target, payload = runtime_adapter.resume_calls[0]
    assert target == ResumeTarget(kind="checkpoint_id", id="checkpoint-1")
    assert payload == ResumePayload(
        kind="approval_decision",
        call_id="call-1",
        data={"decisions": [expected_decision]},
    )
    assert runtime_adapter.stream_handles == [resume_handle]
    assert runtime_adapter.stream_handles[0] is resume_handle
    assert runtime_adapter.attach_calls == [resume_handle]
    artifact_events = [event for event in queue.events if hasattr(event, "artifact")]
    assert artifact_events[-1].last_chunk is True
    assert any(
        getattr(getattr(event, "status", None), "state", None) == TaskState.TASK_STATE_COMPLETED
        for event in queue.events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [False, 0, "", None])
async def test_resume_payload_preserves_falsy_answers(answer: Any) -> None:
    runtime_adapter = _RecordingRuntimeAdapter()
    task_adapter = A2ARuntimeTaskAdapter(runtime_adapter, runtime_type="test")  # type: ignore[arg-type]
    context = _ResumeContext(
        "text fallback",
        metadata=_resume_metadata(payload_kind="hitl_answer"),
    )
    answer_part = Part()
    answer_part.data.struct_value.update({"value": answer})
    context.message = Message(
        message_id="message-1",
        role=Role.ROLE_USER,
        parts=[answer_part],
    )
    executor = A2ARuntimeExecutor(runner=_ForbiddenRunner(), task_adapter=task_adapter)

    await executor.execute(context, _FakeEventQueue())  # type: ignore[arg-type]

    payload = runtime_adapter.resume_calls[0][2]
    assert payload is not None
    assert payload.data == answer
    assert not payload.data


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["later", {"decisions": [{"type": "later"}]}])
async def test_unknown_approval_token_is_rejected_before_runtime_resume(answer: Any) -> None:
    runtime_adapter = _RecordingRuntimeAdapter()
    task_adapter = A2ARuntimeTaskAdapter(runtime_adapter, runtime_type="test")  # type: ignore[arg-type]
    context = _ResumeContext(answer)

    with pytest.raises(ValueError, match="unknown approval decision"):
        await task_adapter.resume_task(context.task_id, context, answer=answer)

    assert runtime_adapter.resume_calls == []


@pytest.mark.asyncio
async def test_invalid_resume_keeps_task_input_required_without_status_events() -> None:
    runtime_adapter = _RecordingRuntimeAdapter()
    task_adapter = A2ARuntimeTaskAdapter(runtime_adapter, runtime_type="test")  # type: ignore[arg-type]
    executor = A2ARuntimeExecutor(runner=_ForbiddenRunner(), task_adapter=task_adapter)
    context = _ResumeContext("later")
    queue = _FakeEventQueue()

    with pytest.raises(ValueError, match="unknown approval decision"):
        await executor.execute(context, queue)  # type: ignore[arg-type]

    assert context.current_task is not None
    assert context.current_task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert queue.events == []
    assert runtime_adapter.resume_calls == []


@pytest.mark.asyncio
async def test_unknown_resume_target_token_is_rejected() -> None:
    runtime_adapter = _RecordingRuntimeAdapter()
    task_adapter = A2ARuntimeTaskAdapter(runtime_adapter, runtime_type="test")  # type: ignore[arg-type]
    context = _ResumeContext(
        "approve",
        metadata=_resume_metadata(target_kind="unknown"),
    )

    with pytest.raises(ValueError, match="resume_target"):
        await task_adapter.resume_task(context.task_id, context, answer="approve")

    assert runtime_adapter.resume_calls == []


@pytest.mark.asyncio
async def test_runtime_error_detail_is_not_returned_on_a2a_wire() -> None:
    secret = "postgresql://user:secret@example.invalid/database"

    class _FailingStartRuntimeAdapter(_RecordingRuntimeAdapter):
        async def start(self, request: Any) -> RunHandle:
            raise RuntimeError(f"connection failed: {secret}")

    task_adapter = A2ARuntimeTaskAdapter(
        _FailingStartRuntimeAdapter(),  # type: ignore[arg-type]
        runtime_type="test",
    )
    executor = A2ARuntimeExecutor(runner=_ForbiddenRunner(), task_adapter=task_adapter)
    context = _ResumeContext("start")
    context.current_task = None
    queue = _FakeEventQueue()

    await executor.execute(context, queue)  # type: ignore[arg-type]

    wire_text = "".join(
        part.text
        for event in queue.events
        for part in getattr(getattr(getattr(event, "status", None), "message", None), "parts", ())
    )
    assert wire_text == "A2A task execution failed"
    assert secret not in wire_text


@pytest.mark.asyncio
async def test_start_uses_trusted_tenant_and_ignores_client_identity_metadata() -> None:
    runtime_adapter = _RecordingRuntimeAdapter()
    task_adapter = A2ARuntimeTaskAdapter(runtime_adapter, runtime_type="test")  # type: ignore[arg-type]
    executor = A2ARuntimeExecutor(runner=_ForbiddenRunner(), task_adapter=task_adapter)
    context = _ResumeContext("start")
    context.current_task = None
    context.metadata = {
        "user_id": "attacker-user",
        "agent_id": "attacker-agent",
        "trace_id": "trace-1",
    }

    await executor.execute(context, _FakeEventQueue())  # type: ignore[arg-type]

    request = runtime_adapter.start_calls[0]
    assert request.user_id == "trusted-tenant"
    assert request.agent_id is None
    assert request.metadata == {"trace_id": "trace-1", "invocation_id": "task-1"}
