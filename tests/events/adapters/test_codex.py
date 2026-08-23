"""Codex app-server 0.147.0 JSONL to canonical RuntimeEvent tests."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pytest
from openai_codex.generated.notification_registry import NOTIFICATION_MODELS
from openai_codex.generated.v2_all import (
    ErrorServerNotification,
    ItemAgentMessageDeltaServerNotification,
    ItemCommandExecutionOutputDeltaServerNotification,
    ItemCompletedServerNotification,
    ItemFileChangeOutputDeltaServerNotification,
    ItemFileChangePatchUpdatedServerNotification,
    ItemMcpToolCallProgressServerNotification,
    ItemPlanDeltaServerNotification,
    ItemReasoningSummaryPartAddedServerNotification,
    ItemReasoningSummaryTextDeltaServerNotification,
    ItemReasoningTextDeltaServerNotification,
    ItemStartedServerNotification,
    ThreadResumeRequest,
    TurnCompletedServerNotification,
    TurnStartedServerNotification,
    WarningServerNotification,
)
from pydantic import ValidationError

from ksadk.events.adapters.codex import (
    _CODEX_0_147_0_NOTIFICATION_METHODS,
    CodexAdapterContext,
    CodexEventAdapter,
    CodexMappingError,
)
from ksadk.events.canonical import (
    ContinuationCreated,
    ContinuationResumed,
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
    ItemFailed,
    ItemStarted,
    ItemUpdated,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunProgress,
    RunStarted,
    RuntimeEvent,
    dump_runtime_event,
    parse_runtime_event,
)
from ksadk.events.content import (
    ArtifactContent,
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.events.reducer import StreamReducer


def _wire(model: type[Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and dump the exact installed 0.144.4 generated wire model."""

    return model.model_validate(payload).model_dump(
        mode="json", by_alias=True, exclude_none=True, warnings=False
    )


def _context() -> CodexAdapterContext:
    return CodexAdapterContext(run_id="runtime-run-9", initial_seq=20)


def _agent_item(item_id: str, text: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "agentMessage",
        "text": text,
        "phase": "final_answer",
    }


def _message_lifecycle(
    *, thread_id: str = "thread-1", turn_id: str = "turn-1", item_id: str = "msg-1"
) -> tuple[tuple[dict[str, Any], str, float], ...]:
    return (
        (
            _wire(
                ItemStartedServerNotification,
                {
                    "method": "item/started",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "startedAtMs": 1_786_440_000_000,
                        "item": _agent_item(item_id, ""),
                    },
                },
            ),
            "jsonl:101",
            1_786_440_000.0,
        ),
        (
            _wire(
                ItemAgentMessageDeltaServerNotification,
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "itemId": item_id,
                        "delta": "hel",
                    },
                },
            ),
            "jsonl:202",
            1_786_440_001.0,
        ),
        (
            _wire(
                ItemCompletedServerNotification,
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "completedAtMs": 1_786_440_002_000,
                        "item": _agent_item(item_id, "hello"),
                    },
                },
            ),
            "jsonl:303",
            1_786_440_002.0,
        ),
    )


def _map_all(
    frames: Iterable[tuple[Mapping[str, Any], str, float]],
    *,
    adapter: CodexEventAdapter | None = None,
    context: CodexAdapterContext | None = None,
) -> tuple[RuntimeEvent, ...]:
    mapper = adapter or CodexEventAdapter()
    ctx = context or _context()
    return tuple(
        canonical
        for raw, cursor, timestamp in frames
        for canonical in mapper.map_protocol_message(
            raw,
            ctx,
            native_cursor=cursor,
            timestamp=timestamp,
        )
    )


def test_real_agent_delta_then_completed_is_authoritative_snapshot() -> None:
    """Break caught: completed full text is appended after its streamed prefix."""

    frames = _message_lifecycle()
    mapped = _map_all(frames)
    replay = _map_all(frames)
    started = [event for event in mapped if isinstance(event, ItemStarted)]
    updated = [event for event in mapped if isinstance(event, ItemUpdated)]
    completed = [event for event in mapped if isinstance(event, ItemCompleted)]

    assert len(started) == len(updated) == len(completed) == 1
    assert started[0].item_kind == "message"
    assert started[0].phase == "final_answer"
    assert updated[0].op == "append"
    assert updated[0].source.native_cursor == "jsonl:202"
    assert updated[0].event_id != completed[0].event_id
    assert completed[0].snapshot.parts == (
        TextContent(part_id=updated[0].update.part_id, text="hello"),
    )
    assert [event.event_id for event in mapped] == [event.event_id for event in replay]
    assert all(event.schema_version == 2 for event in mapped)
    assert all(event.source.framework == "codex" for event in mapped)
    assert [event.seq for event in mapped] == [20, 21, 22]
    assert [
        dump_runtime_event(parse_runtime_event(dump_runtime_event(event))) for event in mapped
    ] == [dump_runtime_event(event) for event in mapped]

    reducer = StreamReducer()
    for event in (*mapped, *replay):
        reducer.apply(event)
    projection = reducer.snapshot()
    assert projection.items[0].status == "completed"
    assert projection.items[0].parts[0].text == "hello"


def test_same_native_item_in_two_turns_has_distinct_scope_and_item_identity() -> None:
    """Break caught: Codex itemId alone merges outputs across turns."""

    mapped = _map_all(
        (
            *_message_lifecycle(thread_id="thread-1", turn_id="turn-1", item_id="same"),
            *(
                (frame, cursor.replace("jsonl:", "jsonl:turn-2:"), timestamp)
                for frame, cursor, timestamp in _message_lifecycle(
                    thread_id="thread-1", turn_id="turn-2", item_id="same"
                )
            ),
        )
    )
    completed = [event for event in mapped if isinstance(event, ItemCompleted)]

    assert len(completed) == 2
    assert len({event.scope_id for event in completed}) == 2
    assert len({event.item_id for event in completed}) == 2
    assert [event.snapshot.parts[0].text for event in completed] == ["hello", "hello"]


def test_reasoning_delta_and_completed_snapshot_keep_native_parts() -> None:
    """Break caught: reasoning is flattened into the agent message text lane."""

    frames = (
        (
            _wire(
                ItemStartedServerNotification,
                {
                    "method": "item/started",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "startedAtMs": 1_786_440_010_000,
                        "item": {
                            "id": "reason-1",
                            "type": "reasoning",
                            "summary": [],
                            "content": [],
                        },
                    },
                },
            ),
            "jsonl:401",
            1_786_440_010.0,
        ),
        (
            _wire(
                ItemReasoningTextDeltaServerNotification,
                {
                    "method": "item/reasoning/textDelta",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "itemId": "reason-1",
                        "contentIndex": 0,
                        "delta": "think",
                    },
                },
            ),
            "jsonl:402",
            1_786_440_011.0,
        ),
        (
            _wire(
                ItemCompletedServerNotification,
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "completedAtMs": 1_786_440_012_000,
                        "item": {
                            "id": "reason-1",
                            "type": "reasoning",
                            "summary": ["brief"],
                            "content": ["think fully"],
                        },
                    },
                },
            ),
            "jsonl:403",
            1_786_440_012.0,
        ),
    )
    mapped = _map_all(frames)
    started = next(event for event in mapped if isinstance(event, ItemStarted))
    updated = next(event for event in mapped if isinstance(event, ItemUpdated))
    completed = next(event for event in mapped if isinstance(event, ItemCompleted))

    assert started.item_kind == updated.item_kind == completed.item_kind == "reasoning"
    assert started.phase == "commentary"
    assert updated.op == "append"
    assert isinstance(updated.update, TextContent)
    assert completed.snapshot.parts == (
        TextContent(part_id=completed.snapshot.parts[0].part_id, text="brief"),
        TextContent(part_id=updated.update.part_id, text="think fully"),
    )
    assert completed.snapshot.parts[0].part_id != completed.snapshot.parts[1].part_id


