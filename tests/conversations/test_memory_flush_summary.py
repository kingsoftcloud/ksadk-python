"""admission boundary 完善：Memory Flush + 摘要 v2 解析 + Memory Candidate 抽取（方案 §9.2/§9.3/§10.3）。"""

from __future__ import annotations

import pytest

from ksadk.conversations.semantic_summary import extract_working_state
from ksadk.memory.extraction import propose_memory_candidates
from ksadk.sessions.base import SessionEvent


def _user_event(seq, text, inv="i1"):
    return SessionEvent(
        id=f"u-{seq}",
        seq_id=seq,
        event_type="user_message",
        author="user",
        invocation_id=inv,
        content={"role": "user", "parts": [{"text": text}]},
        metadata={},
    )


def _assistant_event(seq, text, inv="i1"):
    return SessionEvent(
        id=f"a-{seq}",
        seq_id=seq,
        event_type="assistant_message",
        author="assistant",
        invocation_id=inv,
        content={"role": "assistant", "parts": [{"text": text}]},
        metadata={},
    )


# ---- Memory Candidate 抽取 ----


def test_extractor_explicit_remember():
    events = [_user_event(1, "记住：部署用 uv run 而不是全局 pip")]
    cands = propose_memory_candidates(events, scope_id="u1")
    assert len(cands) == 1
    assert cands[0].memory_type == "profile"
    assert cands[0].reason == "explicit_user_request"
    assert "uv run" in cands[0].content


def test_extractor_remember_english():
    events = [_user_event(1, "Remember that the main branch is master")]
    cands = propose_memory_candidates(events, scope_id="u1")
    assert len(cands) == 1 and "master" in cands[0].content.lower()


def test_extractor_assigns_stable_slot_to_explicit_food_preference():
    events = [_user_event(1, "记住我喜欢吃芥末")]
    cands = propose_memory_candidates(events, scope_id="u1")
    assert len(cands) == 1
    assert cands[0].content == "我喜欢吃芥末"
    assert cands[0].slot_key == "profile.preference.food"


def test_extractor_treats_explicit_preference_correction_as_update():
    events = [_user_event(1, "我喜欢吃的是西红柿，不是芥末。")]
    cands = propose_memory_candidates(events, scope_id="u1")
    assert len(cands) == 1
    assert cands[0].operation == "update"
    assert cands[0].content == "我喜欢吃西红柿"
    assert cands[0].slot_key == "profile.preference.food"
    assert cands[0].reason == "explicit_user_correction"


def test_extractor_assigns_stable_slot_to_explicit_hobby():
    events = [_user_event(1, "记住我的爱好是羽毛球")]
    cands = propose_memory_candidates(events, scope_id="u1")
    assert len(cands) == 1
    assert cands[0].content == "我的爱好是羽毛球"
    assert cands[0].slot_key == "profile.preference.hobby"


def test_extractor_treats_hobby_restatement_as_correction():
    events = [_user_event(1, "记住我的爱好其实是乒乓球")]
    cands = propose_memory_candidates(events, scope_id="u1")
    assert len(cands) == 1
    assert cands[0].operation == "update"
    assert cands[0].content == "我的爱好是乒乓球"
    assert cands[0].slot_key == "profile.preference.hobby"
    assert cands[0].reason == "explicit_user_correction"


def test_extractor_tool_fact():
    events = [_assistant_event(2, "测试结果 confirmed: 全部通过")]
    cands = propose_memory_candidates(events, scope_id="u1")
    assert any(c.memory_type == "fact" and c.reason == "tool_fact" for c in cands)


def test_extractor_no_candidate_for_normal_message():
    events = [_user_event(1, "帮我看看这个文件"), _assistant_event(2, "好的")]
    assert propose_memory_candidates(events, scope_id="u1") == []


def test_extractor_proposes_implicit_preference_without_treating_it_as_explicit():
    cands = propose_memory_candidates([_user_event(1, "我喜欢吃土豆。")], scope_id="u1")

    assert len(cands) == 1
    assert cands[0].reason == "implicit_user_preference"
    assert cands[0].slot_key == "profile.preference.food"
    assert cands[0].confidence < 0.85


def test_extractor_empty_events():
    assert propose_memory_candidates([], scope_id="u1") == []


# ---- 摘要 v2 解析 ----


def test_parse_summary_v2_next_action():
    summary = (
        "当前用户目标：升级依赖\n已完成进展：改了 pyproject\n"
        "下一步工作位置：跑 uv run pytest\n重要引用：无"
    )
    ws = extract_working_state([], summary_text=summary, source_seq_range=(1, 10))
    assert ws.next_action is not None and "uv run pytest" in ws.next_action


def test_parse_summary_v2_decisions_and_errors():
    summary = (
        "当前用户目标：修 bug\n"
        "重要决策：选择方案 A\n"
        "错误修正：之前漏了类型注解\n"
        "下一步工作位置：提交\n"
    )
    ws = extract_working_state([], summary_text=summary, source_seq_range=(1, 10))
    assert ws.decisions and "方案 A" in ws.decisions[0]["text"]
    assert ws.errors_and_corrections and "类型注解" in ws.errors_and_corrections[0]["text"]


def test_parse_summary_v2_empty_when_no_markers():
    ws = extract_working_state([], summary_text="一段没有标记的普通摘要", source_seq_range=(1, 5))
    assert ws.next_action is None
    assert ws.decisions == []
    assert ws.errors_and_corrections == []


def test_parse_summary_v2_english_markers():
    summary = "Next Step: run tests\nDecision: use plan A"
    ws = extract_working_state([], summary_text=summary, source_seq_range=(1, 5))
    assert ws.next_action and "run tests" in ws.next_action


# ---- Memory Flush 端到端（门控）----


@pytest.mark.asyncio
async def test_memory_flush_gated_off_by_default(monkeypatch):
    from ksadk.conversations.runtime_compaction import _maybe_memory_flush

    monkeypatch.delenv("KSADK_MEMORY_FLUSH_ENABLED", raising=False)
    result = await _maybe_memory_flush([_user_event(1, "记住：用 Python 3.12")])
    assert result is None  # 默认关，不改行为


@pytest.mark.asyncio
async def test_memory_flush_runs_when_enabled(monkeypatch):
    from ksadk.conversations.runtime_compaction import _maybe_memory_flush

    monkeypatch.setenv("KSADK_MEMORY_FLUSH_ENABLED", "true")
    events = [_user_event(1, "记住：默认 Python 3.12")]
    result = await _maybe_memory_flush(events, user_id="user-1")
    assert result is not None
    assert result["status"] in ("succeeded", "skipped")
    if result.get("proposed", 0) > 0:
        assert result["committed"] >= 1 or result["rejected"] >= 1


@pytest.mark.asyncio
async def test_memory_flush_rejects_secret(monkeypatch):
    from ksadk.conversations.runtime_compaction import _maybe_memory_flush

    monkeypatch.setenv("KSADK_MEMORY_FLUSH_ENABLED", "true")
    events = [_user_event(1, "记住：api_key=sk-abcdefghijklmnopqrstuvwxyz")]
    result = await _maybe_memory_flush(events)
    assert result is not None
    # secret 被 Policy 拒绝 → rejected，不 commit
    assert result.get("committed", 0) == 0
