from ksadk.conversations.message_projection import project_session_messages


def _event(
    seq_id: int,
    event_type: str,
    text: str,
    *,
    stream_boundary: bool = False,
) -> dict:
    return {
        "EventId": f"evt-{seq_id}",
        "EventType": event_type,
        "InvocationId": "inv-1",
        "SeqId": seq_id,
        "Timestamp": "2026-07-29T00:00:00Z",
        "Content": {"role": "model", "parts": [{"text": text}]},
        **({"Metadata": {"stream_boundary": "before_text"}} if stream_boundary else {}),
    }


def test_history_projection_preserves_interleaved_reasoning_and_stream_text():
    messages = project_session_messages(
        [
            _event(1, "user_message", "演示开发流程"),
            _event(2, "reasoning", "先确定演示结构。", stream_boundary=True),
            _event(3, "assistant_stream_snapshot", "【阶段 1/2】已确定解决思路。"),
            _event(4, "reasoning", "现在展开最终答案。", stream_boundary=True),
            _event(
                5,
                "assistant_stream_snapshot",
                "【阶段 1/2】已确定解决思路。【阶段 2/2】下面是最终答案。",
            ),
            _event(
                6,
                "assistant_message",
                "【阶段 1/2】已确定解决思路。【阶段 2/2】下面是最终答案。",
            ),
        ],
        include_reasoning=True,
    )

    assert messages[-1]["Blocks"] == [
        {"Type": "thinking", "Content": "先确定演示结构。", "SeqId": 2},
        {"Type": "text", "Content": "【阶段 1/2】已确定解决思路。", "SeqId": 3},
        {"Type": "thinking", "Content": "现在展开最终答案。", "SeqId": 4},
        {"Type": "text", "Content": "【阶段 2/2】下面是最终答案。", "SeqId": 5},
    ]


def test_history_projection_keeps_legacy_late_reasoning_on_fallback_contract():
    messages = project_session_messages(
        [
            _event(1, "user_message", "演示开发流程"),
            _event(2, "assistant_stream_snapshot", "【阶段 1/2】已确定解决思路。"),
            _event(
                3,
                "assistant_stream_snapshot",
                "【阶段 1/2】已确定解决思路。【阶段 2/2】下面是最终答案。",
            ),
            _event(4, "reasoning", "合并的历史推理。"),
            _event(
                5,
                "assistant_message",
                "【阶段 1/2】已确定解决思路。【阶段 2/2】下面是最终答案。",
            ),
        ],
        include_reasoning=True,
    )

    assert "Blocks" not in messages[-1]