def test_command_mcp_and_file_change_stay_distinct_typed_items() -> None:
    """Break caught: all structured Codex items are converted to generic text."""

    command_start = {
        "id": "shared",
        "type": "commandExecution",
        "command": "printf hi",
        "cwd": "/tmp",
        "commandActions": [],
        "status": "inProgress",
        "aggregatedOutput": None,
    }
    command_done = {
        **command_start,
        "status": "completed",
        "aggregatedOutput": "hi",
        "exitCode": 0,
        "durationMs": 9,
    }
    mcp_start = {
        "id": "shared",
        "type": "mcpToolCall",
        "server": "docs",
        "tool": "search",
        "arguments": {"q": "x"},
        "status": "inProgress",
    }
    mcp_done = {
        **mcp_start,
        "status": "completed",
        "durationMs": 7,
        "result": {
            "content": [{"type": "text", "text": "found"}],
            "structuredContent": {"count": 1},
        },
    }
    changes = [{"path": "/tmp/a.txt", "kind": {"type": "update"}, "diff": "@@ -1 +1 @@"}]
    file_start = {
        "id": "shared",
        "type": "fileChange",
        "changes": [],
        "status": "inProgress",
    }
    file_done = {**file_start, "changes": changes, "status": "completed"}
    frames = (
        (
            _item_wire(ItemStartedServerNotification, "item/started", command_start),
            "jsonl:501",
            1.0,
        ),
        (
            _wire(
                ItemCommandExecutionOutputDeltaServerNotification,
                {
                    "method": "item/commandExecution/outputDelta",
                    "params": _item_params(item_id="shared", delta="hi"),
                },
            ),
            "jsonl:502",
            2.0,
        ),
        (
            _item_wire(ItemCompletedServerNotification, "item/completed", command_done),
            "jsonl:503",
            3.0,
        ),
        (_item_wire(ItemStartedServerNotification, "item/started", mcp_start), "jsonl:504", 4.0),
        (
            _wire(
                ItemMcpToolCallProgressServerNotification,
                {
                    "method": "item/mcpToolCall/progress",
                    "params": _item_params(item_id="shared", message="working"),
                },
            ),
            "jsonl:505",
            5.0,
        ),
        (_item_wire(ItemCompletedServerNotification, "item/completed", mcp_done), "jsonl:506", 6.0),
        (_item_wire(ItemStartedServerNotification, "item/started", file_start), "jsonl:507", 7.0),
        (
            _wire(
                ItemFileChangePatchUpdatedServerNotification,
                {
                    "method": "item/fileChange/patchUpdated",
                    "params": _item_params(item_id="shared", changes=changes),
                },
            ),
            "jsonl:508",
            8.0,
        ),
        (
            _item_wire(ItemCompletedServerNotification, "item/completed", file_done),
            "jsonl:509",
            9.0,
        ),
    )
    mapped = _map_all(frames)
    completed = [event for event in mapped if isinstance(event, ItemCompleted)]

    assert [event.item_kind for event in completed] == ["tool_call", "tool_call", "data"]
    assert [event.source.metadata["native_item_kind"] for event in completed] == [
        "commandExecution",
        "mcpToolCall",
        "fileChange",
    ]
    assert len({event.item_id for event in completed}) == 3
    assert [type(part) for part in completed[0].snapshot.parts] == [
        ToolCallContent,
        ToolResultContent,
    ]
    assert [type(part) for part in completed[1].snapshot.parts] == [
        ToolCallContent,
        ToolResultContent,
    ]
    assert completed[0].snapshot.parts[1].result["output"] == "hi"
    assert completed[1].snapshot.parts[1].result["structuredContent"] == {"count": 1}
    file_content = completed[2].snapshot.parts[0]
    assert isinstance(file_content, DataContent)
    assert file_content.data == {"changes": changes, "status": "completed"}


def test_generated_plan_dynamic_collab_web_and_image_items_are_typed() -> None:
    """Every legal 0.144.4 ThreadItem lane has a lossless canonical lifecycle."""

    plan_start = {"id": "plan-1", "type": "plan", "text": ""}
    plan_done = {**plan_start, "text": "1. inspect\n2. fix"}
    dynamic_start = {
        "id": "dynamic-1",
        "type": "dynamicToolCall",
        "tool": "render",
        "arguments": {"quality": "high"},
        "namespace": "studio",
        "status": "inProgress",
    }
    dynamic_done = {
        **dynamic_start,
        "status": "completed",
        "success": True,
        "durationMs": 12,
        "contentItems": [{"type": "inputText", "text": "rendered"}],
    }
    collab_start = {
        "id": "collab-1",
        "type": "collabAgentToolCall",
        "tool": "spawnAgent",
        "senderThreadId": "thread-1",
        "receiverThreadIds": ["child-1"],
        "agentsStates": {"child-1": {"status": "running"}},
        "status": "inProgress",
        "prompt": "review",
    }
    collab_done = {
        **collab_start,
        "agentsStates": {"child-1": {"status": "completed", "message": "done"}},
        "status": "completed",
    }
    web = {"id": "web-1", "type": "webSearch", "query": "Codex 0.144.4"}
    image_start = {
        "id": "image-1",
        "type": "imageGeneration",
        "result": "",
        "revisedPrompt": "blue bird",
        "status": "inProgress",
    }
    image_done = {
        **image_start,
        "result": "https://example.test/image.png",
        "savedPath": "/tmp/image.png",
        "status": "completed",
    }
    frames: list[tuple[dict[str, Any], str, float]] = [
        (_item_wire(ItemStartedServerNotification, "item/started", plan_start), "jsonl:1101", 1),
        (
            _wire(
                ItemPlanDeltaServerNotification,
                {
                    "method": "item/plan/delta",
                    "params": _item_params(item_id="plan-1", delta="1. inspect"),
                },
            ),
            "jsonl:1102",
            2,
        ),
        (_item_wire(ItemCompletedServerNotification, "item/completed", plan_done), "jsonl:1103", 3),
    ]
    for offset, (started, completed) in enumerate(
        (
            (dynamic_start, dynamic_done),
            (collab_start, collab_done),
            (web, web),
            (image_start, image_done),
        ),
        start=1,
    ):
        frames.extend(
            (
                (
                    _item_wire(ItemStartedServerNotification, "item/started", started),
                    f"jsonl:11{offset}1",
                    float(offset * 10),
                ),
                (
                    _item_wire(ItemCompletedServerNotification, "item/completed", completed),
                    f"jsonl:11{offset}2",
                    float(offset * 10 + 1),
                ),
            )
        )

    mapped = _map_all(frames)
    completed = [event for event in mapped if isinstance(event, ItemCompleted)]

    assert [event.item_kind for event in completed] == [
        "data",
        "tool_call",
        "tool_call",
        "tool_call",
        "artifact",
    ]
    plan_delta = next(
        event
        for event in mapped
        if isinstance(event, ItemUpdated) and event.source.metadata["method"] == "item/plan/delta"
    )
    assert plan_delta.op == "append"
    assert isinstance(plan_delta.update, TextContent)
    assert isinstance(completed[1].snapshot.parts[0], ToolCallContent)
    assert isinstance(completed[1].snapshot.parts[1], ToolResultContent)
    assert isinstance(completed[2].snapshot.parts[0], ToolCallContent)
    assert isinstance(completed[3].snapshot.parts[0], ToolCallContent)
    assert isinstance(completed[4].snapshot.parts[0], ArtifactContent)
    assert completed[4].snapshot.parts[0].uri == "https://example.test/image.png"


