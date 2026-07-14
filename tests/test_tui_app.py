"""InteractionLoop 全屏 TUI tests（v3，对标 Codex CLI）。

全屏 alternate screen + HSplit(transcript FormattedTextControl+ANSI 滚动 /
输入框 / footer)。这些测试覆盖 session/命令分派/app 结构/颜色保留/表格/
footer context/落定/排队 等维度。
"""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
os.environ.setdefault("database_url", "sqlite+aiosqlite:///./test.db")

from ksadk.tui.loop import InteractionLoop, TranscriptEntry, _render_entry_ansi, _welcome_block


class _Runner:
    def __init__(self, session_id=None):
        self.session_id = session_id


def test_interaction_loop_prefers_runner_session_id():
    runner = _Runner(session_id="sess-demo")
    loop = InteractionLoop(runner, project_dir=".")
    assert loop.session_id == "sess-demo"


def test_interaction_loop_generates_session_when_runner_has_none():
    runner = _Runner(session_id=None)
    loop = InteractionLoop(runner, project_dir=".")
    assert loop.session_id  # 生成了非空
    assert runner.session_id == loop.session_id  # 回写到 runner


def test_handle_command_quit():
    runner = _Runner(session_id="s")
    loop = InteractionLoop(runner, project_dir=".")
    assert loop._handle_command("exit") == "quit"
    assert loop._handle_command("quit") == "quit"
    assert loop._handle_command("退出") == "quit"


def test_handle_command_help_does_not_send():
    runner = _Runner(session_id="s")
    loop = InteractionLoop(runner, project_dir=".")
    assert loop._handle_command("?") == "handled"
    assert loop._handle_command("/help") == "handled"


def test_handle_command_new_resets_session_and_history():
    runner = _Runner(session_id="old")
    loop = InteractionLoop(runner, project_dir=".")
    loop.history = [{"role": "user", "content": "x"}]
    result = loop._handle_command("/new")
    assert result == "handled"
    assert loop.session_id != "old"
    assert loop.session_id == runner.session_id
    assert loop.history == []


def test_handle_command_normal_input_sends():
    runner = _Runner(session_id="s")
    loop = InteractionLoop(runner, project_dir=".")
    assert loop._handle_command("你好") == "send"


def test_handle_command_slash_commands_handled():
    runner = _Runner(session_id="s")
    loop = InteractionLoop(runner, project_dir=".")
    assert loop._handle_command("/clear") == "handled"
    assert loop._handle_command("/session") == "handled"
    assert loop._handle_command("/unknown") == "handled"


def test_build_application_is_full_screen():
    """全屏 alternate screen（对标 Codex CLI）:full_screen=True。"""
    runner = _Runner(session_id="sess-demo")
    loop = InteractionLoop(runner, project_dir=".")

    app = loop._build_application()

    assert app.full_screen is True
    assert loop._input_buffer is not None
    assert loop._transcript_window is not None
    assert app.layout.current_buffer is loop._input_buffer


def test_build_application_no_alt_screen_flag():
    """--no-alt-screen 时不进 alternate screen（保留 scrollback）。"""
    runner = _Runner(session_id="sess-demo")
    loop = InteractionLoop(runner, project_dir=".", no_alt_screen=True)

    app = loop._build_application()
    assert app.full_screen is False


def test_build_application_theme_is_terminal_background_safe():
    """不强制 reverse/white/bg，避免浅色终端背景色糊住文字。"""
    runner = _Runner(session_id="sess-demo")
    loop = InteractionLoop(runner, project_dir=".")

    app = loop._build_application()
    styles = dict(app.style.style_rules)

    assert "reverse" not in styles.get("transcript", "")
    assert "reverse" not in styles.get("footer", "")
    assert "white" not in styles.get("input", "")


def test_render_entry_ansi_keeps_foreground_colors():
    """落定的 assistant rich 输出必须保留前景色。"""
    ansi = _render_entry_ansi(
        TranscriptEntry(role="assistant", content="**粗体** 普通文本"),
        show_thinking=False,
    )
    assert "\x1b[" in ansi  # 颜色码保留
    assert "**粗体**" not in ansi  # Markdown 已渲染成 rich 文本而非源码


