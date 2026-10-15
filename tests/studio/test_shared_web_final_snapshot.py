"""Responses delivery must replace streamed previews with the committed answer."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ksadk.studio.contracts import RunRecord, RunStatus
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService
from ksadk.studio.shared_web import StudioSharedWebBridge


def _frame(frame):
    return json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", ["ACK", "unverified preview", ""])
async def test_committed_answer_has_explicit_done_after_any_preview(tmp_path, monkeypatch, preview):
    studio = StudioService(tmp_path)
    bridge = StudioSharedWebBridge(studio)
    release = asyncio.Event()
    record = RunRecord(
        id="canonical-run", build_id="b", agent_id="a", session_id="s",
        trace_id="t", input="ask", status=RunStatus.COMPLETED, output="ACK",
    )
    monkeypatch.setattr(bridge, "resolve_agent_id", lambda _: "a")
    monkeypatch.setattr(bridge, "_ensure_build", AsyncMock(return_value=SimpleNamespace(
        id="b", runtime_lock={"model": "test-model", "models": ["test-model"]},
    )))
    monkeypatch.setattr(bridge, "_validate_conversation_turn", lambda **_: None)
    # A document-linked answer must match response.completed exactly.
    monkeypatch.setattr(
        "ksadk.studio.shared_web.link_run_documents", lambda *_: "ACK [artifact](artifact:one)"
    )
    executions = []

    async def execute(**kwargs):
        executions.append(kwargs["invocation_id"])
        bridge._run_ids_by_invocation[kwargs["invocation_id"]] = record.id
        await release.wait()
        return record

    sent = False

    async def project(**_):
        nonlocal sent
        if sent:
            return []
        sent = True
        if not preview:
            return []
        return [("response.output_text.delta", bridge._response_delta_payload(
            "response.output_text.delta", invocation_id="invocation", delta=preview,
        ))]

    monkeypatch.setattr(bridge, "_execute_run", execute)
    monkeypatch.setattr(bridge, "_project_response_events", project)
    stream = bridge.stream_run({
        "AgentId": "a", "SessionId": "s", "InvocationId": "invocation",
        "Messages": [{"role": "user", "content": "ask"}],
    }, shared_ui=True)
    try:
        assert _frame(await anext(stream))["type"] == "response.created"
        assert _frame(await anext(stream))["type"] == "response.in_progress"
        if preview:
            assert _frame(await anext(stream))["delta"] == preview
        release.set()
        frames = [_frame(frame) async for frame in stream if frame.startswith("event:")]
        done = [frame for frame in frames if frame["type"] == "response.output_text.done"]
        assert len(done) == 1
        completed = frames[-1]
        assert completed["type"] == "response.completed"
        assert done[0] == {
            "type": "response.output_text.done", "item_id": "msg_invocation",
            "output_index": 0, "content_index": 0,
            "text": completed["response"]["output"][0]["content"][0]["text"],
        }
        assert done[0]["text"] == "ACK [artifact](artifact:one)"
        assert executions == ["invocation"]
    finally:
        release.set()
        await stream.aclose()
        await studio.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["RUN_CANCELLED", "RUN_FAILED"])
async def test_unsuccessful_execution_does_not_publish_final_text(tmp_path, monkeypatch, code):
    studio = StudioService(tmp_path)
    bridge = StudioSharedWebBridge(studio)
    monkeypatch.setattr(bridge, "resolve_agent_id", lambda _: "a")
    monkeypatch.setattr(bridge, "_ensure_build", AsyncMock(return_value=SimpleNamespace(
        id="b", runtime_lock={"model": "test-model", "models": ["test-model"]},
    )))
    monkeypatch.setattr(bridge, "_validate_conversation_turn", lambda **_: None)

    async def execute(**kwargs):
        bridge._run_ids_by_invocation[kwargs["invocation_id"]] = "canonical-run"
        raise StudioError(code, "execution was not completed")

    monkeypatch.setattr(bridge, "_execute_run", execute)
    try:
        frames = [_frame(frame) async for frame in bridge.stream_run({
            "AgentId": "a", "SessionId": "s", "InvocationId": "invocation",
            "Messages": [{"role": "user", "content": "ask"}],
        }, shared_ui=True) if frame.startswith("event:")]
        assert frames[-1]["type"] == (
            "response.cancelled" if code == "RUN_CANCELLED" else "response.failed"
        )
        assert all(frame["type"] != "response.output_text.done" for frame in frames)
    finally:
        await studio.aclose()


@pytest.mark.asyncio
async def test_native_provisional_draft_is_not_promoted_to_responses_answer(tmp_path):
    from tests.studio.response_projection_fixtures import replay_responses

    def native(kind, **flags):
        return {"source": {"metadata": {"engine_kind": "native",
                                        "native_event_kind": kind, **flags}}}

    entries = [
        ("message.delta", {"text": "unfinished draft with code",
                           "runtimeEvent": native("text.delta", provisional=True)}),
        ("message.completed", {"text": "", "runtimeEvent": native(
            "effect.received", provisional=True, provisional_retired=True)}),
        ("message.delta", {"text": "FINAL_DELIVERY",
                           "runtimeEvent": native("text.completed")}),
    ]
    responses = await replay_responses(tmp_path, entries)
    output = [p["delta"] for k, p in responses if k == "response.output_text.delta"]
    assert output == ["FINAL_DELIVERY"]
    assert all("unfinished draft" not in str(p) for k, p in responses)


@pytest.mark.asyncio
async def test_native_progress_uses_process_channel_without_reclassifying_answer(tmp_path):
    from tests.studio.response_projection_fixtures import replay_responses

    responses = await replay_responses(tmp_path, [("message.delta", {
        "text": "Running workspace verification", "runtimeEvent": {"source": {
            "metadata": {"engine_kind": "native", "native_event_kind": "run.progress"}
        }}
    })])
    assert [k for k, p in responses] == ["response.reasoning_summary_text.delta"]


@pytest.mark.asyncio
async def test_sealed_tool_commentary_remains_visible_once(tmp_path):
    from tests.studio.response_projection_fixtures import replay_responses

    metadata = {"engine_kind": "native", "native_event_kind": "text.delta",
                "provisional": True}
    responses = await replay_responses(tmp_path, [
        ("message.delta", {"text": "Now running tests.", "runtimeEvent": {
            "source": {"metadata": metadata}}}),
        ("message.completed", {"text": "Now running tests.", "runtimeEvent": {
            "source": {"metadata": {**metadata, "native_event_kind": "effect.received"}}}}),
    ])
    assert [p["delta"] for k, p in responses if k == "response.output_text.delta"] == [
        "Now running tests."
    ]