def test_every_remaining_generated_thread_item_type_has_data_lifecycle() -> None:
    """The complete 0.144.4 ThreadItem union is accepted, not just output lanes."""

    items = (
        {
            "id": "user-1",
            "type": "userMessage",
            "clientId": "client-1",
            "content": [{"type": "text", "text": "hello"}],
        },
        {
            "id": "hook-1",
            "type": "hookPrompt",
            "fragments": [{"hookRunId": "hook-run-1", "text": "policy"}],
        },
        {
            "id": "subagent-1",
            "type": "subAgentActivity",
            "agentPath": "reviewer",
            "agentThreadId": "child-1",
            "kind": "started",
        },
        {"id": "view-1", "type": "imageView", "path": "/tmp/input.png"},
        {"id": "sleep-1", "type": "sleep", "durationMs": 1000},
        {"id": "review-in", "type": "enteredReviewMode", "review": "review changes"},
        {"id": "review-out", "type": "exitedReviewMode", "review": "looks good"},
        {"id": "compact-1", "type": "contextCompaction"},
    )
    frames = tuple(
        frame
        for index, item in enumerate(items)
        for frame in (
            (
                _item_wire(ItemStartedServerNotification, "item/started", item),
                f"jsonl:119:{index}:start",
                float(index * 2),
            ),
            (
                _item_wire(ItemCompletedServerNotification, "item/completed", item),
                f"jsonl:119:{index}:completed",
                float(index * 2 + 1),
            ),
        )
    )
    mapped = _map_all(frames)
    completed = [event for event in mapped if isinstance(event, ItemCompleted)]

    assert len(completed) == len(items)
    assert {event.item_kind for event in completed} == {"data"}
    for index, event in enumerate(completed):
        part = event.snapshot.parts[0]
        assert isinstance(part, DataContent)
        assert part.data == frames[index * 2][0]["params"]["item"]


def test_generated_error_and_file_output_delta_are_known_typed_methods() -> None:
    """Legal generated notifications must never fall into unsupported_method."""

    file_item = {"id": "file-1", "type": "fileChange", "changes": [], "status": "inProgress"}
    started = _turn_wire(TurnStartedServerNotification, "turn/started", "turn-1", "inProgress")
    retryable_error = _wire(
        ErrorServerNotification,
        {
            "method": "error",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "error": {"message": "temporary overload"},
                "willRetry": True,
            },
        },
    )
    output_delta = _wire(
        ItemFileChangeOutputDeltaServerNotification,
        {
            "method": "item/fileChange/outputDelta",
            "params": _item_params(item_id="file-1", delta="applying patch"),
        },
    )
    mapped = _map_all(
        (
            (started, "jsonl:1201", 1),
            (
                _item_wire(ItemStartedServerNotification, "item/started", file_item),
                "jsonl:1202",
                2,
            ),
            (output_delta, "jsonl:1203", 3),
            (retryable_error, "jsonl:1204", 4),
        )
    )

    file_update = next(event for event in mapped if isinstance(event, ItemUpdated))
    progress = next(event for event in mapped if isinstance(event, RunProgress))
    assert file_update.op == "append"
    assert isinstance(file_update.update, TextContent)
    assert file_update.update.text == "applying patch"
    assert progress.status == "running"
    assert progress.message == "Codex reported a retryable turn error"
    assert "temporary overload" not in progress.model_dump_json()


def test_error_notification_persists_only_safe_turn_error_markers() -> None:
    """TurnError free text and nested details must never enter canonical storage."""

    mapped = _map_all(
        (
            (
                _wire(
                    ErrorServerNotification,
                    {
                        "method": "error",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-sensitive",
                            "error": {
                                "message": "message-secret-7",
                                "additionalDetails": "details-secret-8",
                                "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 429}},
                            },
                            "willRetry": True,
                        },
                    },
                ),
                "jsonl:sensitive-error",
                1.0,
            ),
        )
    )

    progress = next(event for event in mapped if isinstance(event, RunProgress))
    persisted = progress.model_dump_json()
    assert "message-secret-7" not in persisted
    assert "details-secret-8" not in persisted
    assert "httpStatusCode" not in persisted
    assert progress.message == "Codex reported a retryable turn error"
    assert progress.source.metadata["will_retry"] is True
    assert progress.source.metadata["error_message_present"] is True
    assert progress.source.metadata["additional_details_present"] is True
    assert progress.source.metadata["codex_error_info_kind"] == "httpConnectionFailed"


def test_nonretry_error_is_diagnostic_until_authoritative_turn_failure() -> None:
    """The error notification precedes the authoritative failed turn terminal."""

    terminal = _turn("turn-error", "failed")
    terminal["error"] = {"message": "model failed"}
    frames = (
        (
            _turn_wire(
                TurnStartedServerNotification,
                "turn/started",
                "turn-error",
                "inProgress",
            ),
            "jsonl:1211",
            1,
        ),
        (
            _wire(
                ErrorServerNotification,
                {
                    "method": "error",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-error",
                        "error": {"message": "model failed"},
                        "willRetry": False,
                    },
                },
            ),
            "jsonl:1212",
            2,
        ),
        (
            _wire(
                TurnCompletedServerNotification,
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": terminal},
                },
            ),
            "jsonl:1213",
            3,
        ),
    )

    mapped = _map_all(frames)
    assert len([event for event in mapped if isinstance(event, RunProgress)]) == 1
    assert len([event for event in mapped if isinstance(event, RunFailed)]) == 1
    reducer = StreamReducer()
    for event in mapped:
        reducer.apply(event)
    assert reducer.snapshot().status == "failed"


def test_complete_generated_notification_registry_has_lossless_data_fallback() -> None:
    """The locked 0.147.0 registry cannot drift into silent drops as methods grow."""

    assert _CODEX_0_147_0_NOTIFICATION_METHODS == frozenset(NOTIFICATION_MODELS)
    warning = _wire(
        WarningServerNotification,
        {
            "method": "warning",
            "params": {"threadId": "thread-1", "message": "context nearly full"},
        },
    )
    mapped = _map_all(((warning, "jsonl:1210", 1),))
    started = next(event for event in mapped if isinstance(event, ItemStarted))
    completed = next(event for event in mapped if isinstance(event, ItemCompleted))

    assert started.item_kind == completed.item_kind == "data"
    assert completed.snapshot.parts == (
        DataContent(
            part_id=completed.snapshot.parts[0].part_id,
            data={"threadId": "thread-1", "message": "context nearly full"},
        ),
    )


