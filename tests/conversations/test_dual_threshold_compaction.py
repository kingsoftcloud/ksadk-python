"""PR D1：双阈值 compaction（ksadk_hosted 门控）+ proactive soft/hard 触发带。

门控语义：仅 ``prompt_integration_mode=="ksadk_hosted"`` 时启用 soft(50%)/hard(~84%) 双阈值；
非门控走旧单阈值，``trigger_band==""``。``force=True``（PTL）始终 ``trigger_band=="emergency"``，
绕过阈值。``len(groups) <= tail_groups`` 早退保留（不每轮压缩）。
"""

from __future__ import annotations

import pytest

from ksadk.conversations.model_context import (
    get_auto_compact_hard_limit_tokens,
    get_auto_compact_soft_limit_tokens,
    get_auto_compact_threshold_tokens,
)
from ksadk.conversations.runtime_compaction import _plan_compaction
from ksadk.sessions.base import SessionEvent

_MODEL_METADATA = {"context_window_tokens": 200_000, "limits": {"max_output_tokens": 32_000}}


def _user_event(seq: int, text: str = "x" * 1000, invocation_id: str = "inv1") -> SessionEvent:
    return SessionEvent(
        id=f"u-{seq}",
        seq_id=seq,
        event_type="user_message",
        author="user",
        invocation_id=invocation_id,
        content={"role": "user", "parts": [{"text": text}]},
    )


def _assistant_event(seq: int, text: str = "y" * 1000, invocation_id: str = "inv1") -> SessionEvent:
    return SessionEvent(
        id=f"a-{seq}",
        seq_id=seq,
        event_type="assistant_message",
        author="runner",
        invocation_id=invocation_id,
        content={"role": "model", "parts": [{"text": text}]},
    )


def _round_events(start_seq: int) -> list[SessionEvent]:
    """一个完整 api round：user + assistant。"""
    return [_user_event(start_seq), _assistant_event(start_seq + 1)]


def _many_rounds(n: int, chars_per_turn: int = 5000) -> list[SessionEvent]:
    """n 个独立 api round（每轮独立 invocation_id → group_events_by_api_round 每轮成一组）。"""
    events: list[SessionEvent] = []
    seq = 1
    for i in range(n):
        inv = f"inv-{i}"
        events.append(_user_event(seq, "u" * chars_per_turn, invocation_id=inv))
        events.append(_assistant_event(seq + 1, "a" * chars_per_turn, invocation_id=inv))
        seq += 2
    return events


# --- 阈值函数 ---


def test_soft_and_hard_limit_values(monkeypatch) -> None:
    monkeypatch.delenv("KSADK_COMPACT_SOFT_LIMIT_PCT", raising=False)
    monkeypatch.delenv("KSADK_COMPACT_HARD_LIMIT_PCT", raising=False)
    soft = get_auto_compact_soft_limit_tokens(_MODEL_METADATA)
    hard = get_auto_compact_hard_limit_tokens(_MODEL_METADATA)
    legacy = get_auto_compact_threshold_tokens(_MODEL_METADATA)
    # soft = 50% of effective window (200k - 20k reserve = 180k) = 90k
    assert soft == 90_000
    # hard 默认复用 legacy 算法（~167k）
    assert hard == legacy
    assert hard > soft


def test_hard_limit_pct_env_override(monkeypatch) -> None:
    monkeypatch.setenv("KSADK_COMPACT_HARD_LIMIT_PCT", "85")
    hard = get_auto_compact_hard_limit_tokens(_MODEL_METADATA)
    # 85% of 180k effective = 153k
    assert hard == 153_000


# --- _plan_compaction 双阈值门控 ---


