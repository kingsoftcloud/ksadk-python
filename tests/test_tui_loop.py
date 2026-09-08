"""InteractionLoop + render_stream 测试。

render_stream 是纯函数：消费 RemoteRunner.stream 的 chunk 序列，分派到 renderer
回调。测试 mock renderer，断言分派正确性（text 累计/final 不重复/tool_call 去重/
interrupt 信号/error）。不依赖 Rich Live 或真实终端。
"""

import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
os.environ.setdefault("database_url", "sqlite+aiosqlite:///./test.db")

import pytest

from ksadk.tui.loop import InterruptPending, render_stream


class _StreamRunner:
    """喂预设 chunk 序列的假 runner。"""

    def __init__(self, chunks, invoke_result=None):
        self._chunks = chunks
        self._invoke_result = invoke_result or {}

    async def stream(self, input_data):
        for c in self._chunks:
            yield c

    async def invoke(self, input_data):
        return self._invoke_result


class _RecordingRenderer:
    """记录 render_stream 的渲染回调调用，验证分派。"""

    def __init__(self):
        self.text_deltas = []  # text delta 累计
        self.thinking_deltas = []  # thinking delta
        self.tool_calls = []  # (call_id, tool_name, status)
        self.usages = []  # usage dict
        self.errors = []  # error message
        self.finalized = False

    async def on_text(self, full_text):
        self.text_deltas.append(full_text)

    async def on_thinking(self, full_thinking):
        self.thinking_deltas.append(full_thinking)

    async def on_tool_call(self, tool_name, status, args=None, call_id=""):
        self.tool_calls.append((tool_name, status))

    async def on_usage(self, usage):
        self.usages.append(usage)

    async def on_error(self, message):
        self.errors.append(message)

    async def finalize(self):
        self.finalized = True


class _DetailedToolRenderer(_RecordingRenderer):
    def __init__(self):
        super().__init__()
        self.tool_events = []

    async def on_tool_call(self, tool_name, status, args=None, call_id=""):
        self.tool_events.append((tool_name, status, args, call_id))


def _run(coro):
    return asyncio.run(coro)


def test_render_stream_accumulates_text_deltas():
    """text chunk 的 delta 累计到 full_text，每次更新调 on_text。"""
    runner = _StreamRunner(
        [
            {"type": "text", "delta": "你"},
            {"type": "text", "delta": "好"},
        ]
    )
    r = _RecordingRenderer()

    response, usage = _run(render_stream(runner, {"input": "你好"}, renderer=r))

    assert response == "你好"
    assert r.text_deltas[-1] == "你好"  # 最后一次是全文
    assert usage is None


def test_render_stream_final_chunk_does_not_duplicate_output():
    """final chunk 带 output=完整文本，只取 usage，不把 output 当 delta append。"""
    runner = _StreamRunner(
        [
            {"type": "text", "delta": "你好"},
            {
                "type": "final",
                "output": "你好",
                "usage": {"total_tokens": 5, "input_tokens": 3, "output_tokens": 2},
            },
        ]
    )
    r = _RecordingRenderer()

    response, usage = _run(render_stream(runner, {"input": "x"}, renderer=r))

    assert response == "你好"  # 不是 "你好你好"
    assert r.usages == [{"total_tokens": 5, "input_tokens": 3, "output_tokens": 2}]


def test_render_stream_responses_output_does_not_append_list():
    """responses_output 的 output=list 只取 usage，不 append list。"""
    runner = _StreamRunner(
        [
            {"type": "text", "delta": "回复"},
            {
                "type": "responses_output",
                "output": [{"content": [{"text": "回复"}]}],
                "usage": {"total_tokens": 3},
            },
        ]
    )
    r = _RecordingRenderer()

    response, usage = _run(render_stream(runner, {"input": "x"}, renderer=r))

    assert response == "回复"
    assert r.usages == [{"total_tokens": 3}]


def test_render_stream_preserves_last_usage_metadata_for_context_status():
    """terminal chunks must keep metadata.last_usage so TUI can show context usage."""
    runner = _StreamRunner(
        [
            {
                "type": "final",
                "output": "ok",
                "usage": {"total_tokens": 2100},
                "metadata": {"last_usage": {"input_tokens": 4000}},
            },
        ]
    )
    r = _RecordingRenderer()

    response, usage = _run(render_stream(runner, {"input": "x"}, renderer=r))

    assert response == ""
    assert usage == {"total_tokens": 2100, "last_usage": {"input_tokens": 4000}}
    assert r.usages == [usage]