def test_permissions_dynamic_tool_and_mcp_elicitation_requests_are_typed() -> None:
    """The three additional legal 0.144.4 server requests survive round-trip HITL."""

    requests = (
        (
            {
                "id": "permissions-1",
                "method": "item/permissions/requestApproval",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "permission-item",
                    "startedAtMs": 1,
                    "cwd": "/tmp",
                    "reason": "write",
                    "permissions": {"fileSystem": {"read": ["/tmp"], "write": ["/tmp"]}},
                },
            },
            {"id": "permissions-1", "result": {"permissions": {}, "scope": "turn"}},
        ),
        (
            {
                "id": "dynamic-1",
                "method": "item/tool/call",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "callId": "dynamic-call-1",
                    "namespace": "studio",
                    "tool": "render",
                    "arguments": {"x": 1},
                },
            },
            {
                "id": "dynamic-1",
                "result": {
                    "contentItems": [{"type": "inputText", "text": "ok"}],
                    "success": True,
                },
            },
        ),
        (
            {
                "id": "elicitation-1",
                "method": "mcpServer/elicitation/request",
                "params": {
                    "threadId": "thread-1",
                    "serverName": "docs",
                    "mode": "form",
                    "message": "Choose format",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"format": {"type": "string"}},
                    },
                },
            },
            {
                "id": "elicitation-1",
                "result": {"action": "accept", "content": {"format": "markdown"}},
            },
        ),
    )
    frames = tuple(
        frame
        for index, (request, response) in enumerate(requests)
        for frame in (
            (request, f"jsonl:13{index}1", float(index * 2 + 1)),
            (response, f"jsonl:13{index}2", float(index * 2 + 2)),
        )
    )
    mapped = _map_all(frames)
    requested = [event for event in mapped if isinstance(event, InteractionRequested)]
    resolved = [event for event in mapped if isinstance(event, InteractionResolved)]

    assert [event.interaction_kind for event in requested] == [
        "approval",
        "approval",
        "structured_input",
    ]
    assert requested[0].request.kind == "permissions"
    assert requested[1].request.kind == "dynamic_tool_call"
    assert requested[2].request.prompt == "Choose format"
    assert requested[2].request.schema_["properties"]["format"] == {"type": "string"}
    assert [event.interaction_id for event in requested] == [
        event.interaction_id for event in resolved
    ]
    assert len([event for event in mapped if isinstance(event, RunInterrupted)]) == 2


def _item_params(*, item_id: str | None = None, **extra: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"threadId": "thread-1", "turnId": "turn-1", **extra}
    if item_id is not None:
        params["itemId"] = item_id
    return params


def _item_wire(model: type[Any], method: str, item: Mapping[str, Any]) -> dict[str, Any]:
    timestamp_key = "startedAtMs" if method == "item/started" else "completedAtMs"
    return _wire(
        model,
        {
            "method": method,
            "params": _item_params(item=item, **{timestamp_key: 1_786_440_020_000}),
        },
    )


def _turn(
    turn_id: str, status: str, *, items: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    turn: dict[str, Any] = {
        "id": turn_id,
        "status": status,
        "items": items or [],
        "itemsView": "full",
        "startedAt": 1_786_440_000,
    }
    if status != "inProgress":
        turn["completedAt"] = 1_786_440_030
        turn["durationMs"] = 30_000
    return turn


def _turn_wire(
    model: type[Any],
    method: str,
    turn_id: str,
    status: str,
    *,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _wire(
        model,
        {
            "method": method,
            "params": {"threadId": "thread-1", "turn": _turn(turn_id, status, items=items)},
        },
    )


def test_turn_lifecycle_uses_completed_item_output_refs() -> None:
    """Break caught: turn completion closes the run without canonical output identity."""

    frames = (
        (
            _turn_wire(TurnStartedServerNotification, "turn/started", "turn-1", "inProgress"),
            "jsonl:601",
            1.0,
        ),
        *_message_lifecycle(),
        (
            _turn_wire(
                TurnCompletedServerNotification,
                "turn/completed",
                "turn-1",
                "completed",
            ),
            "jsonl:602",
            4.0,
        ),
    )
    mapped = _map_all(frames)
    started = [event for event in mapped if isinstance(event, RunStarted)]
    completed_item = next(event for event in mapped if isinstance(event, ItemCompleted))
    completed_run = next(event for event in mapped if isinstance(event, RunCompleted))

    assert len(started) == 1
    assert frames[-1][0]["params"]["turn"]["items"] == []
    assert completed_run.output_refs[0].scope_id == completed_item.scope_id
    assert completed_run.output_refs[0].item_id == completed_item.item_id
    assert completed_run.source.native_run_id == "turn-1"


@pytest.mark.parametrize(
    ("status", "error", "terminal_type"),
    [
        ("failed", {"message": "model failed"}, RunFailed),
        ("interrupted", None, RunCanceled),
    ],
)
def test_real_terminal_turn_status_maps_failure_or_cancellation(
    status: str, error: dict[str, Any] | None, terminal_type: type[RuntimeEvent]
) -> None:
    """Break caught: every turn/completed status is projected as success."""

    terminal = _turn("turn-terminal", status)
    if error is not None:
        terminal["error"] = error
    frames = (
        (
            _turn_wire(
                TurnStartedServerNotification, "turn/started", "turn-terminal", "inProgress"
            ),
            "jsonl:610",
            1.0,
        ),
        (
            _wire(
                TurnCompletedServerNotification,
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": terminal},
                },
            ),
            "jsonl:611",
            2.0,
        ),
    )
    mapped = _map_all(frames)

    assert any(isinstance(event, terminal_type) for event in mapped)
    assert not any(isinstance(event, RunCompleted) for event in mapped)


def test_command_approval_request_and_response_preserve_interaction_identity() -> None:
    """Break caught: a JSON-RPC approval request is dropped by notification-only parsing."""

    request = {
        "id": 73,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "cmd-1",
            "startedAtMs": 1_786_440_040_000,
            "reason": "network access",
            "command": "curl example.test",
            "cwd": "/tmp",
            "commandActions": [],
        },
    }
    response = {"id": 73, "result": {"decision": "accept"}}
    mapped = _map_all(
        (
            (request, "jsonl:701", 1.0),
            (response, "jsonl:702", 2.0),
        )
    )
    requested = next(event for event in mapped if isinstance(event, InteractionRequested))
    interrupted = next(event for event in mapped if isinstance(event, RunInterrupted))
    resolved = next(event for event in mapped if isinstance(event, InteractionResolved))

    assert requested.interaction_kind == "approval"
    assert requested.request.call_id == "cmd-1"
    assert requested.source.native_event_id == "73"
    assert requested.interaction_id == interrupted.interaction_id == resolved.interaction_id
    assert resolved.response.decision == "approved"


def test_structured_input_request_and_response_preserve_question_schema() -> None:
    """Break caught: requestUserInput is mislabeled as an approval or plain text."""

    request = {
        "id": "input-8",
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "tool-1",
            "questions": [
                {
                    "id": "choice",
                    "header": "Choose",
                    "question": "Pick one",
                    "isOther": False,
                    "isSecret": False,
                    "options": [
                        {"label": "A", "description": "first"},
                        {"label": "B", "description": "second"},
                    ],
                }
            ],
            "autoResolutionMs": None,
        },
    }
    response = {
        "id": "input-8",
        "result": {"answers": {"choice": {"answers": ["A"]}}},
    }
    mapped = _map_all(
        (
            (request, "jsonl:711", 1.0),
            (response, "jsonl:712", 2.0),
        )
    )
    requested = next(event for event in mapped if isinstance(event, InteractionRequested))
    resolved = next(event for event in mapped if isinstance(event, InteractionResolved))

    assert requested.interaction_kind == "structured_input"
    assert requested.request.prompt == "Pick one"
    assert requested.request.schema_["properties"]["choice"]["enum"] == ["A", "B"]
    assert requested.interaction_id == resolved.interaction_id
    assert resolved.response.data == {"answers": {"choice": {"answers": ["A"]}}}


