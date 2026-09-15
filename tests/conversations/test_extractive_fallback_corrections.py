from __future__ import annotations

from ksadk.conversations.context import summarize_event_groups
from ksadk.conversations.semantic_summary import extract_working_state
from ksadk.sessions.base import SessionEvent


def _user(seq: int, text: str) -> SessionEvent:
    return SessionEvent(
        id=f"user-{seq}",
        seq_id=seq,
        event_type="user_message",
        author="user",
        invocation_id=f"inv-{seq}",
        content={"role": "user", "parts": [{"text": text}]},
    )


def _assistant(seq: int, text: str = "收到") -> SessionEvent:
    return SessionEvent(
        id=f"assistant-{seq}",
        seq_id=seq,
        event_type="assistant_message",
        author="agent",
        invocation_id=f"inv-{seq}",
        content={"role": "model", "parts": [{"text": text}]},
    )


def test_extractive_fallback_preserves_long_latest_instruction_with_budget_bound() -> None:
    correction = "不要改生产配置，只使用 uv run，并先执行 dry-run"
    text = "背景" * 5000 + correction

    summary = summarize_event_groups([[_user(1, text), _assistant(2)]])

    assert correction in summary
    assert "最新用户指令（有界保留）" in summary
    assert "中间内容因上下文预算省略" in summary
    assert len(summary) < 11_000


def test_correction_before_later_user_message_reaches_working_state() -> None:
    correction = "最新 Region=cn-shanghai；旧 Region cn-beijing 已废弃"
    groups = [
        [_user(1, correction), _assistant(2)],
        [_user(3, "继续执行后续检查"), _assistant(4)],
    ]

    summary = summarize_event_groups(groups)
    state = extract_working_state(
        [event for group in groups for event in group],
        summary_text=summary,
        source_seq_range=(1, 4),
    )

    assert correction.split("；")[0] in summary
    assert any("cn-shanghai" in str(item.get("text")) for item in state.errors_and_corrections)
    assert state.current_goal == ""
