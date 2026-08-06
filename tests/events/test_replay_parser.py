# -*- coding: utf-8 -*-
"""历史 replay + 共享 parser + projection conformance 的测试 (goal-12)。

核心 conformance:**同一事件流,live 增量渲染与 replay 历史回放逐字节一致**——
因为两者共用同一个 RuntimeEventParser(单实现),从根上防行为漂移(H2 高风险)。
"""

from __future__ import annotations

import pytest

from ksadk.events.parser import RuntimeEventParser
from ksadk.events.replay import replay_transcript
from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.events.store import RuntimeEventStore
from ksadk.sessions.in_memory import InMemorySessionService


def _ev(event_type, invocation_id, seq, payload=None, phase=None, session="s1"):
    return RuntimeEvent.create(
        event_type,
        agent_id="a",
        user_id="u",
        session_id=session,
        invocation_id=invocation_id,
        seq_id=seq,
        payload=payload or {},
        phase=phase,
    )


def _sample_stream() -> list[RuntimeEvent]:
    """一条覆盖 text/reasoning/tool/artifact/run 的事件流(跨 invocation)。"""
    return [
        _ev(EventType.RUN_STARTED, "inv1", 1, {"status": "in_progress"}),
        _ev(EventType.REASONING_DELTA, "inv1", 2, {"text": "想一下"}, phase="commentary"),
        _ev(EventType.TEXT_DELTA, "inv1", 3, {"text": "你"}, phase="final_answer"),
        _ev(EventType.TOOL_CALL_BEGIN, "inv1", 4, {"call_id": "c1", "name": "search"}),
        _ev(
            EventType.TOOL_CALL_END,
            "inv1",
            5,
            {"call_id": "c1", "name": "search", "result": {"hits": 2}},
        ),
        _ev(EventType.TEXT_COMPLETED, "inv1", 6, {"text": "好"}, phase="final_answer"),
        _ev(EventType.RUN_COMPLETED, "inv1", 7, {"status": "completed"}),
        _ev(EventType.RUN_STARTED, "inv2", 8, {"status": "in_progress"}),
        _ev(
            EventType.ARTIFACT_CREATED,
            "inv2",
            9,
            {"name": "report", "version": 1, "text": "v1 内容"},
        ),
        _ev(EventType.RUN_COMPLETED, "inv2", 10, {"status": "completed"}),
    ]


def _render_live(events: list[RuntimeEvent]) -> str:
    """live 渲染:事件边来边喂共享 parser,输出确定性 JSON。"""
    parser = RuntimeEventParser()
    for e in events:
        parser.feed(e)
    return parser.to_json()


@pytest.mark.asyncio
async def test_conformance_live_equals_replay_byte_identical():
    """conformance fixture:live 渲染 == replay 渲染(逐字节)。"""
    stream = _sample_stream()
    svc = InMemorySessionService()
    await svc.create_session(agent_id="a", user_id="u", session_id="s1")
    store = RuntimeEventStore(svc)
    await store.append(stream)

    live_json = _render_live(stream)
    replay_parser = await replay_transcript(store, "s1")
    replay_json = replay_parser.to_json()

    assert live_json == replay_json


@pytest.mark.asyncio
async def test_replay_cross_invocation_includes_all():
    """replay 跨 invocation:两个 invocation 的事件都进 transcript。"""
    stream = _sample_stream()
    svc = InMemorySessionService()
    await svc.create_session(agent_id="a", user_id="u", session_id="s1")
    store = RuntimeEventStore(svc)
    await store.append(stream)

    parser = await replay_transcript(store, "s1")
    transcript = parser.transcript()
    invocations = {
        item.get("invocation_id") for item in transcript["items"] if item.get("invocation_id")
    }
    assert {"inv1", "inv2"} <= invocations
    assert transcript["run_status"] == {"inv1": "completed", "inv2": "completed"}


def test_parser_folds_text_tool_artifact_run():
    """parser 折叠:text 按相位累积、tool 配对、artifact 版本、run 状态。"""
    parser = RuntimeEventParser()
    for e in _sample_stream():
        parser.feed(e)
    t = parser.transcript()
    # text final_answer 累积 "你"+"好"(inv1, completed)
    text_items = [i for i in t["items"] if i["kind"] == "text"]
    assert any(i["text"] == "你好" and i["final"] for i in text_items)
    # tool_call 配对 done + result
    tool = [i for i in t["items"] if i["kind"] == "tool_call"][0]
    assert tool["done"] and tool["result"] == {"hits": 2}
    # artifact
    art = [i for i in t["items"] if i["kind"] == "artifact"][0]
    assert art["name"] == "report" and art["text"] == "v1 内容"


def test_parser_json_is_deterministic():
    """同一事件流喂两遍,JSON 逐字节一致(确定性,conformance 前提)。"""
    stream = _sample_stream()
    assert _render_live(stream) == _render_live(stream)


@pytest.mark.asyncio
async def test_replay_with_cursor_bounds():
    """replay 支持 cursor 界:after/before seq 限定回放窗口。"""
    stream = _sample_stream()
    svc = InMemorySessionService()
    await svc.create_session(agent_id="a", user_id="u", session_id="s1")
    store = RuntimeEventStore(svc)
    await store.append(stream)
    # 只回放 inv2(after inv1 终态 seq=7)
    parser = await replay_transcript(store, "s1", after_seq_id=7)
    t = parser.transcript()
    invocations = {i.get("invocation_id") for i in t["items"] if i.get("invocation_id")}
    assert invocations == {"inv2"}