def test_render_stream_dedupes_tool_call_by_name_status():
    """真实 RemoteRunner chunk 无 call_id；按 (tool_name, status) 去重同状态重复 chunk。"""
    runner = _StreamRunner(
        [
            {"type": "tool_call", "tool_name": "search", "tool_args": "{}", "status": "running"},
            {
                "type": "tool_call",
                "tool_name": "search",
                "tool_args": "{}",
                "status": "running",
            },  # 重复 running 去重
            {"type": "tool_call", "tool_name": "other", "tool_args": "{}", "status": "running"},
        ]
    )
    r = _RecordingRenderer()

    _run(render_stream(runner, {"input": "x"}, renderer=r))

    # search:running 去重成 1 次，other:running 1 次
    search_running = [t for t in r.tool_calls if t == ("search", "running")]
    assert len(search_running) == 1
    assert ("other", "running") in r.tool_calls


def test_render_stream_tool_call_status_transitions_pass_through():
    """同工具的 running→completed 是不同 status，不去重，状态迁移透传。"""
    runner = _StreamRunner(
        [
            {"type": "tool_call", "tool_name": "search", "tool_args": "{}", "status": "running"},
            {"type": "tool_call", "tool_name": "search", "tool_args": "{}", "status": "completed"},
        ]
    )
    r = _RecordingRenderer()

    _run(render_stream(runner, {"input": "x"}, renderer=r))

    assert ("search", "running") in r.tool_calls
    assert ("search", "completed") in r.tool_calls


def test_render_stream_keeps_tool_argument_updates_and_result_for_same_call_id():
    runner = _StreamRunner(
        [
            {
                "type": "tool_call",
                "tool_name": "search",
                "tool_args": "",
                "status": "running",
                "call_id": "fc_1",
            },
            {
                "type": "tool_call",
                "tool_name": "search",
                "tool_args": '{"q":"openclaw"}',
                "status": "running",
                "call_id": "fc_1",
            },
            {
                "type": "tool_result",
                "tool_name": "search",
                "tool_output": '{"ok":true}',
                "call_id": "fc_1",
            },
        ]
    )
    renderer = _DetailedToolRenderer()

    _run(render_stream(runner, {"input": "x"}, renderer=renderer))

    assert renderer.tool_events == [
        ("search", "running", {}, "fc_1"),
        ("search", "running", '{"q":"openclaw"}', "fc_1"),
        ("search", "result", '{"ok":true}', "fc_1"),
    ]


def test_render_stream_http_error_does_not_crash_and_calls_on_error():
    """stream 抛 httpx/transport 错误时 on_error + finalize，不 crash（不传播异常）。"""

    class _ErrorRunner:
        async def stream(self, d):
            raise RuntimeError("401 Unauthorized")
            yield {}  # noqa

        async def invoke(self, d):
            return {}

    r = _RecordingRenderer()
    response, _usage = _run(render_stream(_ErrorRunner(), {"input": "x"}, renderer=r))

    assert any("401" in e for e in r.errors)
    assert r.finalized is True  # finally 调了 finalize


def test_render_stream_transport_error_does_not_retry_with_invoke():
    invoke_called = []

    class _Runner:
        async def stream(self, _input):
            raise RuntimeError("stream disconnected")
            yield  # pragma: no cover

        async def invoke(self, _input):
            invoke_called.append(True)
            return {"output": "duplicate request"}

    renderer = _RecordingRenderer()

    response, _usage = _run(render_stream(_Runner(), {"input": "x"}, renderer=renderer))

    assert response == ""
    assert invoke_called == []
    assert renderer.errors == ["stream disconnected"]


def test_render_stream_tool_only_turn_does_not_trigger_spurious_invoke():
    """纯 tool_call turn（无 text）有 saw_content，不触发 fallback invoke（避免 double 执行）。"""
    invoke_called = []

    class _Runner:
        async def stream(self, d):
            yield {
                "type": "tool_call",
                "tool_name": "search",
                "tool_args": "{}",
                "status": "running",
            }
            yield {"type": "responses_output", "output": [], "usage": {"total_tokens": 1}}

        async def invoke(self, d):
            invoke_called.append(True)
            return {}

    r = _RecordingRenderer()
    _run(render_stream(_Runner(), {"input": "x"}, renderer=r))

    assert invoke_called == []  # 关键：有 tool_call，不 fallback invoke


