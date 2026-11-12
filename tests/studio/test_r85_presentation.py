import pytest

from ksadk.studio.contracts import BuildRecord, BuildStatus, RunRecord
from ksadk.studio.errors import StudioError
from ksadk.studio.event_store import RunEventStore
from ksadk.studio.presentation import StudioConversationProjection
from ksadk.studio.run_service import StudioRunService, project_runtime_event
from ksadk.studio.workspace import Workspace
from tests.events.test_managed_scope_reducer import events


@pytest.mark.asyncio
async def test_persisted_profile_replay_matches_live_and_rejects_switch(tmp_path):
    store = RunEventStore(Workspace(tmp_path))
    record = RunRecord(
        id="public-run",
        build_id="build",
        agent_id="agent",
        session_id="session",
        trace_id="trace",
        input="test",
        presentation_profile="agent-block-v1",
    )
    store.create(record)
    live = StudioConversationProjection("session", "agent-block-v1")
    expected = []
    for event in events():
        kind, data = project_runtime_event(event, session_id="session", public_run_id=record.id)
        expected.append(live.apply(store.append(record.id, kind, data)))
    # Re-open the actual durable store with no live reducer state.
    service = object.__new__(StudioRunService)
    service.event_store = RunEventStore(Workspace(tmp_path))
    assert (
        await service.events(record.id, after=13, presentation_profile="agent-block-v1")
        == expected[13:]
    )
    agent = next(
        e.data["conversationItem"]
        for e in expected
        if e.data["conversationItem"]["kind"] == "agent"
    )
    assert agent["runId"] == record.id
    with pytest.raises(StudioError, match="重连"):
        await service.events(record.id, presentation_profile="flat-v1")


def test_legacy_run_records_default_to_flat():
    record = RunRecord(
        id="r", build_id="b", agent_id="a", session_id="s", trace_id="t", input="test"
    )
    assert record.presentation_profile == "flat-v1"


def test_adk_conversation_api_negotiates_and_persists_profile(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from ksadk.runtime import RuntimeLaunchContext
    from ksadk.studio.api import create_studio_app
    from ksadk.studio.run_service import StudioRunSpec
    from ksadk.studio.service import StudioService
    from tests.studio.runtime_adapter_fixtures import RuntimeFixture
    from tests.studio.test_api import FakeModelClient

    async def native_events(request, handle):
        for event in events():
            yield event

    fixture = RuntimeFixture(native_events, runtime_types=("adk",))
    service = StudioService(
        tmp_path, model_client=FakeModelClient(), runtime_executor=fixture.executor
    )
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="adk", project_dir=tmp_path),
        build_id="build",
        agent_id="agent",
    )
    # The product now validates remote bindings against the Build before
    # resolving this scripted runtime. This presentation-only fixture has
    # no archive or remote bindings, but still needs an actual Build record.
    service.builds.save(BuildRecord(
        id=spec.build_id, agent_id=spec.agent_id, source_revision=1,
        status=BuildStatus.SUCCEEDED, runtime_type="adk",
    ))
    monkeypatch.setattr(service, "resolve_run_spec", lambda *args, **kwargs: spec)
    with TestClient(create_studio_app(tmp_path, service=service, security_enabled=False)) as client:
        surface = client.get(
            "/api/v1/builds/build/conversation-surface", params={"sessionId": "session"}
        )
        assert surface.status_code == 200
        assert any(c["name"] == "agent.block" for c in surface.json()["outputs"])
        response = client.post(
            "/api/v1/builds/build/conversation:stream",
            headers={"Idempotency-Key": "profile-test"},
            json={
                "input": {
                    "inputId": "input",
                    "sessionId": "session",
                    "idempotencyKey": "profile-test",
                    "parts": [{"kind": "text", "text": "test"}],
                    "extensions": {"ksadk.presentation": {"profile": "agent-block-v1"}},
                }
            },
        )
        assert response.status_code == 200, response.text
        assert '"kind":"agent"' in response.text, response.text
        record = service.event_store.list_runs()[0]
        assert record.presentation_profile == "agent-block-v1"
        assert "presentation_profile" not in fixture.start_requests[0].config
        replay = client.get(
            f"/api/v1/runs/{record.id}/events",
            params={"after": 0, "presentationProfile": "agent-block-v1"},
        )
        assert replay.status_code == 200
        assert '"kind":"agent"' in replay.text
        conflict = client.get(
            f"/api/v1/runs/{record.id}/events", params={"presentationProfile": "flat-v1"}
        )
        assert conflict.status_code == 409


def assert_managed_input_live_and_cursor_replay(envelopes, interaction_id):
    from ksadk.studio.contracts import RunEvent
    from ksadk.studio.run_service import _studio_envelope_projection

    stream = []
    for envelope in envelopes:
        projected = _studio_envelope_projection(envelope)
        if projected is not None:
            kind, data = projected
            stream.append(
                RunEvent(id=envelope.seq, run_id=envelope.run_id or "run", type=kind, data=data)
            )
    roots = [event.data["runtimeEvent"] for event in stream
             if event.data.get("runtimeEvent", {}).get("event_type", "").startswith("run.")]
    assert len({event["scope_id"] for event in roots}) == 1, [
        (event["event_type"], event["scope_id"], event["run_id"]) for event in roots
    ]
    for profile in ("flat-v1", "agent-block-v1"):
        projection = StudioConversationProjection("s1", profile)
        output = [projection.apply(event) for event in stream]
        controls = [
            event.data["conversationItem"]
            for event in output
            if event.data.get("conversationItem", {}).get("payload", {}).get("interactionId")
            == interaction_id
        ]
        assert len(controls) == 2
        assert [item["lifecycle"] for item in controls] == ["pending", "completed"]
        assert [item["payload"]["revision"] for item in controls] == [1, 2]
        assert len({item["itemId"] for item in controls}) == 1
        assert all(item["visibility"] == "public" for item in controls)
        assert all(item["nativeRef"]["parentScopeId"] for item in controls)
        if profile == "agent-block-v1":
            assert controls[0]["parentItemId"] == controls[1]["parentItemId"]
        # Rebuild through the requested prefix with a new projection instance.
        cut = (
            next(
                index
                for index, event in enumerate(output)
                if event.data.get("conversationItem", {}).get("payload", {}).get("interactionId")
                == interaction_id
            )
            + 1
        )
        replay = StudioConversationProjection("s1", profile)
        for event in stream[:cut]:
            replay.apply(event)
        assert [replay.apply(event) for event in stream[cut:]] == output[cut:]
