"""Streamer(chat SSE -> responses SSE)状态机单测,含并行 tool call 回归。"""

import json

from ksadk.model_proxy.transform import Streamer


def _events(sse_list):
    out = []
    for raw in sse_list:
        for line in raw.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def _run(chunks):
    s = Streamer("resp_t", "m")
    evts = list(s.start())
    for c in chunks:
        evts += s.handle(c)
    evts += s.finalize()
    return _events(evts)


def _completed(ev):
    return next(e for e in ev if e["type"] == "response.completed")["response"]


def test_text_stream_has_delta_and_completed():
    chunks = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {
            "choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        },
    ]
    ev = _run(chunks)
    assert "response.output_text.delta" in {e["type"] for e in ev}
    resp = _completed(ev)
    assert resp["status"] == "completed"
    assert resp["output"][0]["content"][0]["text"] == "Hello"
    assert resp["usage"]["input_tokens"] == 1


def test_reasoning_then_text():
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "think "}}]},
        {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]},
    ]
    resp = _completed(_run(chunks))
    assert [o["type"] for o in resp["output"]] == ["reasoning", "message"]


def test_parallel_tool_calls_not_truncated():
    """两个 tool call 交错流式:参数必须各自完整,不得因 index 切换被截断(review P0 回归)。"""
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "function": {"name": "shell", "arguments": '{"a":'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_b",
                                "function": {"name": "read", "arguments": '{"b":'},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 1, "function": {"arguments": "2}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    ev = _run(chunks)
    # 两个 function_call 各自 added + done
    fcs = {
        o["call_id"]: o["arguments"]
        for o in _completed(ev)["output"]
        if o["type"] == "function_call"
    }
    assert fcs == {"call_a": '{"a":1}', "call_b": '{"b":2}'}


def test_tool_call_arguments_delta_emitted():
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "function": {"name": "shell", "arguments": '{"a":'},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    ev = _run(chunks)
    types = [e["type"] for e in ev]
    assert "response.function_call_arguments.delta" in types
    assert "response.function_call_arguments.done" in types


def test_interrupted_stream_emits_failed_not_completed():
    # 无 finish_reason 就结束(断流):应发 response.failed,而非 completed/incomplete
    chunks = [{"choices": [{"delta": {"content": "partial"}}]}]
    ev = _run(chunks)
    types = [e["type"] for e in ev]
    assert "response.failed" in types
    assert "response.completed" not in types
    assert "response.incomplete" not in types
