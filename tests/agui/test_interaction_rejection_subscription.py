"""A rejected answer must reach the same legacy run subscription and remain correctable."""

import asyncio
import json

import pytest

from ksadk.kernel.state import RunState
from ksadk.runtime_context import PlatformIdentityContext
from ksadk.server.factory import bind_runtime_state
from ksadk.server.routes.control import subscribe_run_events_action
from ksadk.server.routes.dependencies import bind_session_service
from ksadk.server.routes.projection import _oldest_unconsumed_session_events
from tests.agui.test_default_local_interactions import ask, local_app
from tests.agui.test_kernel_native_input import body, until


async def test_rejected_answer_reaches_original_run_and_correction_preserves_idempotency(
    tmp_path,
    monkeypatch,
):
    before, tools = [], []
    async with local_app(tmp_path, monkeypatch, before, tools) as s:
        record = await ask(s)
        runtime = s.app.state.runtime.local_interactions.runtime
        invalid = body(record, "invalid-private-input", "bad-answer")
        accepted = await s.client.post("/agentengine/api/v1/SubmitInteraction", json=invalid)
        assert accepted.status_code == 202

        async def rejected():
            facts = await runtime.session_events.read("thread-1", 0, 500)
            return next((e for e in facts if e.event_type == "control.command_rejected"), None)

        await until(rejected)
        rejection = await rejected()
        assert rejection.run_id == record.run_id
        assert not tools and before == [1]
        assert (await runtime.kernel_store.get(record.interaction_id)).status == "pending"
        assert (await runtime.kernel_store.load_run(record.run_id)).state is RunState.WAITING

        history = await s.client.post(
            "/agentengine/api/v1/ListSessionEvents",
            json={
                "AgentId": "agent",
                "UserId": "user-1",
                "SessionId": "thread-1",
                "Limit": 500,
            },
        )
        event = next(
            e
            for e in history.json()["Data"]["Events"]
            if e["EventType"] == "control.command_rejected"
        )
        assert event["InvocationId"] == record.run_id
        assert event["Content"]["command_id"] == accepted.json()["Data"]["command_id"]
        assert event["Content"]["status"] == "rejected"
        assert event["Content"]["reason"] == "invalid_command"
        assert (
            event["Content"]["session_event"]["payload"]["command_id"]
            == event["Content"]["command_id"]
        )
        assert "invalid-private-input" not in json.dumps(event)

        duplicate = await s.client.post("/agentengine/api/v1/SubmitInteraction", json=invalid)
        assert duplicate.status_code == 200 and duplicate.json()["Data"]["status"] == "duplicate"
        conflict = await s.client.post(
            "/agentengine/api/v1/SubmitInteraction", json={**invalid, "Response": 0}
        )
        assert conflict.status_code == 409
        # Direct route calls do not pass through the app's HTTP context bridge.
        # Bind the same app-owned state/service for the stream's full lifetime.
        with bind_runtime_state(s.app.state.runtime), bind_session_service(s.service):
            subscription = await subscribe_run_events_action(
                AgentId="agent",
                UserId="user-1",
                SessionId="thread-1",
                InvocationId=record.run_id,
                AfterSeqId=rejection.seq - 1,
                invocation_identity=PlatformIdentityContext(),
            )
            try:
                # ASGITransport buffers a long-lived stream. Exercise the actual
                # authorized generator and require delivery before correcting input.
                async with asyncio.timeout(3):
                    frame = await anext(subscription.body_iterator)
                delivered = json.loads(frame.removeprefix("data: ").strip())
                assert delivered["EventType"] == "control.command_rejected"
                assert delivered["Content"]["command_id"] == event["Content"]["command_id"]
                corrected = await s.client.post(
                    "/agentengine/api/v1/SubmitInteraction", json=body(record, 0, "corrected-answer")
                )
                assert corrected.status_code == 202

                async def completed():
                    return (
                        await runtime.kernel_store.load_run(record.run_id)
                    ).state is RunState.COMPLETED

                await until(completed)
                facts = await runtime.session_events.read("thread-1", 0, 500)
                assert not any(e.event_type == "run.failed" for e in facts)
                assert tools == [0] and before == [1]
            finally:
                await subscription.body_iterator.aclose()


@pytest.mark.parametrize("mismatch", ["run", "session", "interaction"])
async def test_unverified_interaction_rejection_is_not_attributed_to_an_original_run(
    tmp_path,
    monkeypatch,
    mismatch,
):
    before, tools = [], []
    async with local_app(tmp_path, monkeypatch, before, tools) as s:
        record = await ask(s)
        runtime = s.app.state.runtime.local_interactions.runtime
        payload = body(record, "invalid", f"wrong-{mismatch}")
        if mismatch == "run":
            payload["RunId"] = "another-run"
        elif mismatch == "interaction":
            payload["InteractionId"] = "unknown-interaction"
        else:
            created = await s.client.post(
                "/agentengine/api/v1/CreateSession",
                json={
                    "AgentId": "agent",
                    "UserId": "user-1",
                    "SessionId": "other-session",
                },
            )
            assert created.status_code == 200
            payload["SessionId"] = "other-session"
        response = await s.client.post("/agentengine/api/v1/SubmitInteraction", json=payload)
        assert response.status_code == 202, response.text

        async def rejected():
            return [
                e
                for e in await runtime.session_events.read(payload["SessionId"], 0, 500)
                if e.event_type == "control.command_rejected"
            ]

        await until(rejected)
        assert all(e.run_id is None for e in await rejected())
        visible = await _oldest_unconsumed_session_events(
            s.service,
            "thread-1",
            after_seq_id=0,
            invocation_id=record.run_id,
        )
        assert not any(e.event_type == "control.command_rejected" for e in visible)
        assert not tools and before == [1]
        assert (await runtime.kernel_store.get(record.interaction_id)).status == "pending"
