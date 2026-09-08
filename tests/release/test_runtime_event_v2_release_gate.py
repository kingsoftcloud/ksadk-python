"""RuntimeEvent schema v2 release gate (0.8.1 candidate).

This module is the executable 0.8.1 release gate for the canonical
``RuntimeEvent(schema_version=2)`` switch.  Every check is behavior-level: it
drives the real adapters, store, replay, projection, and HTTP routes and then
asserts on observed output.  It never greps source text.

Covered contracts (plan Task 10, step 1):

- AgentEngine's Responses accumulation consumes ksadk's SSE stream correctly.
- A raw v1 ``text.completed`` wire event is never a Responses output delta.
- ``RunAgent(Stream=true)`` stays ``text/event-stream`` with a stable envelope.
- The legacy session event view keeps stable fields and adds ``Metadata.RuntimeItem``.
- ``run_checkpoint`` listing includes only graph checkpoints, with exact Total.
- Mixed-schema old/new runs both stay visible in the legacy read view.
- An active v1-only run cannot resume as a canonical run (HTTP 409 semantics).
- The documented v2 capability surface matches the code it describes.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator
from typing import get_args

import httpx
import pytest

from ksadk.events.canonical import (
    ContinuationCreated,
    EventEnvelope,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCompleted,
    RunStarted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.canonical_replay import (
    LegacyRunNotResumableError,
    ensure_canonical_resume_allowed,
    list_legacy_session_events,
)
from ksadk.events.canonical_store import RuntimeEventStore, runtime_event_to_session_event
from ksadk.events.content import ContentSnapshot, TextContent
from ksadk.events.v1_compat import (
    EventTypeV1,
    RuntimeEventV1,
    RuntimeEventV1ProjectionMode,
    project_to_v1,
)
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
from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService

# ---------------------------------------------------------------------------
# AgentEngine Responses accumulation contract mirror
# ---------------------------------------------------------------------------
#
# ``_agentengine_update_stream_text`` is a faithful behavioral mirror of
# AgentEngine server's
# ``app/services/conversation_runtime_service.py::ConversationRuntimeService._update_stream_text``
# (the consumer of ksadk's ``RunAgent``/``/v1/responses`` SSE stream).  The
# server is a separate repository and is not importable from ksadk's test
# environment, so the release gate pins the accumulation contract here and the
# server's own read-only contract suite is run alongside it (see the candidate
# report).  Keep this mirror byte-for-byte aligned with the server parser.


def _agentengine_update_stream_text(
    *, accumulated_text: str, chunk: str, api_format: str
) -> str:
    next_text = accumulated_text
    current_event = ""
    for line in chunk.splitlines():
        if line.startswith("event: "):
            current_event = line[7:].strip()
            continue
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        event_type = str(data.get("type") or current_event or "")
        if event_type.startswith("response."):
            if event_type in {
                "response.reasoning.delta",
                "response.reasoning_text.delta",
                "response.reasoning_summary.delta",
                "response.reasoning_summary_text.delta",
            }:
                continue
            delta = data.get("delta")
            if delta is None:
                delta = data.get("text")
            if delta and event_type in {
                "response.output_text.delta",
                "response.content_part.delta",
            }:
                next_text += str(delta)
                continue
            output_text = data.get("output_text")
            if output_text:
                output_text = str(output_text)
                if not next_text or output_text.startswith(next_text):
                    next_text = output_text
                elif event_type == "response.completed" and next_text not in output_text:
                    next_text = output_text
                continue
        if api_format == "responses":
            if event_type in {
                "response.reasoning.delta",
                "response.reasoning_text.delta",
                "response.reasoning_summary.delta",
                "response.reasoning_summary_text.delta",
            }:
                continue
            delta = data.get("delta")
            if delta is None:
                delta = data.get("text")
            if delta and event_type in {
                "response.output_text.delta",
                "response.content_part.delta",
            }:
                next_text += str(delta)
                continue
            output_text = data.get("output_text")
            if output_text:
                output_text = str(output_text)
                if not next_text or output_text.startswith(next_text):
                    next_text = output_text
                elif event_type == "response.completed" and next_text not in output_text:
                    next_text = output_text
            continue
        choices = data.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content:
            next_text += str(content)
    return next_text


# ---------------------------------------------------------------------------
# Fixture runtime adapter (commentary + selected final answer, with usage)
# ---------------------------------------------------------------------------


class _Runtime(BaseRuntime):
    runtime_type = "fixture"

    def native_capabilities(self) -> dict[str, object]:
        return {}


class _FixtureAdapter(RuntimeAdapter):
    """Emit a commentary item and a selected final-answer item plus usage."""

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
            "schema_version": 2,
            "timestamp": 1.0,
            "run_id": handle.run_id,
            "scope_id": "scope-1",
            "source": SourceRef(framework="ksadk"),
        }
        yield RunStarted(event_id="run-started", seq=1, status="running", **common)
        yield ItemStarted(
            event_id="commentary-started",
            seq=2,
            item_id="commentary",
            item_kind="message",
            phase="commentary",
            **common,
        )
        commentary = TextContent(part_id="text-0", text="commentary")
        yield ItemUpdated(
            event_id="commentary-updated",
            seq=3,
            item_id="commentary",
            item_kind="message",
            op="append",
            update=commentary,
            **common,
        )
        yield ItemCompleted(
            event_id="commentary-completed",
            seq=4,
            item_id="commentary",
            item_kind="message",
            snapshot=ContentSnapshot(parts=(commentary,)),
            **common,
        )
        yield ItemStarted(
            event_id="selected-started",
            seq=5,
            item_id="selected",
            item_kind="message",
            phase="final_answer",
            **common,
        )
        selected = TextContent(part_id="text-0", text="selected answer")
        yield ItemUpdated(
            event_id="selected-updated",
            seq=6,
            item_id="selected",
            item_kind="message",
            op="append",
            update=selected,
            **common,
        )
        yield ItemCompleted(
            event_id="selected-completed",
            seq=7,
            item_id="selected",
            item_kind="message",
            snapshot=ContentSnapshot(parts=(selected,)),
            **common,
        )
        yield RunCompleted(
            event_id="run-completed",
            seq=8,
            status="completed",
            output_refs=(OutputRef(scope_id="scope-1", item_id="selected", part_id="text-0"),),
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


def _build_app(adapter: RuntimeAdapter, service: InMemorySessionService, *groups: str):
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _context: adapter)
    context = RuntimeLaunchContext(
        runtime_type="fixture",
        project_dir=".",
        detection=type("Detection", (), {"name": "demo-agent"})(),
    )
    return create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=RuntimeExecutor(registry),
            launch_context=context,
            route_groups=set(groups),
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )


def _decode_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    current_event = "message"
    for line in body.splitlines():
        if line.startswith("event: "):
            current_event = line.removeprefix("event: ").strip() or "message"
        elif line.startswith("data: "):
            payload = line.removeprefix("data: ")
            if payload.strip() and payload.strip() != "[DONE]":
                events.append((current_event, json.loads(payload)))
    return events


def _envelope(seq: int, *, event_id: str, run_id: str = "run-v2") -> dict:
    return {
        "schema_version": 2,
        "event_id": event_id,
        "seq": seq,
        "timestamp": float(seq),
        "run_id": run_id,
        "scope_id": "scope-v2",
        "source": SourceRef(framework="adk", native_event_id=event_id),
    }


def _event_text(payload: dict) -> str:
    return "".join(
        str(part.get("text") or "")
        for part in (payload.get("Content") or {}).get("parts") or []
        if isinstance(part, dict)
    )


# ---------------------------------------------------------------------------
# 1-2. AgentEngine Responses accumulation contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_responses_deltas_accumulate_through_agentengine_stream_text_contract():
    from ksadk.conversations.runtime_streaming import (
        stream_runtime_responses_conversation_turn,
    )

    service = InMemorySessionService()
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _context: _FixtureAdapter())
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

    accumulated = ""
    for chunk in chunks:
        accumulated = _agentengine_update_stream_text(
            accumulated_text=accumulated, chunk=chunk, api_format="responses"
        )

    # Only the run.completed output_refs-selected final answer accumulates; the
    # commentary lane must never leak into the visible Responses output.
    assert accumulated == "selected answer"


def test_raw_v1_text_completed_is_not_a_responses_output_delta():
    v1_event = RuntimeEventV1.create(
        EventTypeV1.TEXT_COMPLETED,
        agent_id="agent-1",
        user_id="user-1",
        session_id="sess-1",
        invocation_id="run-1",
        seq_id=3,
        payload={"text": "must not leak"},
        phase="final_answer",
    )
    # Realistic v1 wire form: nested payload + an SSE event field.
    chunk = f"event: text.completed\ndata: {v1_event.to_json()}\n\n"
    assert (
        _agentengine_update_stream_text(
            accumulated_text="", chunk=chunk, api_format="responses"
        )
        == ""
    )

    # A defensively flattened shape still must not accumulate as an output delta.
    flattened_payload = json.dumps({"type": "text.completed", "text": "must not leak"})
    flattened = f"event: text.completed\ndata: {flattened_payload}\n\n"
    assert (
        _agentengine_update_stream_text(
            accumulated_text="selected answer", chunk=flattened, api_format="responses"
        )
        == "selected answer"
    )


# ---------------------------------------------------------------------------
# 3. RunAgent(Stream=true) stays text/event-stream with a stable envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_stream_true_stays_text_event_stream_with_stable_envelope():
    service = InMemorySessionService()
    app = _build_app(_FixtureAdapter(), service, "run", "sessions", "openai_compat")

    transport = httpx.ASGITransport(app=app)
    body = ""
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        async with client.stream(
            "POST",
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-gate-stream",
                "Messages": [{"role": "user", "content": "hi"}],
                "ApiFormat": "responses",
                "Stream": True,
            },
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            async for line in response.aiter_lines():
                body += line + "\n"

    events = _decode_sse(body)
    by_name: dict[str, list[dict]] = {}
    for name, data in events:
        by_name.setdefault(name, []).append(data)

    # The Responses envelope types each frame through the SSE ``event:`` field
    # (AgentEngine's parser reads ``data.get("type") or current_event``).
    assert events, "expected at least one SSE frame"
    assert all(name.startswith("response.") for name, _data in events)
    # Stable envelope landmarks for a Responses stream.
    assert "response.created" in by_name
    deltas = [data["delta"] for data in by_name.get("response.output_text.delta", [])]
    assert deltas == ["selected answer"]
    completed = by_name["response.completed"][-1]
    assert completed["output_text"] == "selected answer"


# ---------------------------------------------------------------------------
# 4. Legacy session event view: stable fields + additive Metadata.RuntimeItem
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_session_events_keep_stable_fields_with_additive_runtime_item():
    service = InMemorySessionService()
    await service.create_session(agent_id="agent-1", user_id="user-1", session_id="sess-fields")
    store = RuntimeEventStore(service)
    for event in (
        RunStarted(**_envelope(1, event_id="run-start"), status="running"),
        ItemStarted(
            **_envelope(2, event_id="item-start"),
            item_id="item-1",
            item_kind="message",
            phase="final_answer",
        ),
        ItemCompleted(
            **_envelope(3, event_id="item-complete"),
            item_id="item-1",
            item_kind="message",
            snapshot=ContentSnapshot(parts=(TextContent(part_id="text-0", text="answer"),)),
        ),
        RunCompleted(
            **_envelope(4, event_id="run-complete"),
            status="completed",
            output_refs=(OutputRef(scope_id="scope-v2", item_id="item-1", part_id="text-0"),),
        ),
    ):
        await store.append_one("sess-fields", event)

    events = await list_legacy_session_events(store, "sess-fields", session_service=service)

    # Stable ListSessionEvents public fields on every projected row.
    for payload in events:
        for field in (
            "EventId",
            "SessionId",
            "Author",
            "EventType",
            "Content",
            "Timestamp",
            "SeqId",
            "Metadata",
        ):
            assert field in payload, f"missing stable field {field!r}: {payload!r}"

    assistant = [
        payload for payload in events if payload["EventType"] == "assistant_message"
    ]
    assert len(assistant) == 1
    metadata = assistant[0]["Metadata"]
    # Additive RuntimeItem identity, alongside the preserved v1 metadata keys.
    assert metadata["RuntimeItem"] == {
        "RunId": "run-v2",
        "ScopeId": "scope-v2",
        "ItemId": "item-1",
        "PartId": "text-0",
        "Operation": "replace",
        "SourceEventId": "run-complete",
    }
    assert metadata["schema_version"] == 1
    assert "RuntimeEventV1" in metadata


# ---------------------------------------------------------------------------
# 5. run_checkpoint listing: only graph checkpoints, exact Total / pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_checkpoint_listing_includes_only_graph_checkpoints_with_exact_total():
    service = InMemorySessionService()
    await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-ckpt")
    store = RuntimeEventStore(service)
    langgraph_capability = {"backend": "postgres", "scope": "shared", "durable": True}
    # A canonical graph checkpoint is listed; a non-graph continuation is not.
    await store.append_one(
        "sess-ckpt",
        ContinuationCreated(
            **{
                **_envelope(1, event_id="graph-ckpt"),
                "source": SourceRef(
                    framework="langgraph",
                    native_event_id="graph-ckpt",
                    metadata={"capability": langgraph_capability},
                ),
            },
            continuation_id="ckpt-1",
            continuation_kind="graph_checkpoint",
            resumable=True,
            ref={
                "thread_id": "sess-ckpt",
                "checkpoint_id": "ckpt-1",
                "next_node": "stage",
                "granularity": "snapshot",
            },
        ),
    )
    await store.append_one(
        "sess-ckpt",
        ContinuationCreated(
            **{
                **_envelope(2, event_id="invocation-resume"),
                "source": SourceRef(framework="adk", native_event_id="invocation-resume"),
            },
            continuation_id="inv-1",
            continuation_kind="invocation_resume",
            resumable=True,
            ref={"invocation_id": "inv-1"},
        ),
    )

    app = _build_app(_FixtureAdapter(), service, "sessions")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        full = await client.post(
            "/agentengine/api/v1/ListSessionCheckpoints",
            json={"AgentId": "demo-agent", "SessionId": "sess-ckpt", "UserId": "user-1"},
        )
        paged = await client.post(
            "/agentengine/api/v1/ListSessionCheckpoints",
            json={
                "AgentId": "demo-agent",
                "SessionId": "sess-ckpt",
                "UserId": "user-1",
                "Offset": 0,
                "Limit": 1,
            },
        )

    assert full.status_code == 200
    data = full.json()["Data"]
    # Only the graph checkpoint is projected; the invocation_resume continuation
    # must not surface as a run_checkpoint.
    assert data["Total"] == 1
    assert [item["CheckpointId"] for item in data["Checkpoints"]] == ["ckpt-1"]
    assert data["Checkpoints"][0]["Framework"] == "langgraph"

    assert paged.status_code == 200
    page = paged.json()["Data"]
    assert page["Total"] == 1
    assert page["Offset"] == 0
    assert page["Limit"] == 1
    assert [item["CheckpointId"] for item in page["Checkpoints"]] == ["ckpt-1"]


# ---------------------------------------------------------------------------
# 6. Mixed-schema old/new runs both stay visible in the legacy read view
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_schema_old_and_new_runs_remain_visible_in_legacy_view():
    service = InMemorySessionService()
    await service.create_session(agent_id="agent-1", user_id="user-1", session_id="sess-mixed")
    await service.append_event(
        "sess-mixed",
        SessionEvent(
            id="legacy-answer",
            session_id="sess-mixed",
            author="assistant",
            event_type="assistant_message",
            content={"role": "model", "parts": [{"text": "old answer"}]},
            invocation_id="run-v1-finished",
            metadata={"schema_version": 1},
        ),
    )
    store = RuntimeEventStore(service)
    for event in (
        RunStarted(**_envelope(1, event_id="run-start"), status="running"),
        ItemStarted(
            **_envelope(2, event_id="item-start"),
            item_id="item-1",
            item_kind="message",
            phase="final_answer",
        ),
        ItemCompleted(
            **_envelope(3, event_id="item-complete"),
            item_id="item-1",
            item_kind="message",
            snapshot=ContentSnapshot(parts=(TextContent(part_id="text-0", text="new answer"),)),
        ),
        RunCompleted(
            **_envelope(4, event_id="run-complete"),
            status="completed",
            output_refs=(OutputRef(scope_id="scope-v2", item_id="item-1", part_id="text-0"),),
        ),
    ):
        await store.append_one("sess-mixed", event)

    events = await list_legacy_session_events(store, "sess-mixed", session_service=service)
    assistant = [
        payload for payload in events if payload["EventType"] == "assistant_message"
    ]

    # The historical v1 row and the new canonical run both stay visible, in the
    # shared physical seq order (old before new).
    assert [_event_text(payload) for payload in assistant] == ["old answer", "new answer"]
    assert [payload["SeqId"] for payload in events] == sorted(
        payload["SeqId"] for payload in events
    )


# ---------------------------------------------------------------------------
# 7. Active v1-only run cannot resume as a canonical run (409 semantics)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_v1_run_resume_is_rejected_with_409():
    service = InMemorySessionService()
    await service.create_session(agent_id="agent-1", user_id="user-1", session_id="sess-resume")
    await service.append_event(
        "sess-resume",
        SessionEvent(
            id="legacy-active",
            session_id="sess-resume",
            author="assistant",
            event_type="run_status",
            content={"status": "in_progress"},
            invocation_id="run-v1-active",
            metadata={"schema_version": 1, "ksadk_runtime_event": True},
        ),
    )
    store = RuntimeEventStore(service)

    with pytest.raises(LegacyRunNotResumableError) as caught:
        await ensure_canonical_resume_allowed(
            store,
            "sess-resume",
            "run-v1-active",
            session_service=service,
        )

    assert caught.value.status_code == 409
    assert caught.value.code == "legacy_run_not_resumable"


# ---------------------------------------------------------------------------
# 8. Documented v2 capability surface matches the code it describes
# ---------------------------------------------------------------------------


def test_runtime_event_v2_capability_surface_matches_code():
    # RuntimeEventVersions = [1, 2]: the frozen v1 envelope (read path) and the
    # canonical v2 envelope are both real, distinct schema versions.
    assert get_args(RuntimeEventV1.model_fields["schema_version"].annotation) == (1,)
    assert get_args(EventEnvelope.model_fields["schema_version"].annotation) == (2,)

    # RuntimeEventDefault = 2: the canonical store only accepts schema v2 writes.
    class _V1Only:
        schema_version = 1

    with pytest.raises(ValueError, match="schema_version=2 only"):
        runtime_event_to_session_event("sess-1", _V1Only())
    accepted = runtime_event_to_session_event(
        "sess-1", RunStarted(**_envelope(1, event_id="run-start"), status="running")
    )
    assert accepted.metadata["schema_version"] == 2

    # RuntimeEventV1ProjectionModes / RuntimeEventV1ProjectionDefault.
    assert set(get_args(RuntimeEventV1ProjectionMode)) == {"snapshot_only", "identity_replace"}
    assert inspect.signature(project_to_v1).parameters["mode"].default == "snapshot_only"