def test_render_entry_ansi_renders_markdown_table():
    """表格落定时由 rich 完整渲染：框线对齐、列对齐，框线在结果里。"""
    ansi = _render_entry_ansi(
        TranscriptEntry(role="assistant", content="| A | B |\n|---|---|\n| 1 | 2 |"),
        show_thinking=False,
    )
    assert "─" in ansi  # 表格框线
    assert "| A | B |" not in ansi  # 源码表格被渲染成框线表格


def test_render_entry_ansi_strips_background_but_keeps_foreground():
    """浅/深色终端兼容：剥背景色（40/48），但保留前景色。"""
    ansi = _render_entry_ansi(
        TranscriptEntry(role="assistant", content="# 标题\n\n正文"),
        show_thinking=False,
    )
    assert "\x1b[" in ansi  # 前景色在
    assert "\x1b[40m" not in ansi  # 无背景色
    assert "\x1b[48;" not in ansi  # 无 256/真彩背景


def test_render_entry_role_prefixes_are_subtle_claude_style():
    """user=› assistant=● tool=•(单 bullet,对标 Codex) error=!,不用标签。"""
    import re

    def _plain(s):
        return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", s)

    user = _plain(_render_entry_ansi(TranscriptEntry(role="user", content="好的"), show_thinking=False))
    asst = _plain(_render_entry_ansi(TranscriptEntry(role="assistant", content="收到"), show_thinking=False))
    tool = _plain(_render_entry_ansi(TranscriptEntry(role="tool", content="search [running]"), show_thinking=False))
    err = _plain(_render_entry_ansi(TranscriptEntry(role="error", content="boom"), show_thinking=False))

    assert "› 好的" in user
    assert "●" in asst and "收到" in asst
    assert "• Running search" in tool
    assert "! boom" in err
    assert "You:" not in user
    assert "Assistant:" not in asst


def test_welcome_block_has_rounded_border_and_help():
    """启动屏圆角框（对标 Codex session.rs）含 model/session，下方帮助列表。"""
    block = _welcome_block("abc123", "glm-5.1", __import__("pathlib").Path("/tmp/proj"), show_help=True)
    assert "╭" in block and "╮" in block  # 顶圆角
    assert "╰" in block and "╯" in block  # 底圆角
    assert "glm-5.1" in block
    assert "abc123" in block
    assert "/new" in block  # 帮助命令


def test_transcript_shows_welcome_before_first_message():
    """首条消息前 transcript 显示 welcome 圆角框。"""
    runner = _Runner(session_id="sess-demo")
    loop = InteractionLoop(runner, project_dir=".")
    loop._build_application()
    loop._refresh_transcript()

    assert "╭" in loop._transcript_ansi  # welcome 圆角框


def test_commit_entry_appends_to_transcript():
    """落定 = append entry 到 _entries + 刷新 transcript ansi。"""
    runner = _Runner(session_id="sess-demo")
    loop = InteractionLoop(runner, project_dir=".")
    loop._build_application()

    loop._commit_entry_sync(TranscriptEntry(role="assistant", content="落定内容"))

    assert len(loop._entries) == 1
    assert "落定内容" in loop._transcript_ansi
    assert "\x1b[" in loop._transcript_ansi  # 颜色保留


def test_streaming_entry_shown_in_transcript():
    """streaming entry 在 transcript 末尾动态显示。"""
    runner = _Runner(session_id="sess-demo")
    loop = InteractionLoop(runner, project_dir=".")
    loop._build_application()
    loop._streaming_entry = TranscriptEntry(role="assistant", content="正在生成", status="streaming")
    loop._refresh_transcript()

    assert "正在生成" in loop._transcript_ansi


