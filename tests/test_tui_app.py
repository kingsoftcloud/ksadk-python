"""InteractionLoop inline TUI tests（对标 Codex CLI）。

Inline scrollback + HSplit(transcript FormattedTextControl+ANSI 滚动 /
输入框 / footer)。这些测试覆盖 session/命令分派/app 结构/颜色保留/表格/
footer context/落定/排队 等维度。
"""
import asyncio
import json
import os
import re
import time
from types import SimpleNamespace

from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
os.environ.setdefault("database_url", "sqlite+aiosqlite:///./test.db")

from ksadk.tui.loop import (
    InteractionLoop,
    RichLiveRenderer,
    TranscriptEntry,
    _codex_surface_style,
    _render_entry_ansi,
    _welcome_block,
)


class _Runner:
    def __init__(self, session_id=None):
        self.session_id = session_id


class _FixedSizeOutput(DummyOutput):
    def get_size(self):
        return Size(rows=12, columns=60)


class _CodexSizeOutput(DummyOutput):
    def get_size(self):
        return Size(rows=30, columns=100)


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


def test_build_application_uses_inline_terminal_buffer():
    """Codex-style inline viewport preserves native terminal scrollback."""
    runner = _Runner(session_id="sess-demo")
    loop = InteractionLoop(runner, project_dir=".")

    app = loop._build_application()

    assert app.full_screen is False
    assert loop._input_buffer is not None
    assert loop._transcript_window is not None
    assert app.layout.current_buffer is loop._input_buffer


def test_build_application_leaves_mouse_to_native_scrollback():
    """Inline mode must not capture wheel/selection events from the terminal."""
    loop = InteractionLoop(_Runner(session_id="sess-demo"), project_dir=".")

    app = loop._build_application()

    assert not app.mouse_support()


def test_build_application_throttles_streaming_redraws():
    """Token-level invalidations must be coalesced to avoid stale footer rows."""
    loop = InteractionLoop(_Runner(session_id="sess-demo"), project_dir=".")

    app = loop._build_application()

    assert app.min_redraw_interval == 0.05


def test_working_elapsed_time_refreshes_without_stream_events():
    """The status timer must not wait for the next model token to repaint."""

    async def _body():
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=_CodexSizeOutput()):
                loop = InteractionLoop(_Runner(session_id="sess-demo"), project_dir=".")
                loop._streaming_entry = TranscriptEntry(role="assistant", status="streaming")
                loop._turn_started_at = time.monotonic()
                app = loop._build_application()
                task = asyncio.create_task(app.run_async())
                active_turn = asyncio.get_running_loop().create_future()
                loop._active_task = active_turn
                try:
                    await asyncio.sleep(0.1)
                    loop._ensure_status_refresh()
                    loop._turn_started_at = time.monotonic() - 3
                    await asyncio.sleep(0.7)
                    screen = app.renderer._last_screen
                    rendered = "\n".join(
                        "".join(
                            screen.data_buffer[y][x].char or " "
                            for x in range(100)
                        )
                        for y in range(30)
                    )
                    assert "Working (3s" in rendered
                finally:
                    active_turn.cancel()
                    app.exit()
                    await task

    asyncio.run(_body())


def test_build_application_uses_blinking_beam_cursor():
    from prompt_toolkit.cursor_shapes import CursorShape

    loop = InteractionLoop(_Runner(session_id="sess-demo"), project_dir=".")
    app = loop._build_application()

    assert app.cursor.get_cursor_shape(app) is CursorShape.BLINKING_BEAM


def test_model_picker_matches_codex_selection_view_shape(monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "environment-model")
    runner = _Runner(session_id="sess-demo")
    runner.model = "gpt-5.2"
    runner.available_models = [
        {"id": "gpt-5.6-sol"},
        {"id": "gpt-5.6-terra"},
        {"id": "gpt-5.2"},
    ]
    loop = InteractionLoop(runner, project_dir=".")
    loop._build_application()
    loop._open_model_picker(runner.available_models)

    header = "".join(text for _style, text in loop._model_picker_header_fragments())
    rows = "".join(text for _style, text in loop._model_picker_fragments())

    assert "Select Model and Effort" in header
    assert "Access legacy models" in header
    assert "  1. gpt-5.6-sol" in rows
    assert "  2. gpt-5.6-terra" in rows
    assert "› 3. gpt-5.2 (current)" in rows