def test_secret_user_input_answers_are_redacted_per_question() -> None:
    """Secret answers must not enter canonical events while public answers remain useful."""

    request = {
        "id": "input-secret",
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "tool-secret",
            "questions": [
                {
                    "id": "region",
                    "header": "Region",
                    "question": "Choose region",
                    "isOther": False,
                    "isSecret": False,
                    "options": None,
                },
                {
                    "id": "credential",
                    "header": "Credential",
                    "question": "Provide credential",
                    "isOther": False,
                    "isSecret": True,
                    "options": None,
                },
            ],
            "autoResolutionMs": None,
        },
    }
    mapped = _map_all(
        (
            (request, "jsonl:secret-request", 1.0),
            (
                {
                    "id": "input-secret",
                    "result": {
                        "answers": {
                            "region": {"answers": ["west"]},
                            "credential": {"answers": ["credential-secret-9"]},
                        }
                    },
                },
                "jsonl:secret-response",
                2.0,
            ),
        )
    )

    resolved = next(event for event in mapped if isinstance(event, InteractionResolved))
    assert "credential-secret-9" not in resolved.model_dump_json()
    assert resolved.response.data == {
        "answers": {
            "region": {"answers": ["west"]},
            "credential": {"answersPresent": True, "redacted": True},
        }
    }


def test_secret_question_marker_requires_a_real_boolean_transactionally() -> None:
    """Truthy strings cannot silently turn arbitrary question ids into secret state."""

    adapter = CodexEventAdapter()
    context = _context()
    request = {
        "id": "strict-secret",
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "tool-strict-secret",
            "questions": [
                {
                    "id": "credential",
                    "header": "Credential",
                    "question": "Provide credential",
                    "isOther": False,
                    "isSecret": "true",
                    "options": None,
                }
            ],
            "autoResolutionMs": None,
        },
    }

    with pytest.raises(CodexMappingError) as caught:
        adapter.map_protocol_message(
            request,
            context,
            native_cursor="jsonl:strict-secret",
            timestamp=1.0,
        )
    assert caught.value.code == "invalid_interaction_request"

    request["params"]["questions"][0]["isSecret"] = True
    requested = adapter.map_protocol_message(
        request,
        context,
        native_cursor="jsonl:strict-secret",
        timestamp=2.0,
    )
    assert [event.seq for event in requested] == [20, 21]


def test_unknown_user_input_answer_id_fails_closed_and_can_retry() -> None:
    """A renamed secret answer key cannot be persisted as a public answer."""

    adapter = CodexEventAdapter()
    context = _context()
    request = {
        "id": "secret-key-retry",
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "tool-secret-key",
            "questions": [
                {
                    "id": "credential",
                    "header": "Credential",
                    "question": "Provide credential",
                    "isOther": False,
                    "isSecret": True,
                    "options": None,
                }
            ],
            "autoResolutionMs": None,
        },
    }
    adapter.map_protocol_message(
        request,
        context,
        native_cursor="jsonl:secret-key-request",
        timestamp=1.0,
    )
    invalid_response = {
        "id": "secret-key-retry",
        "result": {"answers": {"credential_typo": {"answers": ["renamed-key-secret-11"]}}},
    }

    with pytest.raises(CodexMappingError) as caught:
        adapter.map_protocol_message(
            invalid_response,
            context,
            native_cursor="jsonl:secret-key-response",
            timestamp=2.0,
        )
    assert caught.value.code == "invalid_interaction_response"
    assert "renamed-key-secret-11" not in str(caught.value)

    corrected = adapter.map_protocol_message(
        {
            "id": "secret-key-retry",
            "result": {"answers": {"credential": {"answers": ["renamed-key-secret-11"]}}},
        },
        context,
        native_cursor="jsonl:secret-key-response",
        timestamp=3.0,
    )
    resolved = next(event for event in corrected if isinstance(event, InteractionResolved))
    assert "renamed-key-secret-11" not in resolved.model_dump_json()
    assert resolved.response.data == {
        "answers": {
            "credential": {"answersPresent": True, "redacted": True},
        }
    }
    assert [event.seq for event in corrected] == [22, 23]
    adapter.finish_stream()


def test_thread_resume_request_resumes_same_continuation_in_new_turn_scope() -> None:
    """Break caught: cold resume changes continuation scope to the new turn."""

    first_turn = _turn_wire(TurnStartedServerNotification, "turn/started", "turn-1", "inProgress")
    resume = _wire(
        ThreadResumeRequest,
        {"id": "resume-rpc-9", "method": "thread/resume", "params": {"threadId": "thread-1"}},
    )
    second_turn = _turn_wire(TurnStartedServerNotification, "turn/started", "turn-2", "inProgress")
    original = _map_all(((first_turn, "jsonl:801", 1.0),))
    restored = _map_all(
        (
            (resume, "jsonl:802", 2.0),
            (second_turn, "jsonl:803", 3.0),
        ),
        adapter=CodexEventAdapter(),
        context=CodexAdapterContext(run_id="runtime-run-9", initial_seq=30),
    )
    mapped = (*original, *restored)
    created = next(event for event in mapped if isinstance(event, ContinuationCreated))
    resumed = next(event for event in mapped if isinstance(event, ContinuationResumed))
    run_starts = [event for event in mapped if isinstance(event, RunStarted)]

    assert created.continuation_kind == resumed.continuation_kind == "thread_resume"
    assert created.continuation_id == resumed.continuation_id
    assert created.scope_id == resumed.scope_id
    assert created.ref == {
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "source_cursor": "jsonl:801",
    }
    assert "checkpoint_id" not in created.ref
    assert resumed.resume_attempt_id == "resume-rpc-9"
    assert len({event.scope_id for event in run_starts}) == 2

    reducer = StreamReducer()
    reducer.apply(created)
    reducer.apply(resumed)
    continuation = reducer.snapshot().continuations[0]
    assert continuation.scope_id == created.scope_id
    assert continuation.resume_attempt_ids == ("resume-rpc-9",)


def test_reasoning_summary_part_added_delta_and_snapshot_share_part_identity() -> None:
    """Break caught: summaryIndex is ignored and summary deltas overwrite raw reasoning."""

    reasoning = {"id": "reason-s", "type": "reasoning", "summary": [], "content": []}
    completed_reasoning = {
        **reasoning,
        "summary": ["summary final"],
        "content": ["raw thought"],
    }
    frames = (
        (_item_wire(ItemStartedServerNotification, "item/started", reasoning), "jsonl:901", 1.0),
        (
            _wire(
                ItemReasoningSummaryPartAddedServerNotification,
                {
                    "method": "item/reasoning/summaryPartAdded",
                    "params": _item_params(item_id="reason-s", summaryIndex=0),
                },
            ),
            "jsonl:902",
            2.0,
        ),
        (
            _wire(
                ItemReasoningSummaryTextDeltaServerNotification,
                {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": _item_params(item_id="reason-s", summaryIndex=0, delta="summary"),
                },
            ),
            "jsonl:903",
            3.0,
        ),
        (
            _item_wire(ItemCompletedServerNotification, "item/completed", completed_reasoning),
            "jsonl:904",
            4.0,
        ),
    )
    mapped = _map_all(frames)
    updates = [event for event in mapped if isinstance(event, ItemUpdated)]
    completed = next(event for event in mapped if isinstance(event, ItemCompleted))

    assert [event.op for event in updates] == ["replace", "append"]
    assert updates[0].update.part_id == updates[1].update.part_id
    assert completed.snapshot.parts[0].part_id == updates[0].update.part_id
    assert completed.snapshot.parts[0].text == "summary final"
    assert completed.snapshot.parts[1].text == "raw thought"