def test_non_ksadk_hosted_uses_legacy_single_threshold(monkeypatch) -> None:
    """非门控 → trigger_band=""，旧单阈值行为。token 略低于 legacy 阈值 → 不压缩。"""
    monkeypatch.delenv("KSADK_COMPACT_SOFT_LIMIT_PCT", raising=False)
    monkeypatch.delenv("KSADK_COMPACT_HARD_LIMIT_PCT", raising=False)
    events = _many_rounds(6, chars_per_turn=5_000)  # ~30k tokens < legacy 167k
    plan = _plan_compaction(events, model_metadata=_MODEL_METADATA, prompt_integration_mode="")
    assert plan.trigger_band == ""
    assert plan.soft_limit_tokens is None
    assert plan.hard_limit_tokens is None
    # 旧单阈值：~30k < 167k → 不压缩
    assert plan.should_compact is False


def test_ksadk_hosted_soft_band_triggers(monkeypatch) -> None:
    """ksadk_hosted + total > soft_limit + groups 充足 → should_compact, trigger_band=soft。"""
    monkeypatch.delenv("KSADK_COMPACT_SOFT_LIMIT_PCT", raising=False)
    monkeypatch.delenv("KSADK_COMPACT_HARD_LIMIT_PCT", raising=False)
    # soft=90k。构造约 120k tokens（超 soft、未超 hard），且 6 rounds > tail 4。
    events = _many_rounds(6, chars_per_turn=40_000)  # ~120k
    plan = _plan_compaction(
        events, model_metadata=_MODEL_METADATA, prompt_integration_mode="ksadk_hosted"
    )
    assert plan.soft_limit_tokens == 90_000
    assert plan.hard_limit_tokens is not None
    assert plan.should_compact is True
    assert plan.trigger_band == "soft"


def test_framework_assisted_uses_dual_threshold_when_ksadk_owns_compaction(
    monkeypatch,
) -> None:
    events = _many_rounds(6, chars_per_turn=40_000)

    plan = _plan_compaction(
        events,
        model_metadata=_MODEL_METADATA,
        prompt_integration_mode="framework_assisted",
        compaction_owner="ksadk",
    )

    assert plan.should_compact is True
    assert plan.trigger_band == "soft"


def test_ksadk_hosted_hard_band_triggers(monkeypatch) -> None:
    """ksadk_hosted + total > hard_limit → trigger_band=hard。"""
    monkeypatch.delenv("KSADK_COMPACT_SOFT_LIMIT_PCT", raising=False)
    monkeypatch.delenv("KSADK_COMPACT_HARD_LIMIT_PCT", raising=False)
    # hard=167k。构造 ~270k tokens（超 hard），6 rounds > tail 4
    events = _many_rounds(6, chars_per_turn=90_000)  # ~270k
    plan = _plan_compaction(
        events, model_metadata=_MODEL_METADATA, prompt_integration_mode="ksadk_hosted"
    )
    assert plan.should_compact is True
    assert plan.trigger_band == "hard"


def test_ksadk_hosted_groups_too_few_no_compact(monkeypatch) -> None:
    """ksadk_hosted 但 groups <= tail_groups（4）→ 不压缩（即使超 soft）。避免每轮压缩。"""
    monkeypatch.delenv("KSADK_COMPACT_SOFT_LIMIT_PCT", raising=False)
    monkeypatch.delenv("KSADK_COMPACT_HARD_LIMIT_PCT", raising=False)
    # 3 rounds（< tail_groups=4），单 round 巨大（超 soft）
    events = _many_rounds(3, chars_per_turn=90_000)  # ~270k，但仅 3 rounds
    plan = _plan_compaction(
        events, model_metadata=_MODEL_METADATA, prompt_integration_mode="ksadk_hosted"
    )
    assert plan.should_compact is False
    # groups 不足 → 无可压缩（groups_to_compact 空），即使 token 超 soft 也不压缩。
    # trigger_band 可能记 soft（token 确超 soft），但 should_compact=False 才是门禁保证。
    assert plan.groups_to_compact == []


