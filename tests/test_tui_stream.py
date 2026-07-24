"""TUI 流式渲染纯函数测试（extract_stream_delta / format_usage / clean_response）。

这些函数从 app.py 迁到 stream_render.py，被 loop.py（交互 TUI）和 cmd_invoke._invoke_once
（-m 单次）共用。Textual 专属测试（AgentTUI/AssistantMessage/去硬编码颜色）已随 Textual
移除废弃，由 test_tui_loop.py 的 render_stream 测试替代。
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
os.environ.setdefault("database_url", "sqlite+aiosqlite:///./test.db")

from ksadk.tui.stream_render import clean_response, extract_stream_delta, format_usage


def test_extract_stream_delta_text_chunk_returns_delta():
    assert extract_stream_delta({"type": "text", "delta": "你好"}) == ("你好", None, False)


def test_extract_stream_delta_final_chunk_does_not_return_output_as_delta():
    """chat 路径结束的 final chunk 带 output=完整文本，不能当 delta append。"""
    delta, usage, is_terminal = extract_stream_delta(
        {"type": "final", "output": "完整回复文本", "usage": {"input_tokens": 10}}
    )
    assert delta == ""
    assert usage == {"input_tokens": 10}
    assert is_terminal is True


def test_extract_stream_delta_responses_output_chunk_does_not_return_list_as_delta():
    """responses 路径 response.completed 的 responses_output 带 output=list，
    不能当 delta append。
    """
    delta, usage, is_terminal = extract_stream_delta(
        {
            "type": "responses_output",
            "output": [{"type": "message", "content": [{"text": "回复"}]}],
            "usage": {"input_tokens": 20, "output_tokens": 5},
            "response_id": "resp_1",
        }
    )
    assert delta == ""
    assert usage == {"input_tokens": 20, "output_tokens": 5}
    assert is_terminal is True


def test_extract_stream_delta_final_without_usage_still_terminal():
    delta, usage, is_terminal = extract_stream_delta({"type": "final", "output": "文本"})
    assert delta == ""
    assert usage is None
    assert is_terminal is True


def test_extract_stream_delta_unknown_type_falls_back_to_delta():
    delta, _usage, is_terminal = extract_stream_delta({"type": "weird", "delta": "x"})
    assert delta == "x"
    assert is_terminal is False


def test_format_usage_handles_both_field_naming_conventions():
    """兼容 input_tokens/output_tokens 与 prompt_tokens/completion_tokens。"""
    assert (
        format_usage({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}) == "↑10 ↓5 ⌀15"
    )
    assert (
        format_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        == "↑10 ↓5 ⌀15"
    )


def test_format_usage_empty_or_none_returns_empty():
    assert format_usage(None) == ""
    assert format_usage({}) == ""


def test_format_usage_partial_fields():
    assert format_usage({"input_tokens": 100}) == "↑100"
    assert format_usage({"total_tokens": 50}) == "⌀50"


def test_clean_response_strips_internal_debug_artifacts():
    """清理 LLM 响应里的内部伪影,但保留正常 XML/HTML 文本。"""
    assert clean_response("[Tool Result: x] hello") == "hello"
    assert clean_response("<think>hidden</think>answer") == "answer"
    assert clean_response("<tag>text</tag>") == "<tag>text</tag>"
    assert clean_response("正常文本") == "正常文本"
