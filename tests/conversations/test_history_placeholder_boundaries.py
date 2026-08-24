from ksadk.conversations.context import build_history_from_events
from ksadk.sessions.base import SessionEvent


def _event(seq: int, event_type: str, author: str, text: str) -> SessionEvent:
    return SessionEvent(
        id=f"evt-{seq}",
        author=author,
        event_type=event_type,
        content={"parts": [{"text": text}]},
        seq_id=seq,
    )


def test_runtime_placeholders_never_merge_with_normal_messages() -> None:
    history = build_history_from_events(
        [
            _event(1, "assistant_message", "agent", "before"),
            _event(2, "tool_call", "agent", "dangerous()"),
            _event(3, "assistant_message", "agent", "after"),
            _event(4, "user_message", "user", "question"),
            _event(5, "tool_result", "tool", "result"),
            _event(6, "user_message", "user", "continue"),
        ]
    )

    assert history == [
        {"role": "model", "content": "before"},
        {"role": "model", "content": "[tool_call] dangerous()"},
        {"role": "model", "content": "after"},
        {"role": "user", "content": "question"},
        {"role": "user", "content": "[tool_result] result"},
        {"role": "user", "content": "continue"},
    ]