@pytest.mark.parametrize(
    ("native_decision", "canonical_decision"),
    [("decline", "rejected"), ("cancel", "canceled")],
)
def test_approval_decline_and_cancel_keep_distinct_resolution(
    native_decision: str, canonical_decision: str
) -> None:
    """Break caught: every non-accept approval response is rejected as unknown."""

    request = {
        "id": "approval-terminal",
        "method": "item/fileChange/requestApproval",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "patch-1",
            "startedAtMs": 1_786_440_050_000,
            "reason": "write",
            "grantRoot": None,
        },
    }
    mapped = _map_all(
        (
            (request, "jsonl:911", 1.0),
            (
                {"id": "approval-terminal", "result": {"decision": native_decision}},
                "jsonl:912",
                2.0,
            ),
        )
    )
    resolved = next(event for event in mapped if isinstance(event, InteractionResolved))

    assert resolved.response.decision == canonical_decision


def test_failed_command_emits_authoritative_result_then_item_failure() -> None:
    """Break caught: a failed native command is marked as a completed item."""

    started = {
        "id": "cmd-fail",
        "type": "commandExecution",
        "command": "false",
        "cwd": "/tmp",
        "commandActions": [],
        "status": "inProgress",
    }
    failed = {
        **started,
        "status": "failed",
        "aggregatedOutput": "boom",
        "exitCode": 1,
        "durationMs": 3,
    }
    mapped = _map_all(
        (
            (_item_wire(ItemStartedServerNotification, "item/started", started), "jsonl:921", 1.0),
            (
                _item_wire(ItemCompletedServerNotification, "item/completed", failed),
                "jsonl:922",
                2.0,
            ),
        )
    )
    corrected = next(event for event in mapped if isinstance(event, ItemUpdated))
    item_failed = next(event for event in mapped if isinstance(event, ItemFailed))

    assert corrected.op == "replace"
    assert isinstance(corrected.update, ToolResultContent)
    assert corrected.update.result["output"] == "boom"
    assert corrected.update.is_error is True
    assert item_failed.error.code == "codex_command_failed"
    assert not any(isinstance(event, ItemCompleted) for event in mapped)


def test_finish_stream_rejects_truncated_open_item() -> None:
    """Break caught: EOF silently leaves a provisional Codex item open forever."""

    adapter = CodexEventAdapter()
    raw, cursor, timestamp = _message_lifecycle()[0]
    adapter.map_protocol_message(raw, _context(), native_cursor=cursor, timestamp=timestamp)

    with pytest.raises(CodexMappingError) as caught:
        adapter.finish_stream()
    assert caught.value.code == "open_state_at_stream_end"


def test_unknown_notification_method_fails_closed() -> None:
    """Break caught: a future app-server notification is mistaken for an item delta."""

    with pytest.raises(CodexMappingError) as caught:
        CodexEventAdapter().map_protocol_message(
            {
                "method": "item/futureLane/delta",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "future-1",
                    "delta": "x",
                },
            },
            _context(),
            native_cursor="jsonl:999",
            timestamp=1.0,
        )
    assert caught.value.code == "unsupported_method"


def test_invalid_terminal_frame_does_not_consume_active_turn() -> None:
    """Break caught: a malformed terminal frame prevents later authoritative correction."""

    adapter = CodexEventAdapter()
    context = _context()
    adapter.map_protocol_message(
        _turn_wire(TurnStartedServerNotification, "turn/started", "turn-safe", "inProgress"),
        context,
        native_cursor="jsonl:1001",
        timestamp=1.0,
    )
    with pytest.raises(CodexMappingError) as caught:
        adapter.map_protocol_message(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": _turn("turn-safe", "futureTerminal"),
                },
            },
            context,
            native_cursor="jsonl:1002",
            timestamp=2.0,
        )
    assert caught.value.code == "invalid_turn_status"

    corrected = adapter.map_protocol_message(
        _turn_wire(
            TurnCompletedServerNotification,
            "turn/completed",
            "turn-safe",
            "completed",
        ),
        context,
        native_cursor="jsonl:1003",
        timestamp=3.0,
    )
    assert any(isinstance(event, RunCompleted) for event in corrected)


def test_invalid_interaction_response_keeps_request_resolvable() -> None:
    """Break caught: malformed JSON-RPC response silently discards pending HITL state."""

    adapter = CodexEventAdapter()
    context = _context()
    request = {
        "id": "approval-retry",
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "cmd-retry",
            "startedAtMs": 1_786_440_060_000,
            "command": "echo ok",
            "cwd": "/tmp",
            "commandActions": [],
        },
    }
    adapter.map_protocol_message(request, context, native_cursor="jsonl:1011", timestamp=1.0)
    with pytest.raises(CodexMappingError) as caught:
        adapter.map_protocol_message(
            {"id": "approval-retry", "result": {}},
            context,
            native_cursor="jsonl:1012",
            timestamp=2.0,
        )
    assert caught.value.code == "missing_native_identity"

    resolved = adapter.map_protocol_message(
        {"id": "approval-retry", "result": {"decision": "accept"}},
        context,
        native_cursor="jsonl:1013",
        timestamp=3.0,
    )
    assert isinstance(resolved[0], InteractionResolved)


def test_failed_item_mapping_rolls_back_state_cursor_and_part_identity() -> None:
    """A malformed command start must permit a clean same-id/cursor retry."""

    adapter = CodexEventAdapter()
    context = _context()
    command = {
        "id": "command-transaction",
        "type": "commandExecution",
        "command": "printf safe",
        "cwd": "/tmp",
        "commandActions": [],
        "status": "inProgress",
        "aggregatedOutput": None,
    }
    malformed = {
        "method": "item/started",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-transaction",
            "item": {key: value for key, value in command.items() if key != "command"},
        },
    }

    with pytest.raises(CodexMappingError, match="command"):
        adapter.map_protocol_message(
            malformed,
            context,
            native_cursor="jsonl:item-transaction",
            timestamp=1.0,
        )

    started = adapter.map_protocol_message(
        {
            **malformed,
            "params": {**malformed["params"], "item": command},
        },
        context,
        native_cursor="jsonl:item-transaction",
        timestamp=2.0,
    )
    completed = adapter.map_protocol_message(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-transaction",
                "item": {
                    **command,
                    "status": "completed",
                    "aggregatedOutput": "safe",
                    "exitCode": 0,
                    "durationMs": 1,
                },
            },
        },
        context,
        native_cursor="jsonl:item-transaction-completed",
        timestamp=3.0,
    )

    assert [event.seq for event in (*started, *completed)] == [20, 21]
    adapter.finish_stream()


