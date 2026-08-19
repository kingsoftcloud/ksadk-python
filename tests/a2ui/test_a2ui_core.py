# -*- coding: utf-8 -*-
"""A2UICore + RuntimeEvent adapter + conformance 的测试 (goal-13)。

核心验收:**A2UI 事件确实落在 RuntimeEvent store(而非独立通道/不经 A2A 绕过)**——
display/request_input/submit_action 产出的 a2ui.* 事件都能在 A7 RuntimeEventStore 中
按 cursor 读出,且经 goal-12 的共享 parser/replay 回放一致。
"""

from __future__ import annotations

import pytest

from ksadk.a2ui import A2UICore, A2UIValidationError, Component, Surface
from ksadk.events.canonical import InteractionRequested, ItemCompleted, ItemStarted, ItemUpdated
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
    # canonical: ItemStarted(item_kind="data") + ItemUpdated(item_kind="data")
    assert len(events) == 2
    assert isinstance(events[0], ItemStarted) and events[0].item_kind == "data"
    assert isinstance(events[1], ItemUpdated) and events[1].item_kind == "data"
    assert all(e.source.protocol == "a2ui" for e in events)
    assert all(e.source.metadata["surface_id"] == surface.surface_id for e in events)
    # surface data in DataContent
    surface_data = events[0].initial.parts[0].data
    assert surface_data["components"][0]["type"] == "Card"


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
    # 事件:surface.begin(展示) + interaction.requested(请求输入)
    events = await store.list("s1")
    # ItemStarted(item_kind="data") for surface display + InteractionRequested for input
    surface_events = [e for e in events if isinstance(e, ItemStarted) and e.item_kind == "data"]
    interaction_events = [e for e in events if isinstance(e, InteractionRequested)]
    assert len(surface_events) == 1
    assert len(interaction_events) == 1
    assert interaction_events[0].interaction_kind == "structured_input"
    assert interaction_events[0].interaction_id == interaction.interaction_id
    assert interaction_events[0].source.metadata["kind"] == "form"
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
    action_event = [
        e for e in await store.list("s1")
        if isinstance(e, InteractionRequested) and e.interaction_kind == "approval"
    ][0]
    assert action_event.interaction_id == "act1"
    assert action_event.request.detail["name"] == "refresh"
    assert action_event.run_id == "inv2"


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

    # 全部 A2UI 事件都能从 store 按 cursor 读出(证明经 RuntimeEvent,未绕过)。
    events = await store.list("s1")
    # canonical: surface = ItemStarted/Updated(item_kind="data", protocol="a2ui")
    #            interaction = InteractionRequested(structured_input/approval, protocol="a2ui")
    surface_events = [
        e for e in events
        if isinstance(e, (ItemStarted, ItemUpdated, ItemCompleted)) and e.item_kind == "data"
    ]
    interaction_events = [e for e in events if isinstance(e, InteractionRequested)]
    assert len(surface_events) >= 1
    assert len(interaction_events) >= 2  # request_ui_input + submit_action
    # surface events have protocol="a2ui"
    assert all(e.source.protocol == "a2ui" for e in surface_events)
    # interaction kinds
    interaction_kinds = {e.interaction_kind for e in interaction_events}
    assert "structured_input" in interaction_kinds
    assert "approval" in interaction_kinds


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


# ------------------------------------------------- Phase 1 Task 5 Step 6


async def _kernel_backed_core():
    """A2UICore + InMemoryAgentKernelStore ledger(durable interaction 权威)。"""
    from ksadk.events.session_event import SessionServiceEventStore
    from ksadk.kernel.contracts import ActivationWriteGuard
    from ksadk.kernel.memory_store import InMemoryAgentKernelStore
    from ksadk.kernel.store import ActivationLeaseRequest

    svc = InMemorySessionService()
    await svc.create_session(agent_id="a", user_id="u", session_id="s1")
    kernel = InMemoryAgentKernelStore(SessionServiceEventStore(svc))
    lease = await kernel.acquire_activation(
        ActivationLeaseRequest(
            agent_instance_id="a", session_id="s1", activation_id="act-a2ui"
        )
    )
    guard = ActivationWriteGuard(
        activation_id=lease.activation_id, fencing_token=lease.fencing_token
    )
    store = RuntimeEventStore(svc)
    core = A2UICore(
        store,
        agent_id="a",
        user_id="u",
        session_id="s1",
        interaction_ledger=kernel,
        interaction_guard=guard,
    )
    return core, store, kernel


@pytest.mark.asyncio
async def test_request_ui_input_delegates_to_ledger():
    core, store, kernel = await _kernel_backed_core()
    interaction = await core.request_ui_input(
        _form_surface(),
        schema={"type": "object"},
        kind="form",
        invocation_id="inv1",
    )
    record = await kernel.get(interaction.interaction_id)
    assert record is not None
    assert record.status == "pending"
    assert record.session_id == "s1"
    assert record.run_id == "inv1"
    assert [r.interaction_id for r in await kernel.list_pending_interactions("default", "s1")] == [
        interaction.interaction_id
    ]


@pytest.mark.asyncio
async def test_submit_action_resolves_original_interaction_without_second_requested():
    core, store, kernel = await _kernel_backed_core()
    interaction = await core.request_ui_input(
        _form_surface(), schema={}, kind="form", invocation_id="inv1"
    )
    receipt = await core.submit_action(
        {
            "action_id": "act1",
            "surface_id": interaction.surface_id,
            "name": "submit",
            "actor": "user",
            "interaction_id": interaction.interaction_id,
        },
        invocation_id="inv1",
    )
    assert receipt.status == "resolved"

    record = await kernel.get(interaction.interaction_id)
    assert record is not None and record.status == "resolved"
    assert record.revision == 2

    # durable 路径不再发第二个 InteractionRequested。
    requested = [
        e
        for e in await store.list("s1")
        if isinstance(e, InteractionRequested)
    ]
    assert len(requested) == 1  # 只有 request_ui_input 的那一条
    # pending 视图同步收口。
    assert core.pending_interaction(interaction.interaction_id).status == "resolved"


@pytest.mark.asyncio
async def test_submit_action_without_interaction_id_keeps_legacy_path():
    core, store, kernel = await _kernel_backed_core()
    await core.display_ui(_card_surface(), invocation_id="inv1")
    receipt = await core.submit_action(
        {"action_id": "act1", "surface_id": "surf_x", "name": "refresh"},
        invocation_id="inv2",
    )
    assert receipt.status == "received"
    action_events = [
        e for e in await store.list("s1") if isinstance(e, InteractionRequested)
    ]
    assert len(action_events) == 1
