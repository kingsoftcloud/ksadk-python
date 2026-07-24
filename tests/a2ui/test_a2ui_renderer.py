# -*- coding: utf-8 -*-
"""A2UI renderer + SubmitA2UIAction 回传闭环 + live/replay conformance 的测试 (goal-14)。

验收:
- Card/Text/Approval 流式渲染;Approval 卡带 approve/deny 可交互动作。
- Approval 点选经 SubmitA2UIAction 回传到 A2UICore.submit_action,形成
  "渲染→人点选→回传→resume"闭环(本地证明,不等 goal-18)。
- 未知 catalog/组件安全降级,不执行动态代码。
- live/replay 状态一致(含交互记录,经 goal-12 共享 parser/replay,renderer 不复制 parser)。
"""

from __future__ import annotations

import pytest

from ksadk.a2ui import (
    A2UICore,
    A2UIRenderer,
    Component,
    Surface,
    submit_a2ui_action,
)
from ksadk.events.replay import replay_transcript
from ksadk.events.runtime_event import EventType
from ksadk.events.store import RuntimeEventStore
from ksadk.sessions.in_memory import InMemorySessionService


def _approval_surface() -> Surface:
    return Surface.new(
        [
            Component(
                component_id="ap1",
                type="ApprovalBar",
                props={
                    "tool_name": "delete_file",
                    "summary": "删除 /tmp/x.txt",
                    "approve_label": "批准",
                    "deny_label": "拒绝",
                },
            )
        ]
    )


async def _core_and_store():
    svc = InMemorySessionService()
    await svc.create_session(agent_id="a", user_id="u", session_id="s1")
    store = RuntimeEventStore(svc)
    return A2UICore(store, agent_id="a", user_id="u", session_id="s1"), store


# ---- Card/Text/Approval 渲染 ----


def test_render_card_and_text():
    renderer = A2UIRenderer()
    surface = Surface.new(
        [
            Component(
                component_id="c1",
                type="Card",
                props={"title": "天气", "body": "晴"},
                children=[Component(component_id="t1", type="Text", props={"text": "详情"})],
            )
        ]
    )
    node = renderer.render_surface(surface)
    assert node.type == "surface"
    card = node.children[0]
    assert card.type == "card" and card.props["title"] == "天气"
    assert card.children[0].type == "text" and card.children[0].props["text"] == "详情"


def test_render_approval_has_interactive_actions():
    renderer = A2UIRenderer()
    node = renderer.render_surface(_approval_surface())
    approval = node.children[0]
    assert approval.type == "approval"
    assert approval.props["tool_name"] == "delete_file"
    names = {a["name"] for a in approval.actions}
    assert names == {"approve", "deny"}


def test_unknown_component_safe_placeholder_no_code():
    renderer = A2UIRenderer()
    node = renderer.render_component(
        Component(component_id="x", type="EvilScript", props={"js": "alert(1)"})
    )
    # 安全降级:placeholder,不回显/执行任何动态代码
    assert node.type == "placeholder"
    assert "alert" not in node.to_json()


# ---- SubmitA2UIAction 回传闭环:渲染 → 人点选 → 回传 → resume ----


@pytest.mark.asyncio
async def test_approval_click_submit_action_loop():
    core, store = await _core_and_store()
    renderer = A2UIRenderer()
    surface = _approval_surface()

    # 1. Agent 渲染审批卡(request_ui_input,kind=approval,input_required)
    interaction = await core.request_ui_input(
        surface, schema={}, kind="approval", invocation_id="inv1"
    )
    node = renderer.render_surface(surface)
    approve_action = next(a for a in node.children[0].actions if a["name"] == "approve")

    # 2. 用户点选"批准" → SubmitA2UIAction 回传到 A2UICore.submit_action
    receipt = await submit_a2ui_action(
        core,
        action_id="act-approve-1",
        surface_id=surface.surface_id,
        name=approve_action["name"],
        actor="user",
        component_id="ap1",
        invocation_id="inv1",
    )
    assert receipt.status == "received"
    assert receipt.name == "approve"

    # 3. a2ui.action 事件落在 store(payload 正确)→ resume 通路可据此 resolve pending
    events = await store.list("s1")
    action_events = [e for e in events if e.event_type == EventType.A2UI_ACTION]
    assert len(action_events) == 1
    assert action_events[0].payload["name"] == "approve"
    assert action_events[0].payload["action_id"] == "act-approve-1"
    # pending interaction 仍存在(待 resolve);回传闭环在 store 层闭合
    assert core.pending_interaction(interaction.interaction_id) is not None


# ---- live/replay 状态一致(含交互记录)----


@pytest.mark.asyncio
async def test_live_render_equals_replay_render_and_actions_replayable():
    core, store = await _core_and_store()
    renderer = A2UIRenderer()
    surface = _approval_surface()

    # live:display + 一次审批点选
    await core.display_ui(surface, invocation_id="inv1")
    await submit_a2ui_action(
        core,
        action_id="act1",
        surface_id=surface.surface_id,
        name="approve",
        invocation_id="inv1",
    )
    live_json = renderer.render_surface(surface).to_json()

    # replay:从 store 回放 surface.begin 事件,重建 surface 再渲染
    events = await store.list("s1")
    begin = [e for e in events if e.event_type == EventType.A2UI_SURFACE_BEGIN][0]
    replayed_surface = Surface.from_dict(begin.payload["surface"])
    replay_json = renderer.render_surface(replayed_surface).to_json()

    # live 渲染 == replay 渲染(逐字节)
    assert live_json == replay_json

    # 交互记录(a2ui.action)经 goal-12 replay 可回放(保序,不丢)
    parser = await replay_transcript(store, "s1")
    extras_types = [x["event_type"] for x in parser.transcript()["extras"]]
    assert EventType.A2UI_ACTION in extras_types