def test_force_ptl_always_emergency_regardless_of_gate(monkeypatch) -> None:
    """force=True（PTL）→ trigger_band=emergency，绕过阈值，与门控无关。"""
    monkeypatch.delenv("KSADK_COMPACT_SOFT_LIMIT_PCT", raising=False)
    # force 下需 groups > keep_tail_groups(2) 才有 groups_to_compact
    events = _many_rounds(6, chars_per_turn=1_000)  # 6 groups > tail 2
    for mode in ("", "ksadk_hosted"):
        plan = _plan_compaction(
            events,
            model_metadata=_MODEL_METADATA,
            force=True,
            keep_tail_groups=2,
            prompt_integration_mode=mode,
        )
        assert plan.trigger_band == "emergency"
        # force 下 soft/hard 仍填充（ksadk_hosted）或 None（非门控），但不影响 emergency 判定
        assert plan.should_compact is True


def test_ksadk_hosted_below_soft_no_compact(monkeypatch) -> None:
    """ksadk_hosted + total < soft_limit → none，不压缩。"""
    monkeypatch.delenv("KSADK_COMPACT_SOFT_LIMIT_PCT", raising=False)
    events = _many_rounds(6, chars_per_turn=4_000)  # ~24k < soft 90k
    plan = _plan_compaction(
        events, model_metadata=_MODEL_METADATA, prompt_integration_mode="ksadk_hosted"
    )
    assert plan.should_compact is False
    assert plan.trigger_band == "none"


# --- 集成：build_run_input proactive compact 透传门控 ---


@pytest.mark.asyncio
async def test_build_run_input_proactive_compact_threads_gate(monkeypatch) -> None:
    """build_run_input + ksadk_hosted + 超 soft 的长 history → 触发 proactive compact，
    checkpoint metadata trigger_band 记 soft/hard。"""
    from ksadk.conversations.runtime_preparation import build_run_input
    from ksadk.sessions.in_memory import InMemorySessionService

    monkeypatch.delenv("KSADK_COMPACT_SOFT_LIMIT_PCT", raising=False)
    monkeypatch.delenv("KSADK_COMPACT_HARD_LIMIT_PCT", raising=False)
    monkeypatch.delenv("KSADK_PLATFORM_SAFETY_TEXT", raising=False)

    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id="sess-d1")
    # 预先灌入超 soft 的长历史。tokenizer ~3 chars/token，soft=90k tokens → 需 ~270k+ chars。
    # 6 rounds × 50_000 = 300k chars → ~90k+ tokens > soft 90k，groups(6) > tail(4)。
    for i in range(6):
        await service.append_event(
            "sess-d1",
            SessionEvent(
                id=f"pre-u-{i}",
                seq_id=i * 2 + 1,
                event_type="user_message",
                author="user",
                invocation_id=f"pre-{i}",
                content={"role": "user", "parts": [{"text": "u" * 50_000}]},
            ),
        )
        await service.append_event(
            "sess-d1",
            SessionEvent(
                id=f"pre-a-{i}",
                seq_id=i * 2 + 2,
                event_type="assistant_message",
                author="runner",
                invocation_id=f"pre-{i}",
                content={"role": "model", "parts": [{"text": "a" * 50_000}]},
            ),
        )

    prepared = await build_run_input(
        agent_id="a",
        user_id="u",
        session_id="sess-d1",
        messages=[{"role": "user", "content": "继续"}],
        model="m",
        model_metadata=_MODEL_METADATA,
        agent_system="你是助手",
        agent_task="用中文",
        prompt_integration_mode="ksadk_hosted",
        session_service_provider=lambda: service,
        runtime_type="langgraph",
    )
    # proactive compact 触发（超 soft）
    assert prepared.compaction_triggered is True
    events = await service.get_events("sess-d1")
    checkpoint = next((e for e in reversed(events) if e.event_type == "context_checkpoint"), None)
    assert checkpoint is not None
    meta = checkpoint.metadata or {}
    # trigger_band 审计字段存在且为 soft/hard（非 emergency，因为是 proactive 非 PTL）
    assert meta.get("trigger_band") in ("soft", "hard")