def test_failed_turn_mapping_rolls_back_state_and_placeholder_seq() -> None:
    """Even a canonical construction error cannot strand an active turn."""

    adapter = CodexEventAdapter()
    context = _context()
    frame = _turn_wire(
        TurnStartedServerNotification,
        "turn/started",
        "turn-transaction",
        "inProgress",
    )

    with pytest.raises(ValidationError):
        adapter.map_protocol_message(
            frame,
            context,
            native_cursor="jsonl:turn-transaction",
            timestamp="invalid",  # type: ignore[arg-type]
        )

    started = adapter.map_protocol_message(
        frame,
        context,
        native_cursor="jsonl:turn-transaction",
        timestamp=1.0,
    )
    completed = adapter.map_protocol_message(
        _turn_wire(
            TurnCompletedServerNotification,
            "turn/completed",
            "turn-transaction",
            "completed",
        ),
        context,
        native_cursor="jsonl:turn-transaction-completed",
        timestamp=2.0,
    )

    assert [event.seq for event in (*started, *completed)] == [20, 21, 22]
    adapter.finish_stream()


def test_failed_interaction_mapping_rolls_back_state_and_placeholder_seq() -> None:
    """A failed request projection must not make its JSON-RPC id permanently pending."""

    adapter = CodexEventAdapter()
    context = _context()
    request = {
        "id": "input-transaction",
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "tool-transaction",
            "questions": [
                {
                    "id": "answer",
                    "header": "Answer",
                    "question": "Provide an answer",
                    "isOther": False,
                    "isSecret": False,
                    "options": None,
                }
            ],
            "autoResolutionMs": None,
        },
    }

    with pytest.raises(ValidationError):
        adapter.map_protocol_message(
            request,
            context,
            native_cursor="jsonl:interaction-transaction",
            timestamp="invalid",  # type: ignore[arg-type]
        )

    requested = adapter.map_protocol_message(
        request,
        context,
        native_cursor="jsonl:interaction-transaction",
        timestamp=1.0,
    )
    resolved = adapter.map_protocol_message(
        {
            "id": "input-transaction",
            "result": {"answers": {"answer": {"answers": ["safe"]}}},
        },
        context,
        native_cursor="jsonl:interaction-transaction-resolved",
        timestamp=2.0,
    )

    assert [event.seq for event in (*requested, *resolved)] == [20, 21, 22, 23]
    adapter.finish_stream()


def test_accept_for_session_is_an_approved_native_decision() -> None:
    """Break caught: a valid 0.144.4 session approval is rejected as unknown."""

    mapped = _map_all(
        (
            (
                {
                    "id": "approval-session",
                    "method": "item/fileChange/requestApproval",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "itemId": "patch-session",
                        "startedAtMs": 1_786_440_070_000,
                        "reason": "session write",
                        "grantRoot": None,
                    },
                },
                "jsonl:1021",
                1.0,
            ),
            (
                {
                    "id": "approval-session",
                    "result": {"decision": "acceptForSession"},
                },
                "jsonl:1022",
                2.0,
            ),
        )
    )
    resolved = next(event for event in mapped if isinstance(event, InteractionResolved))
    assert resolved.response.decision == "approved"


@pytest.mark.parametrize(
    "decision",
    [
        {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": {"command": ["git", "status"]}}},
        {
            "applyNetworkPolicyAmendment": {
                "network_policy_amendment": {"host": "example.test", "action": "allow"}
            }
        },
    ],
)
def test_tagged_command_approval_amendments_are_approved(decision: dict[str, Any]) -> None:
    """0.144.4 structured approval variants are externally tagged objects."""

    mapped = _map_all(
        (
            (
                {
                    "id": "approval-amendment",
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "itemId": "cmd-amendment",
                        "startedAtMs": 1,
                        "command": "git status",
                        "cwd": "/tmp",
                        "commandActions": [],
                    },
                },
                "jsonl:1401",
                1,
            ),
            (
                {"id": "approval-amendment", "result": {"decision": decision}},
                "jsonl:1402",
                2,
            ),
        )
    )
    resolved = next(event for event in mapped if isinstance(event, InteractionResolved))
    assert resolved.response.decision == "approved"
    assert resolved.response.data["decision"] == decision


@pytest.mark.parametrize(
    "decision",
    ["acceptWithExecpolicyAmendment", "applyNetworkPolicyAmendment"],
)
def test_structured_command_approval_variant_rejects_naked_string(decision: str) -> None:
    """A malformed string must not erase the amendment payload required by the protocol."""

    adapter = CodexEventAdapter()
    context = _context()
    adapter.map_protocol_message(
        {
            "id": "approval-invalid-amendment",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "cmd-amendment",
                "startedAtMs": 1,
                "command": "git status",
                "cwd": "/tmp",
                "commandActions": [],
            },
        },
        context,
        native_cursor="jsonl:1411",
        timestamp=1,
    )
    with pytest.raises(CodexMappingError) as caught:
        adapter.map_protocol_message(
            {"id": "approval-invalid-amendment", "result": {"decision": decision}},
            context,
            native_cursor="jsonl:1412",
            timestamp=2,
        )
    assert caught.value.code == "invalid_interaction_response"


def test_multiple_hitl_resolutions_resume_run_before_next_interruption() -> None:
    """A turn can legally pause more than once without corrupting reducer run order."""

    def approval(request_id: str, item_id: str) -> dict[str, Any]:
        return {
            "id": request_id,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-hitl",
                "itemId": item_id,
                "startedAtMs": 1,
                "command": "echo ok",
                "cwd": "/tmp",
                "commandActions": [],
            },
        }

    mapped = _map_all(
        (
            (
                _turn_wire(
                    TurnStartedServerNotification, "turn/started", "turn-hitl", "inProgress"
                ),
                "jsonl:1501",
                1,
            ),
            (approval("approval-1", "cmd-1"), "jsonl:1502", 2),
            ({"id": "approval-1", "result": {"decision": "decline"}}, "jsonl:1503", 3),
            (approval("approval-2", "cmd-2"), "jsonl:1504", 4),
            ({"id": "approval-2", "result": {"decision": "cancel"}}, "jsonl:1505", 5),
        )
    )
    interactions = [
        event for event in mapped if isinstance(event, (InteractionRequested, InteractionResolved))
    ]
    assert len({event.interaction_id for event in interactions}) == 2
    assert [
        type(event)
        for event in mapped
        if isinstance(event, (InteractionResolved, RunProgress, RunInterrupted))
    ] == [
        RunInterrupted,
        InteractionResolved,
        RunProgress,
        RunInterrupted,
        InteractionResolved,
    ]

    reducer = StreamReducer()
    for event in mapped:
        reducer.apply(event)
    assert reducer.snapshot().status == "interrupted"


def test_server_request_resolved_closes_pending_interaction_without_resuming_run() -> None:
    """The authoritative cleanup notification cannot leave HITL state open forever."""

    adapter = CodexEventAdapter()
    context = _context()
    request = {
        "id": "approval-cleanup",
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "cmd-cleanup",
            "startedAtMs": 1,
            "command": "echo ok",
            "cwd": "/tmp",
            "commandActions": [],
        },
    }
    requested = adapter.map_protocol_message(
        request,
        context,
        native_cursor="jsonl:1511",
        timestamp=1,
    )
    resolved = adapter.map_protocol_message(
        {
            "method": "serverRequest/resolved",
            "params": {"threadId": "thread-1", "requestId": "approval-cleanup"},
        },
        context,
        native_cursor="jsonl:1512",
        timestamp=2,
    )

    assert any(isinstance(event, InteractionRequested) for event in requested)
    assert [type(event) for event in resolved] == [InteractionResolved]
    assert resolved[0].response.decision == "canceled"
    adapter.finish_stream()


