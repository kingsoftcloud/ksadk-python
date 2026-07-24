# -*- coding: utf-8 -*-
"""A2UICore + RuntimeEvent adapter + conformance 的测试 (goal-13)。

核心验收:**A2UI 事件确实落在 RuntimeEvent store(而非独立通道/不经 A2A 绕过)**——
display/request_input/submit_action 产出的 a2ui.* 事件都能在 A7 RuntimeEventStore 中
按 cursor 读出,且经 goal-12 的共享 parser/replay 回放一致。
"""

from __future__ import annotations

import pytest

from ksadk.a2ui import A2UICore, A2UIValidationError, Component, Surface
from ksadk.events.replay import replay_transcript
from ksadk.events.runtime_event import EventType
from ksadk.events.store import RuntimeEventStore
from ksadk.sessions.in_memory import InMemorySessionService


async def _core():
    svc = InMemorySessionService()
    await svc.create_session(agent_id="a", user_id="u", session_id="s1")
    store = RuntimeEventStore(svc)
    core = A2UICore(store, agent_id="a", user_id="u", session_id="s1")
    return core, store


def _card_surface() -> Surface:
    return Surface.new(
        [
            Component(
                component_id="c1",
                type="Card",
                props={"title": "天气", "body": "北京晴"},
                children=[Component(component_id="t1", type="Text", props={"text": "详情"})],
            )
        ]
    )


def _form_surface() -> Surface:
    return Surface.new(
        [
            Component(
                component_id="f1",
                type="Form",
                props={"title": "确认", "fields": [{"name": "city"}], "submit_label": "提交"},
            )
        ]
    )


@pytest.mark.asyncio
async def test_display_ui_emits_surface_begin_then_update():
    core, store = await _core()
    surface = _card_surface()
    await core.display_ui(surface, invocation_id="inv1")
    await core.display_ui(surface, invocation_id="inv1")  # 重复显 → update
    events = await store.list("s1")
    types = [e.event_type for e in events]
    assert types == [EventType.A2UI_SURFACE_BEGIN, EventType.A2UI_SURFACE_UPDATE]
    assert all(e.payload["surface_id"] == surface.surface_id for e in events)
    assert events[0].payload["surface"]["components"][0]["type"] == "Card"


@pytest.mark.asyncio
async def test_request_ui_input_emits_interaction_and_returns_pending():
    core, store = await _core()
    interaction = await core.request_ui_input(
        _form_surface(),
        schema={"type": "object", "properties": {"city": {"type": "string"}}},
        kind="form",
        invocation_id="inv1",
    )
    assert interaction.status == "pending"
    assert interaction.kind == "form"
    # 事件:surface.begin(展示) + a2ui.interaction(请求输入)
    events = await store.list("s1")
    types = [e.event_type for e in events]
    assert EventType.A2UI_SURFACE_BEGIN in types
    assert EventType.A2UI_INTERACTION in types
    interaction_event = [e for e in events if e.event_type == EventType.A2UI_INTERACTION][0]
    assert interaction_event.payload["interaction_id"] == interaction.interaction_id
    assert interaction_event.payload["kind"] == "form"
    # pending 可查询
    assert core.pending_interaction(interaction.interaction_id) is interaction


@pytest.mark.asyncio
async def test_submit_action_emits_action_and_returns_receipt():
    core, store = await _core()
    await core.display_ui(_card_surface(), invocation_id="inv1")
    receipt = await core.submit_action(
        {"action_id": "act1", "surface_id": "surf_x", "name": "refresh", "actor": "user"},
        invocation_id="inv2",  # 非阻塞 action 可在另一 invocation(run 后)
    )
    assert receipt.status == "received"
    assert receipt.action_id == "act1"
    action_event = [e for e in await store.list("s1") if e.event_type == EventType.A2UI_ACTION][0]
    assert action_event.payload["action_id"] == "act1"
    assert action_event.payload["name"] == "refresh"
    assert action_event.invocation_id == "inv2"


@pytest.mark.asyncio
async def test_a2ui_events_land_in_runtime_event_store_not_bypassed():
    """conformance:A2UI 事件全部落在 RuntimeEvent store(可 list/subscribe/replay),非独立通道。"""
    core, store = await _core()
    surface = _card_surface()
    await core.display_ui(surface, invocation_id="inv1")
    await core.request_ui_input(_form_surface(), schema={}, kind="form", invocation_id="inv1")
    await core.submit_action(
        {"action_id": "a1", "surface_id": surface.surface_id, "name": "ok"}, invocation_id="inv2"
    )

    # 全部 a2ui.* 事件都能从 store 按 cursor 读出(证明经 RuntimeEvent,未绕过)。
    events = await store.list("s1")
    a2ui_types = {e.event_type for e in events if e.event_type.startswith("a2ui.")}
    assert {
        EventType.A2UI_SURFACE_BEGIN,
        EventType.A2UI_INTERACTION,
        EventType.A2UI_ACTION,
    } <= a2ui_types
    # 经 goal-12 共享 parser/replay 回放:a2ui 事件进入 extras(保序,不丢)。
    parser = await replay_transcript(store, "s1")
    extras_types = {x["event_type"] for x in parser.transcript()["extras"]}
    assert {
        EventType.A2UI_SURFACE_BEGIN,
        EventType.A2UI_INTERACTION,
        EventType.A2UI_ACTION,
    } <= extras_types


def test_unknown_component_type_safely_rejected():
    core_surface = Surface.new(
        [Component(component_id="x", type="EvilScript", props={"js": "alert(1)"})]
    )
    with pytest.raises(A2UIValidationError, match="未知组件类型"):
        core_surface.validate()


def test_component_rejects_executable_prop():
    comp = Component(component_id="x", type="Text", props={"text": lambda: "code"})
    with pytest.raises(A2UIValidationError, match="可执行代码"):
        comp.validate()


def test_pinned_a2ui_core_version():
    """版本约束(goal-13):a2ui-core==0.1.1 pinned,未漂移。"""
    from ksadk.a2ui.models import A2UI_CORE_VERSION

    assert A2UI_CORE_VERSION == "0.1.1"