def test_external_window_scroll_disables_follow_on_next_stream_refresh(monkeypatch):
    """A direct Window scroll change must disable follow before the next frame."""
    runner = _Runner(session_id="sess-demo")
    loop = InteractionLoop(runner, project_dir=".")
    loop._build_application()
    loop._last_max_scroll = 40
    loop._user_scroll = 40
    loop._pin_to_bottom = True
    loop._transcript_window.vertical_scroll = 25
    monkeypatch.setattr(loop, "_max_scroll", lambda: 50)

    loop._streaming_entry = TranscriptEntry(
        role="assistant",
        content="new streaming content",
        status="streaming",
    )
    loop._refresh_transcript()

    assert loop._transcript_window.vertical_scroll == 25
    assert loop._user_scroll == 25
    assert loop._pin_to_bottom is False


def test_following_transcript_tracks_new_bottom(monkeypatch):
    runner = _Runner(session_id="sess-demo")
    loop = InteractionLoop(runner, project_dir=".")
    loop._build_application()
    loop._last_max_scroll = 40
    loop._user_scroll = 40
    loop._pin_to_bottom = True
    loop._transcript_window.vertical_scroll = 40
    monkeypatch.setattr(loop, "_max_scroll", lambda: 50)

    loop._refresh_transcript()

    assert loop._transcript_window.vertical_scroll == 50
    assert loop._user_scroll == 50
    assert loop._pin_to_bottom is True


def test_context_percent_uses_codex_format():
    """context 格式对标 Codex: Context {n}% left · {used}K used · {window}K window。"""
    runner = _Runner(session_id="s")
    runner.model_metadata = {"context_window_tokens": 200000}
    loop = InteractionLoop(runner, project_dir=".")
    loop._last_usage = {"total_tokens": 21000, "last_usage": {"total_tokens": 21000}}

    ctx = loop._context_percent()
    # used=21000, window=200000, remaining=179000 → 89.5% → int=89%
    assert ctx == "Context 89% left · 21.0K used · 200.0K window"


def test_context_percent_omitted_without_model_metadata():
    runner = _Runner(session_id="s")
    loop = InteractionLoop(runner, project_dir=".")
    loop._last_usage = {"total_tokens": 100, "last_usage": {"total_tokens": 50}}
    assert loop._context_percent() is None


def test_context_percent_omitted_without_token_usage():
    runner = _Runner(session_id="s")
    runner.model_metadata = {"context_window_tokens": 200000}
    loop = InteractionLoop(runner, project_dir=".")
    assert loop._context_percent() is None


def test_footer_includes_context_percent():
    runner = _Runner(session_id="s")
    runner.model_metadata = {"context_window_tokens": 100000}
    loop = InteractionLoop(runner, project_dir=".")
    loop._last_usage = {"last_usage": {"total_tokens": 20500}}

    footer = "".join(t for _s, t in loop._footer_fragments())
    # used=20500, window=100000, remaining=79500 → 79.5% → int=79%
    assert "Context 79% left" in footer
    assert "20.5K used" in footer
    assert "100.0K window" in footer


def test_slash_completer_offers_commands():
    from prompt_toolkit.document import Document

    from ksadk.tui.loop import SlashCommandCompleter

    completer = SlashCommandCompleter()
    completions = list(completer.get_completions(Document("/c"), None))
    assert any(item.text == "/clear" for item in completions)


def test_submit_while_streaming_queues_input():
    """streaming 时提交 → 排队下一轮，不立即发给 agent。"""

    async def _body():
        runner = _Runner(session_id="s")
        loop = InteractionLoop(runner, project_dir=".")
        loop._build_application()
        loop._active_task = asyncio.Future()  # 伪造活跃 turn

        loop._submit_text("第二轮问题")

        assert [item.text for item in loop._queued_inputs] == ["第二轮问题"]
        assert loop.history == []  # 未真正发出，不入 history

    asyncio.run(_body())


def test_build_input_data_marks_responses_turn_for_responses_runner():
    runner = _Runner(session_id="s")
    runner.api_format = "responses"
    loop = InteractionLoop(runner, project_dir=".")

    input_data = loop._build_input_data("你好")
    assert input_data["input"] == "你好"
    assert input_data["session_id"] == "s"
    assert input_data["responses_conversation"] is True


def test_build_input_data_omits_responses_flag_for_chat_runner():
    runner = _Runner(session_id="s")
    loop = InteractionLoop(runner, project_dir=".")

    input_data = loop._build_input_data("你好")
    assert "responses_conversation" not in input_data