@pytest.mark.parametrize(
    "request_frame",
    [
        {
            "id": "request-error",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "cmd-error",
                "startedAtMs": 1,
                "command": "echo ok",
                "cwd": "/tmp",
                "commandActions": [],
            },
        },
        {
            "id": "request-error",
            "method": "account/chatgptAuthTokens/refresh",
            "params": {"reason": "unauthorized", "previousAccountId": "org-123"},
        },
    ],
)
def test_jsonrpc_error_resolves_pending_interaction_without_persisting_error_data(
    request_frame: dict[str, Any],
) -> None:
    """A legal JSON-RPC error closes request state and redacts arbitrary error data."""

    adapter = CodexEventAdapter()
    context = _context()
    adapter.map_protocol_message(
        request_frame,
        context,
        native_cursor="jsonl:1515",
        timestamp=1,
    )
    resolved = adapter.map_protocol_message(
        {
            "id": "request-error",
            "error": {
                "code": -32001,
                "message": "request failed with must-not-message",
                "data": {"accessToken": "must-not-persist"},
            },
        },
        context,
        native_cursor="jsonl:1516",
        timestamp=2,
    )

    assert [type(event) for event in resolved] == [InteractionResolved]
    assert "must-not-persist" not in resolved[0].model_dump_json()
    assert "must-not-message" not in resolved[0].model_dump_json()
    adapter.finish_stream()


def test_thread_resume_jsonrpc_error_does_not_echo_sensitive_error_data() -> None:
    """Resume failures can be logged, so arbitrary JSON-RPC data must stay redacted."""

    adapter = CodexEventAdapter()
    context = _context()
    adapter.map_protocol_message(
        {"id": "resume-error", "method": "thread/resume", "params": {"threadId": "thread-1"}},
        context,
        native_cursor="jsonl:1517",
        timestamp=1,
    )
    with pytest.raises(CodexMappingError) as caught:
        adapter.map_protocol_message(
            {
                "id": "resume-error",
                "error": {
                    "code": -32001,
                    "message": "resume failed",
                    "data": {"accessToken": "must-not-echo"},
                },
            },
            context,
            native_cursor="jsonl:1518",
            timestamp=2,
        )
    assert caught.value.code == "thread_resume_failed"
    assert "must-not-echo" not in str(caught.value)

    corrected = adapter.map_protocol_message(
        {"id": "resume-error", "result": {}},
        context,
        native_cursor="jsonl:1518",
        timestamp=3,
    )
    assert corrected == ()
    adapter.map_protocol_message(
        _turn_wire(
            TurnStartedServerNotification,
            "turn/started",
            "turn-after-resume-error",
            "inProgress",
        ),
        context,
        native_cursor="jsonl:1519",
        timestamp=4,
    )
    adapter.map_protocol_message(
        _turn_wire(
            TurnCompletedServerNotification,
            "turn/completed",
            "turn-after-resume-error",
            "completed",
        ),
        context,
        native_cursor="jsonl:1520",
        timestamp=5,
    )
    adapter.finish_stream()


@pytest.mark.parametrize(
    ("method", "params", "result", "required_result_field"),
    [
        (
            "account/chatgptAuthTokens/refresh",
            {"reason": "unauthorized", "previousAccountId": "org-123"},
            {
                "accessToken": "access-token",
                "chatgptAccountId": "org-123",
                "chatgptPlanType": "business",
            },
            "accessToken",
        ),
        (
            "attestation/generate",
            {},
            {"token": "opaque-attestation-token"},
            "token",
        ),
    ],
)
def test_latest_control_server_requests_are_lossless_structured_interactions(
    method: str,
    params: dict[str, Any],
    result: dict[str, Any],
    required_result_field: str,
) -> None:
    """0.144.4 auth and attestation requests remain typed and response-correlated."""

    adapter = CodexEventAdapter()
    context = _context()
    requested = adapter.map_protocol_message(
        {"id": 91, "method": method, "params": params},
        context,
        native_cursor="jsonl:1521",
        timestamp=1,
    )
    resolved = adapter.map_protocol_message(
        {"id": 91, "result": result},
        context,
        native_cursor="jsonl:1522",
        timestamp=2,
    )

    assert [type(event) for event in requested] == [InteractionRequested]
    assert requested[0].interaction_kind == "structured_input"
    assert required_result_field in requested[0].request.schema_["required"]
    if method == "account/chatgptAuthTokens/refresh":
        assert "chatgptPlanType" not in requested[0].request.schema_["required"]
    assert [type(event) for event in resolved] == [InteractionResolved]
    serialized = resolved[0].model_dump_json()
    assert result[required_result_field] not in serialized
    assert resolved[0].response.data[f"{required_result_field}Present"] is True
    if method == "account/chatgptAuthTokens/refresh":
        assert resolved[0].response.data["chatgptAccountId"] == "org-123"
        assert resolved[0].response.data["chatgptPlanType"] == "business"
    adapter.finish_stream()


def test_native_mutation_replay_is_bounded_idempotent_and_detects_collision() -> None:
    """Replay identity is native cursor + full payload, never repeated text."""

    adapter = CodexEventAdapter()
    context = _context()
    start, start_cursor, start_time = _message_lifecycle()[0]
    delta, delta_cursor, delta_time = _message_lifecycle()[1]
    adapter.map_protocol_message(
        start,
        context,
        native_cursor=start_cursor,
        timestamp=start_time,
    )
    first = adapter.map_protocol_message(
        delta,
        context,
        native_cursor=delta_cursor,
        timestamp=delta_time,
    )
    duplicate = adapter.map_protocol_message(
        delta,
        context,
        native_cursor=delta_cursor,
        timestamp=delta_time,
    )
    repeated_text = adapter.map_protocol_message(
        delta,
        context,
        native_cursor="jsonl:1603",
        timestamp=delta_time + 1,
    )

    assert duplicate == ()
    assert first[0].event_id != repeated_text[0].event_id
    assert first[0].update == repeated_text[0].update
    with pytest.raises(CodexMappingError) as caught:
        adapter.map_protocol_message(
            {
                **delta,
                "params": {**delta["params"], "delta": "different payload"},
            },
            context,
            native_cursor=delta_cursor,
            timestamp=delta_time,
        )
    assert caught.value.code == "native_event_collision"

    for index in range(adapter.replay_window_limit + 5):
        adapter.map_protocol_message(
            delta,
            context,
            native_cursor=f"jsonl:bounded:{index}",
            timestamp=delta_time + index + 2,
        )
    assert adapter.replay_window_size == adapter.replay_window_limit


def test_replay_window_stores_only_fixed_length_payload_digests() -> None:
    """The bounded replay cache must not retain 1024 copies of source secrets."""

    adapter = CodexEventAdapter()
    context = _context()
    frame = _wire(
        ErrorServerNotification,
        {
            "method": "error",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-replay-secret",
                "error": {
                    "message": "replay-window-secret-12",
                    "additionalDetails": "replay-window-details-13",
                },
                "willRetry": True,
            },
        },
    )

    adapter.map_protocol_message(
        frame,
        context,
        native_cursor="jsonl:replay-secret",
        timestamp=1.0,
    )
    record = adapter._replay_window["jsonl:replay-secret"]

    assert "replay-window-secret-12" not in record.payload_digest
    assert "replay-window-details-13" not in record.payload_digest
    assert len(record.payload_digest) == 64
    assert int(record.payload_digest, 16) >= 0
    assert (
        adapter.map_protocol_message(
            frame,
            context,
            native_cursor="jsonl:replay-secret",
            timestamp=2.0,
        )
        == ()
    )
