"""Native graph UI remains visible without exposing internal or remote output."""

import pytest

from ksadk.a2ui.catalog import catalog_manifest
from ksadk.conversations.runtime_projection import RuntimeConversationProjection
from ksadk.events.canonical import ItemCompleted, ItemStarted, RunStarted, SourceRef
from ksadk.events.content import ContentSnapshot, DataContent, TextContent
from tests.events.test_managed_scope_reducer import events


def ui_events(*, framework="langgraph", parent="root", scope="nested", seq=2, metadata=None):
    source = SourceRef(framework=framework, protocol="a2ui", metadata={
        "surface_id": "input", **(metadata or {}),
    })
    common = dict(schema_version=2, timestamp=1000.0, run_id="run", scope_id=scope,
                  parent_scope_id=parent, source=source, item_id="ui", item_kind="data")
    snapshot = ContentSnapshot(parts=(DataContent(part_id="ui", data=[
        {"version": "v0.9.1", "createSurface": {
            "surfaceId": "input", "catalogId": catalog_manifest()["catalog_id"],
        }},
    ]),))
    return [ItemStarted(**common, seq=seq, event_id=f"{scope}-start", initial=snapshot),
            ItemCompleted(**common, seq=seq + 1, event_id=f"{scope}-complete", snapshot=snapshot)]


def root_event():
    return RunStarted(schema_version=2, timestamp=1000.0, run_id="run", scope_id="root",
                      source=SourceRef(framework="langgraph"), seq=1, event_id="root-start",
                      status="running")


@pytest.mark.parametrize("framework", ["langgraph", "adk", "codex"])
@pytest.mark.parametrize("profile", ["flat-v1", "agent-block-v1"])
def test_native_subgraph_ui_is_public_and_replay_keeps_its_identity(framework, profile):
    stream = [root_event(), *ui_events(framework=framework)]
    projector = RuntimeConversationProjection("session", profile=profile)
    projected = [projector.apply(event) for event in stream]
    card = projected[-1]
    assert card.kind == "a2ui" and card.visibility == "public"
    assert card.native_ref["scopeId"] == "nested"
    assert card.native_ref["parentScopeId"] == "root"
    assert projector.apply(stream[-1]) is None
    replay = RuntimeConversationProjection("session", profile=profile)
    for event in stream[:-1]:
        replay.apply(event)
    assert replay.apply(stream[-1]) == card


@pytest.mark.parametrize("metadata", [
    {"visibility": "internal"}, {"conversation_visibility": "hidden"},
])
def test_private_native_card_stays_hidden_in_flat_profile(metadata):
    projector = RuntimeConversationProjection("session", profile="flat-v1")
    projector.apply(root_event())
    for event in ui_events(metadata=metadata):
        item = projector.apply(event)
        assert item.visibility == "hidden"


def test_native_subgraph_intermediate_text_is_still_hidden():
    projector = RuntimeConversationProjection("session", profile="flat-v1")
    projector.apply(root_event())
    for event in ui_events():
        snapshot = ContentSnapshot(parts=(
            TextContent(part_id="text", text='{"intent":"execute"}'),
        ))
        update = {"item_kind": "message", "source": SourceRef(framework="langgraph")}
        update["initial" if isinstance(event, ItemStarted) else "snapshot"] = snapshot
        assert projector.apply(event.model_copy(update=update)).visibility == "hidden"


@pytest.mark.parametrize("framework", ["langgraph", "a2a", "ksadk"])
def test_remote_agent_ui_stays_hidden_in_flat_profile(framework):
    projector = RuntimeConversationProjection("session", profile="flat-v1")
    stream = events()
    for event in stream[:6]:
        projector.apply(event)
    for event in ui_events(
        framework=framework, parent="child-scope-1", scope="remote-node", seq=100,
    ):
        event = event.model_copy(update={"run_id": "root-run-1"})
        item = projector.apply(event)
        assert item.visibility == "hidden"