def test_page_and_arrow_keys_scroll_rendered_transcript():
    """PageUp/PageDown own transcript paging; arrows remain composer navigation."""

    async def _body():
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=_FixedSizeOutput()):
                loop = InteractionLoop(_Runner(session_id="sess-demo"), project_dir=".")
                loop._entries = [
                    TranscriptEntry(
                        role="assistant",
                        content="\n\n".join(f"paragraph {i}" for i in range(100)),
                    )
                ]
                app = loop._build_application()
                task = asyncio.create_task(app.run_async())
                try:
                    await asyncio.sleep(0.05)
                    pipe_input.send_text("\x1b[5~")  # PageUp
                    await asyncio.sleep(0.05)
                    after_page_up = loop._transcript_window.vertical_scroll
                    assert after_page_up > 0
                    assert loop._history_pager_active is True

                    pipe_input.send_text("\x1b[5~")  # PageUp again
                    await asyncio.sleep(0.05)
                    pipe_input.send_text("\x1b[6~")  # PageDown
                    await asyncio.sleep(0.05)
                    after_page_down = loop._transcript_window.vertical_scroll
                    assert after_page_down == after_page_up

                finally:
                    app.exit()
                    await task

    asyncio.run(_body())


def test_arrow_keys_select_slash_completion_and_confirm_with_enter():
    """Slash popup has priority over input history, matching Codex command navigation."""

    async def _body():
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=_FixedSizeOutput()):
                loop = InteractionLoop(_Runner(session_id="sess-demo"), project_dir=".")
                selected: list[str] = []
                loop._submit_text = selected.append
                app = loop._build_application()
                task = asyncio.create_task(app.run_async())
                try:
                    await asyncio.sleep(0.05)
                    pipe_input.send_text("/")
                    await asyncio.sleep(0.05)
                    assert loop._input_buffer.complete_state is not None

                    pipe_input.send_text("\x1b[B")
                    await asyncio.sleep(0.05)
                    completion = loop._input_buffer.complete_state.current_completion
                    assert completion is not None
                    expected = completion.text

                    pipe_input.send_text("\r")
                    await asyncio.sleep(0.05)
                    assert selected == [expected]
                finally:
                    app.exit()
                    await task

    asyncio.run(_body())


def test_arrow_keys_recall_submitted_input_history():
    """Outside popups, Up/Down navigate submitted composer history."""

    async def _body():
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=_FixedSizeOutput()):
                loop = InteractionLoop(_Runner(session_id="sess-demo"), project_dir=".")
                loop._submit_text = lambda _text: None
                app = loop._build_application()
                task = asyncio.create_task(app.run_async())
                try:
                    await asyncio.sleep(0.05)
                    pipe_input.send_text("first question\r")
                    await asyncio.sleep(0.05)
                    pipe_input.send_text("second question\r")
                    await asyncio.sleep(0.05)

                    pipe_input.send_text("\x1b[A")
                    await asyncio.sleep(0.05)
                    assert loop._input_buffer.text == "second question"
                    pipe_input.send_text("\x1b[A")
                    await asyncio.sleep(0.05)
                    assert loop._input_buffer.text == "first question"
                    pipe_input.send_text("\x1b[B")
                    await asyncio.sleep(0.05)
                    assert loop._input_buffer.text == "second question"
                finally:
                    app.exit()
                    await task

    asyncio.run(_body())


def test_live_renderer_preserves_text_tool_text_event_order():
    """Completed tools stay where they occurred instead of moving below the final answer."""

    async def _body():
        loop = InteractionLoop(_Runner(session_id="sess-demo"), project_dir=".")
        loop._build_application()
        assistant = TranscriptEntry(role="assistant", status="streaming")
        renderer = RichLiveRenderer(loop, assistant)

        await renderer.on_text("before tool")
        await renderer.on_tool_call("list_skills", "running", {"action": "list"}, call_id="call-1")
        await renderer.on_tool_call("list_skills", "result", {"ok": True}, call_id="call-1")
        await renderer.on_text("before tool\nafter tool")

        entries = renderer.final_entries("before tool\nafter tool")
        assert [entry.role for entry in entries] == ["assistant", "tool", "assistant"]
        assert entries[0].content == "before tool"
        assert "list_skills" in entries[1].content
        assert entries[1].status == "result"
        assert entries[2].content == "\nafter tool"

    asyncio.run(_body())


def test_cursor_is_clamped_when_transcript_shrinks_between_frames():
    """A stale render_info must not point the next UIContent past its last line."""
    loop = InteractionLoop(_Runner(session_id="sess-demo"), project_dir=".")
    loop._build_application()
    window = loop._transcript_window

    window.vertical_scroll = 40
    window.render_info = SimpleNamespace(
        ui_content=SimpleNamespace(line_count=50),
        window_height=8,
    )
    loop._transcript_ansi = "short\n"

    ui_content = window.content.create_content(width=60, height=8)
    window._scroll_when_linewrapping(ui_content, width=60, height=8)

    assert ui_content.cursor_position.y < ui_content.line_count