def test_render_stream_renderers_on_tool_call_no_call_id_param():
    """on_tool_call 新签名只收 (tool_name, status, args)，无 call_id。"""
    runner = _StreamRunner(
        [{"type": "tool_call", "tool_name": "t", "tool_args": "{}", "status": "running"}]
    )
    r = _RecordingRenderer()
    _run(render_stream(runner, {"input": "x"}, renderer=r))
    assert r.tool_calls == [("t", "running")]


def test_render_stream_default_renderer_accepts_tool_events():
    runner = _StreamRunner(
        [
            {
                "type": "tool_call",
                "tool_name": "search",
                "tool_args": "{}",
                "status": "running",
                "call_id": "fc_1",
            },
            {"type": "text", "delta": "done"},
        ]
    )

    response, _usage = _run(render_stream(runner, {"input": "x"}))

    assert response == "done"


def test_render_stream_tool_result_is_visible_as_restrained_tool_entry():
    """Responses function_call_output chunks should not disappear from the TUI."""
    runner = _StreamRunner(
        [
            {
                "type": "tool_result",
                "tool_name": "search",
                "tool_output": '{"result":"done"}',
            },
        ]
    )
    r = _RecordingRenderer()

    _run(render_stream(runner, {"input": "x"}, renderer=r))

    assert r.tool_calls == [("search", "result")]


def test_render_stream_dedupes_replayed_tool_results():
    """Some runtimes replay function_call_output again in response.completed.output."""
    runner = _StreamRunner(
        [
            {
                "type": "tool_result",
                "tool_name": "search",
                "tool_output": '{"result":"done"}',
            },
            {
                "type": "tool_result",
                "tool_name": "search",
                "tool_output": '{"result":"done"}',
            },
        ]
    )
    r = _RecordingRenderer()

    _run(render_stream(runner, {"input": "x"}, renderer=r))

    assert r.tool_calls == [("search", "result")]


def test_render_stream_thinking_deltas_accumulate():
    """thinking chunk 的 delta 累计。"""
    runner = _StreamRunner(
        [
            {"type": "thinking", "delta": "先想"},
            {"type": "thinking", "delta": "一下"},
            {"type": "text", "delta": "答案"},
        ]
    )
    r = _RecordingRenderer()

    _run(render_stream(runner, {"input": "x"}, renderer=r))

    assert r.thinking_deltas[-1] == "先想一下"


def test_render_stream_raises_interrupt_pending_for_interrupt_chunk():
    """interrupt chunk 抛 InterruptPending 信号给循环，带 interrupt_info。"""
    info = {"id": "ap1", "name": "dangerous", "server_label": "srv"}
    runner = _StreamRunner([{"type": "interrupt", "interrupt_info": info}])
    r = _RecordingRenderer()

    with pytest.raises(InterruptPending) as exc:
        _run(render_stream(runner, {"input": "x"}, renderer=r))

    assert exc.value.interrupt_info == info


def test_render_stream_error_chunk_calls_on_error():
    """error chunk 调 on_error。"""
    runner = _StreamRunner([{"type": "error", "message": "boom"}])
    r = _RecordingRenderer()

    response, _usage = _run(render_stream(runner, {"input": "x"}, renderer=r))

    assert r.errors == ["boom"]


def test_render_stream_empty_stream_falls_back_to_invoke():
    """stream 无任何文本且未中断 → fallback runner.invoke，返回其 output。"""
    runner = _StreamRunner([], invoke_result={"output": "兜底回复"})
    r = _RecordingRenderer()

    response, _usage = _run(render_stream(runner, {"input": "x"}, renderer=r))

    assert response == "兜底回复"


def test_render_stream_finalizes_renderer():
    """流结束调 renderer.finalize（收尾重绘/spinner 停）。"""
    runner = _StreamRunner([{"type": "text", "delta": "hi"}])
    r = _RecordingRenderer()

    _run(render_stream(runner, {"input": "x"}, renderer=r))

    assert r.finalized is True