def test_cursor_survives_repeated_transcript_growth_and_shrink():
    loop = InteractionLoop(_Runner(session_id="sess-demo"), project_dir=".")
    loop._build_application()
    window = loop._transcript_window

    for index in range(100):
        stale_lines = 120 if index % 2 == 0 else 2
        current_lines = 2 if index % 2 == 0 else 120
        window.vertical_scroll = stale_lines - 1
        window.render_info = SimpleNamespace(
            ui_content=SimpleNamespace(line_count=stale_lines),
            window_height=8,
        )
        loop._transcript_ansi = "line\n" * (current_lines - 1)

        ui_content = window.content.create_content(width=60, height=8)
        window._scroll_when_linewrapping(ui_content, width=60, height=8)

        assert ui_content.cursor_position.y < ui_content.line_count


def test_shell_matches_codex_content_first_layout():
    """Composer follows short transcript content instead of sticking to the bottom."""

    async def _body():
        with create_pipe_input() as pipe_input:
            with create_app_session(input=pipe_input, output=_CodexSizeOutput()):
                runner = _Runner(session_id="sess-demo")
                runner.model = "glm-test"
                loop = InteractionLoop(runner, project_dir="/tmp/project")
                app = loop._build_application()
                task = asyncio.create_task(app.run_async())
                try:
                    deadline = time.monotonic() + 1.0
                    lines: list[str] = []
                    while time.monotonic() < deadline:
                        await asyncio.sleep(0.01)
                        screen = app.renderer._last_screen
                        lines = [
                            "".join(screen.data_buffer[y][x].char or " " for x in range(100))
                            for y in range(30)
                        ]
                        if any("Ask KsADK to do anything" in line for line in lines) and any(
                            "glm-test" in line and "/tmp/project" in line for line in lines
                        ):
                            break
                    placeholder_row = next(
                        i for i, line in enumerate(lines) if "Ask KsADK to do anything" in line
                    )
                    assert placeholder_row < 20
                    assert not any(
                        line.strip() and set(line.strip()) == {"─"} for line in lines
                    )
                    footer = next(line for line in lines if "glm-test" in line and "/tmp/project" in line)
                    assert "session" not in footer
                finally:
                    app.exit()
                    await task

    asyncio.run(_body())


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


def test_composer_surface_uses_codex_light_and_dark_blends():
    # A 4% blend is appropriate for Codex user-message cells but too subtle
    # for this full-width composer on a white terminal background.
    assert _codex_surface_style((255, 255, 255)) == "bg:#eaeaea"
    assert _codex_surface_style((40, 44, 52)) == "bg:#41454c"
    assert _codex_surface_style(None) == ""


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
    """user=› assistant/tool=• error=!, matching Codex history cells."""
    import re

    def _plain(s):
        return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", s)

    user = _plain(_render_entry_ansi(TranscriptEntry(role="user", content="好的"), show_thinking=False))
    asst = _plain(_render_entry_ansi(TranscriptEntry(role="assistant", content="收到"), show_thinking=False))
    tool = _plain(_render_entry_ansi(TranscriptEntry(role="tool", content="search [running]"), show_thinking=False))
    err = _plain(_render_entry_ansi(TranscriptEntry(role="error", content="boom"), show_thinking=False))

    assert "› 好的" in user
    assert "•" in asst and "收到" in asst
    assert "• Running search" in tool
    assert "! boom" in err
    assert "You:" not in user
    assert "Assistant:" not in asst


def test_render_tool_result_unwraps_nested_json_before_folding():
    payload = json.dumps(
        {
            "ok": True,
            "skills": [
                {"name": "ppt-translator", "description": "translate presentations"},
                {"name": "skill-creator", "description": "create skills"},
            ],
        },
        ensure_ascii=False,
    )
    encoded_payload = json.dumps(payload, ensure_ascii=False)

    ansi = _render_entry_ansi(
        TranscriptEntry(
            role="tool",
            content=f"list_skills [result]\n{encoded_payload}",
            status="result",
        ),
        show_thinking=False,
        show_tool_details=True,
    )
    plain = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", ansi)

    assert "Ran list_skills" in plain
    assert '"skills": [' in plain
    assert '\\"skills\\"' not in plain


def test_tool_history_is_collapsed_by_default():
    loop = InteractionLoop(_Runner(session_id="sess-demo"), project_dir=".")
    loop._build_application()
    entry = TranscriptEntry(
        role="tool",
        content='list_skills [result]\n{"ok": true, "skills": [1, 2]}',
        status="result",
    )

    rendered = loop._history_entry_ansi(entry)

    assert "Ran list_skills" in rendered
    assert "└" not in rendered


def test_markdown_normalizes_common_model_heading_and_list_typos():
    from ksadk.tui.loop import _normalize_markdown

    normalized = _normalize_markdown(
        "```markdown\n###1. 📄 ppt-translator\n##标题\n\n- 功能：测试\n```"
    )

    assert normalized.splitlines() == ["1. 📄 ppt-translator", "## 标题", "", "- 功能：测试"]


def test_markdown_splits_inline_numbered_items_before_rendering():
    from ksadk.tui.loop import _normalize_markdown

    normalized = _normalize_markdown(
        "1 🔍 发现技能 - 查看可用技能2. 📄 加载技能 - 阅读 SKILL.md3. 🌐 网页抓取"
    )

    assert normalized.splitlines() == [
        "1. 🔍 发现技能 - 查看可用技能",
        "2. 📄 加载技能 - 阅读 SKILL.md",
        "3. 🌐 网页抓取",
    ]


def test_clear_terminal_sequence_purges_scrollback():
    from ksadk.tui.loop import _terminal_clear_sequence

    assert _terminal_clear_sequence() == "\x1b[r\x1b[0m\x1b[H\x1b[2J\x1b[3J\x1b[H"


def test_welcome_block_has_rounded_border_and_help():
    """启动屏圆角框（对标 Codex session.rs）含 model/session，下方帮助列表。"""
    block = _welcome_block("abc123", "glm-5.1", __import__("pathlib").Path("/tmp/proj"), show_help=True)
    assert "╭" in block and "╮" in block  # 顶圆角
    assert "╰" in block and "╯" in block  # 底圆角
    assert "glm-5.1" in block
    assert "abc123" in block
    assert "/help" in block


def test_welcome_block_border_edges_have_equal_visible_width():
    from prompt_toolkit.utils import get_cwidth

    lines = [line for line in _welcome_block("abc123", "glm-5.1", __import__("pathlib").Path("/tmp/proj"), show_help=True).splitlines() if line]
    assert get_cwidth(lines[0]) == get_cwidth(lines[1])
    assert get_cwidth(lines[0]) == get_cwidth(lines[-2])


def test_transcript_shows_welcome_before_first_message():
    """首条消息前 transcript 显示 welcome 圆角框。"""
    runner = _Runner(session_id="sess-demo")
    loop = InteractionLoop(runner, project_dir=".")
    loop._build_application()
    loop._refresh_transcript()

    assert "╭" in loop._welcome_history_ansi()  # welcome 圆角框写入 history
    assert "╭" not in loop._transcript_ansi  # live tail 不重复绘制 history


def test_commit_entry_appends_to_transcript():
    """落定 = append entry 到 _entries + 刷新 transcript ansi。"""
    runner = _Runner(session_id="sess-demo")
    loop = InteractionLoop(runner, project_dir=".")
    loop._build_application()

    loop._commit_entry_sync(TranscriptEntry(role="assistant", content="落定内容"))

    assert len(loop._entries) == 1
    assert "落定内容" in loop._history_entry_ansi(loop._entries[0])
    assert "落定内容" not in loop._transcript_ansi
    assert "\x1b[" in loop._history_entry_ansi(loop._entries[0])  # 颜色保留


def test_streaming_entry_shown_in_transcript():
    """streaming entry 在 transcript 末尾动态显示。"""
    runner = _Runner(session_id="sess-demo")
    loop = InteractionLoop(runner, project_dir=".")
    loop._build_application()
    loop._streaming_entry = TranscriptEntry(role="assistant", content="正在生成", status="streaming")
    loop._refresh_transcript()

    assert "正在生成" in loop._transcript_ansi


def test_completed_history_is_not_redrawn_in_live_transcript():
    """Committed history must be printed once above the live prompt, not redrawn per token."""
    runner = _Runner(session_id="sess-demo")
    loop = InteractionLoop(runner, project_dir=".")
    loop._build_application()
    loop._entries = [TranscriptEntry(role="assistant", content="completed response")]

    loop._refresh_transcript()

    assert "completed response" not in loop._transcript_ansi
    assert ">_ KsADK" not in loop._transcript_ansi


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
    assert "20.5K used" not in footer
    assert "100.0K window" not in footer


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
