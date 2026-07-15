"""Agent TUI based on a Codex-style inline prompt_toolkit application.

Transcript 使用 ANSI 保留 rich 颜色和 Markdown 落定格式，composer 紧跟内容，
终端原生 scrollback 保留。支持键盘滚动、流式跟底和输入排队。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from prompt_toolkit.completion import Completer

from ksadk.tui.stream_render import clean_response, extract_stream_delta

_TERMINAL_BG_PROBED = False
_TERMINAL_BG: tuple[int, int, int] | None = None


def _parse_terminal_rgb(response: str) -> tuple[int, int, int] | None:
    """Parse OSC 11 ``rgb:rrrr/gggg/bbbb`` or ``#rrggbb`` responses."""
    import re

    match = re.search(r"(?:rgb:)([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", response)
    if match:
        channels: list[int] = []
        for value in match.groups():
            maximum = (16 ** len(value)) - 1
            channels.append(int(int(value, 16) / maximum * 255) if maximum else 0)
        return channels[0], channels[1], channels[2]
    match = re.search(r"#([0-9a-fA-F]{6})", response)
    if match:
        value = match.group(1)
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    return None


def _terminal_background() -> tuple[int, int, int] | None:
    """Query the terminal default background using the same OSC 11 signal as Codex."""
    global _TERMINAL_BG, _TERMINAL_BG_PROBED
    if _TERMINAL_BG_PROBED:
        return _TERMINAL_BG
    _TERMINAL_BG_PROBED = True

    try:
        import fcntl
        import os
        import select
        import sys
        import termios
        import tty

        if os.name != "posix" or os.getenv("TERM", "") == "dumb":
            return None
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return None
        fd = sys.stdin.fileno()
        previous_termios = termios.tcgetattr(fd)
        previous_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        data = bytearray()
        try:
            tty.setcbreak(fd)
            fcntl.fcntl(fd, fcntl.F_SETFL, previous_flags | os.O_NONBLOCK)
            sys.stdout.write("\x1b]11;?\x1b\\")
            sys.stdout.flush()
            deadline = time.monotonic() + 0.12
            while time.monotonic() < deadline:
                readable, _, _ = select.select([fd], [], [], deadline - time.monotonic())
                if not readable:
                    break
                try:
                    chunk = os.read(fd, 128)
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                data.extend(chunk)
                if b"\x07" in data or b"\x1b\\" in data:
                    break
        finally:
            fcntl.fcntl(fd, fcntl.F_SETFL, previous_flags)
            termios.tcsetattr(fd, termios.TCSADRAIN, previous_termios)
        _TERMINAL_BG = _parse_terminal_rgb(data.decode("ascii", errors="ignore"))
    except Exception:
        _TERMINAL_BG = None
    return _TERMINAL_BG


def _codex_surface_rgb(
    background: tuple[int, int, int] | None,
    *,
    composer: bool = False,
) -> tuple[int, int, int] | None:
    """Match Codex's user-message/composer surface blend for light and dark terminals."""
    if background is None:
        return None
    r, g, b = background
    is_light = (0.299 * r + 0.587 * g + 0.114 * b) > 128.0
    top = (0, 0, 0) if is_light else (255, 255, 255)
    # Keep Codex's 4% user-message blend. The composer spans the whole width,
    # so 4% black on a light terminal is visually imperceptible; 8% gives the
    # input surface a stable edge without introducing a border.
    alpha = 0.08 if is_light and composer else 0.04 if is_light else 0.12
    return tuple(int(top[i] * alpha + background[i] * (1.0 - alpha)) for i in range(3))


def _codex_surface_style(background: tuple[int, int, int] | None) -> str:
    blended = _codex_surface_rgb(background, composer=True)
    if blended is None:
        return ""
    return f"bg:#{blended[0]:02x}{blended[1]:02x}{blended[2]:02x}"


def _terminal_clear_sequence() -> str:
    """Reset the inline viewport and purge terminal scrollback, like Codex ``/clear``."""
    return "\x1b[r\x1b[0m\x1b[H\x1b[2J\x1b[3J\x1b[H"


class InterruptPending(Exception):
    """interrupt chunk 信号：render_stream 抛出，InteractionLoop 捕获后弹确认。"""

    def __init__(self, interrupt_info: Any) -> None:
        super().__init__("interrupt pending")
        self.interrupt_info = interrupt_info


class _NullRenderer:
    """默认 no-op renderer（render_stream 不传 renderer 时用）。"""

    async def on_text(self, full_text: str) -> None:
        pass

    async def on_thinking(self, full_thinking: str) -> None:
        pass

    async def on_tool_call(
        self,
        tool_name: str,
        status: str,
        args: Any = None,
        call_id: str = "",
    ) -> None:
        pass

    async def on_usage(self, usage: dict) -> None:
        pass

    async def on_error(self, message: str) -> None:
        pass

    async def finalize(self) -> None:
        pass


async def render_stream(
    runner,
    input_data: dict,
    *,
    renderer=None,
) -> tuple[str, Optional[dict]]:
    """消费 runner.stream，分派 chunk 到 renderer，返回 (cleaned_response, usage)。

    - text/thinking → 累计全文，调 on_text/on_thinking
    - tool_call → 按调用身份和参数快照去重，调 on_tool_call
    - final/responses_output → 只取 usage（extract_stream_delta），不 append output
    - interrupt → 抛 InterruptPending
    - error → 调 on_error
    - 流空且未中断 → fallback runner.invoke
    流结束调 renderer.finalize()。
    """
    r = renderer if renderer is not None else _NullRenderer()
    full_response = ""
    full_thinking = ""
    usage: Optional[dict] = None
    tool_event_keys: set[tuple[str, str, str, str]] = set()
    stream_failed = False

    # text/thinking/tool_call/terminal 都算有内容，避免 tool-only turn 误触发 fallback
    saw_content = False
    try:
        async for chunk in runner.stream(input_data):
            chunk_type = str(chunk.get("type") or "text")

            if chunk_type == "thinking":
                delta = chunk.get("delta") or ""
                if delta:
                    full_thinking += str(delta)
                    saw_content = True
                    await r.on_thinking(full_thinking)
                continue

            if chunk_type == "interrupt":
                raise InterruptPending(chunk.get("interrupt_info"))

            if chunk_type == "tool_call":
                tool_name = str(chunk.get("tool_name") or chunk.get("name") or "")
                status = str(chunk.get("status") or "running").lower()
                args = chunk.get("tool_args") or chunk.get("arguments") or {}
                # 只过滤完全相同的工具事件。参数流会对同一个 call_id 连续给出更完整
                # 的快照，必须继续透传给 renderer 覆盖；调用和结果也必须使用不同键。
                call_id = str(chunk.get("call_id") or "")
                dedup_key = (
                    "call",
                    call_id or tool_name,
                    status,
                    _compact_json(args),
                )
                if dedup_key in tool_event_keys:
                    continue
                tool_event_keys.add(dedup_key)
                saw_content = True
                await r.on_tool_call(tool_name, status, args, call_id=call_id)
                continue

            if chunk_type == "tool_result":
                # responses 路径的 function_call_output。结果和调用分开去重，否则
                # 同一个 call_id 的 running 事件会把最终结果错误吞掉。
                tool_name = str(chunk.get("tool_name") or chunk.get("name") or "")
                call_id = str(chunk.get("call_id") or "")
                output = chunk.get("tool_output")
                dedup_key = (
                    "result",
                    call_id or tool_name,
                    "result",
                    _compact_json(output),
                )
                if dedup_key in tool_event_keys:
                    continue
                tool_event_keys.add(dedup_key)
                saw_content = True
                await r.on_tool_call(tool_name, "result", output, call_id=call_id)
                continue

            if chunk_type == "error":
                await r.on_error(str(chunk.get("message") or "未知错误"))
                saw_content = True
                continue

            delta, chunk_usage, is_terminal = extract_stream_delta(chunk)
            if is_terminal:
                if chunk_usage:
                    usage = chunk_usage
                    await r.on_usage(chunk_usage)
                saw_content = True
                continue
            if delta:
                full_response += delta
                saw_content = True
                await r.on_text(full_response)
    except InterruptPending:
        raise  # finalize 由 finally 统一处理
    except Exception as exc:
        # HTTP/transport 错误（raise_for_status 等）→ on_error，不 crash TUI。
        # 请求可能已在服务端执行，不能再 fallback invoke，否则工具副作用会执行两次。
        stream_failed = True
        await r.on_error(str(exc) or exc.__class__.__name__)
    finally:
        await r.finalize()

    if not full_response and not saw_content and not stream_failed:
        # 流完全空且未中断 → fallback invoke；失败显式 on_error，不静默吞。
        try:
            result = await runner.invoke(input_data)
            full_response = str(result.get("output") or "")
            if full_response:
                await r.on_text(full_response)
            if isinstance(result.get("usage"), dict):
                usage = result["usage"]
                await r.on_usage(usage)
        except Exception as exc:
            await r.on_error(str(exc) or exc.__class__.__name__)

    return clean_response(full_response), usage


@dataclass
class TranscriptEntry:
    role: str
    content: str = ""
    status: str = ""
    usage: Optional[dict[str, Any]] = None
    thinking: str = ""


@dataclass
class QueuedInput:
    text: str
    entry: TranscriptEntry


class SlashCommandCompleter(Completer):
    """Slash command completer that works with `/c` style prefixes.

    runner 可选：提供后 `/model ` 前缀补全 runner.available_models 的模型 id。
    """

    _COMMANDS = {
        "/new": "start a new session",
        "/clear": "clear transcript",
        "/session": "show session id",
        "/model": "show/switch model",
        "/tools": "toggle tool details",
        "/help": "show commands",
    }

    def __init__(self, runner=None) -> None:
        self.runner = runner

    def get_completions(self, document, complete_event):
        from prompt_toolkit.completion import Completion

        text = document.text_before_cursor
        prefix = text.strip()
        if not prefix.startswith("/"):
            return
        # /model <prefix> → 补全可选模型 id
        if prefix.startswith("/model ") and self.runner is not None:
            model_prefix = prefix[len("/model "):].strip()
            available = getattr(self.runner, "available_models", None) or []
            for m in available:
                mid = str(m.get("id") or m.get("name") or "")
                if mid and mid.startswith(model_prefix):
                    yield Completion(mid, start_position=-len(model_prefix), display_meta=m.get("display_name") or "")
            return
        for command, meta in self._COMMANDS.items():
            if command.startswith(prefix):
                yield Completion(command, start_position=-len(prefix), display_meta=meta)


class RichLiveRenderer:
    """Renderer adapter used by `render_stream` inside the inline TUI。

    文本和工具事件按到达顺序保存在 ``_ordered_entries``。同一个 call_id 的
    running/result 更新原位置，工具后的新文本进入新的 assistant 段，因此落定
    后不会把已完成工具统一搬到最终回复下面。
    """

    def __init__(
        self,
        loop: "InteractionLoop" | None = None,
        assistant_entry: TranscriptEntry | None = None,
        *,
        show_thinking: bool = False,
    ) -> None:
        self.loop = loop
        self.assistant_entry = assistant_entry
        self.show_thinking = show_thinking
        self._tool_entries: list[TranscriptEntry] = []
        self._ordered_entries: list[TranscriptEntry] = []
        self._tool_entries_by_key: dict[str, TranscriptEntry] = {}
        self._active_text_entry: TranscriptEntry | None = None
        self._last_full_text = ""

    def _compose_streaming(self) -> str:
        parts: list[str] = []
        for entry in self._ordered_entries:
            if entry.role == "assistant":
                if entry.content:
                    parts.append(entry.content)
                continue
            # streaming 期 tool 就地显示；完成态仍更新同一行，不改变事件位置。
            name = (entry.content or "").split("\n", 1)[0]
            parts.append(f"• {name}")
        return "\n".join(parts)

    async def on_text(self, full_text: str) -> None:
        if self.loop is not None and self.assistant_entry is not None:
            self.assistant_entry.content = full_text
            self.assistant_entry.status = "streaming"
            if full_text.startswith(self._last_full_text):
                delta = full_text[len(self._last_full_text):]
            else:
                # A final/snapshot event can replace the cumulative text. Preserve
                # ordering when possible; without tools it is safe to replace the
                # sole assistant segment directly.
                delta = full_text
                if not self._tool_entries:
                    self._ordered_entries.clear()
                    self._active_text_entry = None
            if delta:
                if self._active_text_entry is None:
                    self._active_text_entry = TranscriptEntry(
                        role="assistant",
                        content="",
                        status="streaming",
                    )
                    self._ordered_entries.append(self._active_text_entry)
                self._active_text_entry.content += delta
            self._last_full_text = full_text
            self.loop._set_streaming(self._compose_streaming())

    async def on_thinking(self, full_thinking: str) -> None:
        if self.loop is not None and self.assistant_entry is not None:
            self.assistant_entry.thinking = full_thinking
            # thinking 不进动态区，落定时随 assistant 一起渲染（show_thinking）

    async def on_tool_call(self, tool_name: str, status: str, args: Any = None, call_id: str = "") -> None:
        if self.loop is None:
            return
        content = f"{tool_name} [{status}]"
        if args:
            content = f"{content}\n{_compact_json(args)}"
        entry = TranscriptEntry(role="tool", content=content, status=status)
        # 同一调用按 call_id 覆盖（running → result 状态变化）；无 call_id 回退按 name 覆盖。
        # 不同 call_id（同名多次调用）各自追加，不互相覆盖。
        merge_key = call_id or tool_name
        existing = self._tool_entries_by_key.get(merge_key)
        if existing is None:
            self._tool_entries.append(entry)
            self._ordered_entries.append(entry)
            self._tool_entries_by_key[merge_key] = entry
            # The next text delta belongs after this tool event.
            self._active_text_entry = None
        else:
            existing.content = entry.content
            existing.status = entry.status
            entry = existing
        entry._tool_call_id = merge_key  # type: ignore[attr-defined]
        # tool 行立即进动态区（就地在文本流里显示），不中途落定避免时序错乱。
        self.loop._set_streaming(self._compose_streaming())

    async def on_usage(self, usage: dict) -> None:
        if self.loop is not None and self.assistant_entry is not None:
            self.assistant_entry.usage = usage
            self.loop._last_usage = usage

    async def on_error(self, message: str) -> None:
        if self.loop is not None:
            # error 立即落定（错误不属于 assistant 文本流，单列醒目）。
            await self.loop._commit_entry(TranscriptEntry(role="error", content=message))

    async def finalize(self) -> None:
        # 落定由 InteractionLoop._run_turn_async 统一处理，这里只清动态区。
        if self.loop is not None:
            self.loop._clear_streaming()

    def final_entries(self, response: str) -> list[TranscriptEntry]:
        """Return finalized display cells in the original stream event order."""
        if response and response.startswith(self._last_full_text):
            delta = response[len(self._last_full_text):]
            if delta:
                if self._active_text_entry is None:
                    self._active_text_entry = TranscriptEntry(role="assistant")
                    self._ordered_entries.append(self._active_text_entry)
                self._active_text_entry.content += delta
        elif response and not self._tool_entries:
            if self._ordered_entries:
                self._ordered_entries[0].content = response
            else:
                self._ordered_entries.append(TranscriptEntry(role="assistant", content=response))

        assistants = [entry for entry in self._ordered_entries if entry.role == "assistant"]
        if self.assistant_entry is not None and self.assistant_entry.thinking:
            if not assistants:
                thinking_entry = TranscriptEntry(role="assistant")
                self._ordered_entries.insert(0, thinking_entry)
                assistants = [thinking_entry]
            assistants[0].thinking = self.assistant_entry.thinking
        if self.assistant_entry is not None and self.assistant_entry.usage and assistants:
            assistants[-1].usage = self.assistant_entry.usage

        for entry in assistants:
            entry.status = ""
        return [
            entry
            for entry in self._ordered_entries
            if entry.role != "assistant" or entry.content or entry.thinking
        ]


class InteractionLoop:
    """Inline Codex-style prompt_toolkit interaction loop."""

    def __init__(
        self,
        runner,
        *,
        show_thinking: bool = False,
        project_dir: str = ".",
        no_alt_screen: bool = False,
    ) -> None:
        self.runner = runner
        self.show_thinking = show_thinking
        self.project_dir = Path(project_dir).resolve()
        self._no_alt_screen = no_alt_screen
        self.session_id = str(getattr(runner, "session_id", None) or uuid.uuid4().hex[:8])
        if getattr(runner, "session_id", None) is None:
            runner.session_id = self.session_id
        self.history: list[dict[str, str]] = []
        self._model_name = _resolve_model_name(runner)
        self._queued_inputs: list[QueuedInput] = []
        self._active_task: Any = None
        self._status_refresh_task: Any = None
        self._turn_started_at: float | None = None
        self._pending_interrupt: tuple[InterruptPending, dict[str, Any]] | None = None
        self._app = None
        self._input_buffer = None
        self._entries: list[TranscriptEntry] = []
        self._streaming_entry: TranscriptEntry | None = None
        self._transcript_ansi = ""
        self._transcript_window = None
        self._user_scroll = 0  # 用户手动滚动位置（pin bottom 时忽略）
        self._pin_to_bottom = True
        self._last_max_scroll = 0
        self._history_pager_active = False
        self._entry_ansi_cache: dict[int, str] = {}  # 历史 entries ANSI 缓存（避免 streaming 全量重渲）
        self._last_usage: dict[str, Any] | None = None
        self._showed_help = False  # 首次运行显示帮助列表
        self._welcome_ansi: str | None = None
        self._emitted_entry_ids: set[int] = set()
        self._show_tool_details = False
        # 交互式模型选择器状态
        self._model_picker_active = False
        self._model_picker_index = 0
        self._model_picker_models: list[dict[str, Any]] = []

    def run(self) -> None:
        asyncio.run(self.run_async())

    async def run_async(self) -> None:
        app = self._build_application()
        self._print_initial_history(app)
        await app.run_async()

    def _print_initial_history(self, app) -> None:
        """Write the startup card and any preloaded entries once before live rendering."""
        from prompt_toolkit.formatted_text import ANSI

        chunks = [self._welcome_history_ansi()]
        for entry in self._entries:
            chunks.append(self._history_entry_ansi(entry))
            self._emitted_entry_ids.add(id(entry))
        app.print_text(ANSI("\n".join(chunks)))

    def _welcome_history_ansi(self) -> str:
        if self._welcome_ansi is None:
            self._welcome_ansi = _welcome_block(
                self.session_id,
                self._current_model_name(),
                self.project_dir,
                show_help=True,
            )
            self._showed_help = True
        return self._welcome_ansi

    def _history_entry_ansi(self, entry: TranscriptEntry) -> str:
        key = id(entry)
        ansi = self._entry_ansi_cache.get(key)
        if ansi is None:
            ansi = _render_entry_ansi(
                entry,
                show_thinking=self.show_thinking,
                show_tool_details=self._show_tool_details,
            )
            self._entry_ansi_cache[key] = ansi
        return ansi.rstrip("\n")

    async def _emit_history_entry(self, entry: TranscriptEntry) -> None:
        """Commit one history cell above the live prompt, matching Codex scrollback."""
        from prompt_toolkit.application import run_in_terminal
        from prompt_toolkit.formatted_text import ANSI

        if id(entry) in self._emitted_entry_ids:
            return
        self._emitted_entry_ids.add(id(entry))
        if self._app is not None and self._app.is_running:
            await run_in_terminal(
                lambda: self._app.print_text(ANSI(self._history_entry_ansi(entry) + "\n"))
            )

    def _build_application(self):
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.cursor_shapes import CursorShape
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import (
            ConditionalContainer,
            Float,
            FloatContainer,
            HSplit,
            VSplit,
            Window,
        )
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.layout.menus import CompletionsMenu
        from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor
        from prompt_toolkit.styles import Style

        self._input_buffer = Buffer(
            completer=SlashCommandCompleter(runner=self.runner),
            complete_while_typing=True,
            multiline=True,
            history=InMemoryHistory(),
        )

        # Codex uses an inline, content-first viewport: short transcripts keep
        # the composer directly below the content and a filler absorbs the rest
        # of the terminal. Long transcripts shrink to the available viewport.
        self._transcript_height = lambda: Dimension(
            min=1,
            preferred=self._transcript_line_count(),
            max=self._transcript_line_count(),
        )
        self._transcript_window = Window(
            FormattedTextControl(
                self._transcript_fragments,
                get_cursor_position=self._transcript_cursor,
                show_cursor=False,
            ),
            height=self._transcript_height,
            wrap_lines=True,
            allow_scroll_beyond_bottom=True,
            style="class:transcript",
        )
        self._transcript_window_left = Window(
            width=2,
            height=self._transcript_height,
            char=" ",
            style="class:transcript",
        )
        transcript_row = VSplit([self._transcript_window_left, self._transcript_window])

        # Codex composer: a three-row input band, with the prompt vertically
        # centered and a muted placeholder while the buffer is empty.
        prompt_window = Window(
            FormattedTextControl(lambda: [("class:prompt", "› ")]),
            width=2,
            dont_extend_width=True,
        )
        self._input_height = lambda: Dimension(
            min=1,
            preferred=self._input_display_height(),
            max=max(1, self._input_display_height()),
        )
        placeholder = ConditionalProcessor(
            processor=AfterInput(
                "Ask KsADK to do anything",
                style="class:input-placeholder",
            ),
            filter=Condition(lambda: not self._input_buffer.text),
        )
        self._input_window = Window(
            BufferControl(
                buffer=self._input_buffer,
                input_processors=[placeholder],
            ),
            height=self._input_height,
            wrap_lines=True,
            style="class:input",
        )
        input_row = VSplit(
            [prompt_window, self._input_window],
            style="class:input-frame",
        )
        composer = HSplit(
            [
                Window(height=1, char=" ", style="class:input-frame"),
                input_row,
                Window(height=1, char=" ", style="class:input-frame"),
            ],
            style="class:input-frame",
        )

        self._status_condition = Condition(
            lambda: self._streaming_entry is not None or self._pending_interrupt is not None
        )
        self._status_window = Window(
            FormattedTextControl(self._status_fragments),
            height=1,
            style="class:status",
        )
        status_block = ConditionalContainer(
            HSplit(
                [
                    Window(height=1),
                    self._status_window,
                    Window(height=1),
                ]
            ),
            filter=self._status_condition,
        )

        self._footer_window = Window(
            FormattedTextControl(self._footer_fragments),
            height=1,
            style="class:footer",
        )

        self._picker_condition = Condition(lambda: self._model_picker_active)
        picker_reserve = ConditionalContainer(
            Window(
                height=lambda: Dimension(
                    preferred=5 + min(12, max(3, len(self._model_picker_models)))
                ),
                char=" ",
                style="class:app",
            ),
            filter=self._picker_condition,
        )

        body = HSplit(
            [
                transcript_row,
                status_block,
                Window(height=1),
                picker_reserve,
                composer,
                self._footer_window,
                Window(char=" ", style="class:app"),
            ],
            style="class:app",
        )
        # Codex-style model selection surface: unframed title/subtitle, numbered
        # rows, and a short confirmation hint above the composer.
        picker_width = Dimension(min=32, preferred=68, max=88)
        self._picker_window = Window(
            FormattedTextControl(
                self._model_picker_fragments,
                get_cursor_position=self._picker_cursor,
                show_cursor=False,
            ),
            width=picker_width,
            height=lambda: Dimension(preferred=min(12, max(3, len(self._model_picker_models)))),
            wrap_lines=False,
            allow_scroll_beyond_bottom=True,
            style="class:model-picker",
        )
        picker_body = HSplit(
            [
                Window(
                    FormattedTextControl(self._model_picker_header_fragments),
                    width=picker_width,
                    height=3,
                    style="class:model-picker-header",
                ),
                self._picker_window,
                Window(
                    FormattedTextControl(self._model_picker_footer_fragments),
                    width=picker_width,
                    height=2,
                    style="class:model-picker-footer",
                ),
            ],
            style="class:model-picker",
        )
        picker_float = Float(
            # Sit above composer (3 rows), footer (1), and trailing app row (1).
            bottom=5,
            left=2,
            content=ConditionalContainer(
                picker_body,
                filter=self._picker_condition,
            ),
        )
        root = FloatContainer(
            content=body,
            floats=[
                Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=6)),
                picker_float,
            ],
        )
        bindings = KeyBindings()

        @bindings.add("enter")
        def _submit(event) -> None:
            text = self._input_buffer.text
            self._input_buffer.reset(append_to_history=bool(text.strip()))
            self._submit_text(text)

        @bindings.add("escape", "enter")
        def _newline(event) -> None:
            self._input_buffer.insert_text("\n")

        @bindings.add("c-c")
        def _interrupt_or_exit(event) -> None:
            if self._has_active_turn():
                self._active_task.cancel()
            else:
                event.app.exit()

        @bindings.add("escape", filter=self._picker_condition)
        def _picker_cancel(event) -> None:
            self._close_model_picker()
            event.app.invalidate()

        # 注：ESC 取消流式因 prompt_toolkit 的 escape,enter 序列冲突不可靠，
        # 流式取消用 c-c（_interrupt_or_exit 已处理）。

        @bindings.add("c-d")
        def _exit_on_empty(event) -> None:
            if not self._input_buffer.text:
                event.app.exit()

        @bindings.add("pageup")
        def _scroll_up(event) -> None:
            self._scroll_transcript(-10)
            event.app.invalidate()

        @bindings.add("pagedown")
        def _scroll_down(event) -> None:
            self._scroll_transcript(10)
            event.app.invalidate()

        @bindings.add("c-u")
        def _scroll_half_up(event) -> None:
            self._scroll_transcript(-12)
            event.app.invalidate()

        @bindings.add("c-f")
        def _scroll_half_down(event) -> None:
            self._scroll_transcript(12)
            event.app.invalidate()

        composer_arrow_filter = Condition(lambda: not self._model_picker_active)

        @bindings.add("up", filter=composer_arrow_filter)
        def _composer_up(event) -> None:
            # prompt_toolkit auto_up implements the desired priority:
            # completion popup -> multiline cursor -> submitted input history.
            event.current_buffer.auto_up()

        @bindings.add("down", filter=composer_arrow_filter)
        def _composer_down(event) -> None:
            event.current_buffer.auto_down()

        # 模型选择器键：仅 picker 激活时生效
        @bindings.add("up", filter=self._picker_condition)
        def _picker_up(event) -> None:
            if self._model_picker_models:
                self._model_picker_index = (self._model_picker_index - 1) % len(self._model_picker_models)
                self._scroll_picker_to_selected()
                event.app.invalidate()

        @bindings.add("down", filter=self._picker_condition)
        def _picker_down(event) -> None:
            if self._model_picker_models:
                self._model_picker_index = (self._model_picker_index + 1) % len(self._model_picker_models)
                self._scroll_picker_to_selected()
                event.app.invalidate()

        @bindings.add("enter", filter=self._picker_condition)
        def _picker_select(event) -> None:
            models = self._model_picker_models
            if models and 0 <= self._model_picker_index < len(models):
                mid = str(models[self._model_picker_index].get("id") or models[self._model_picker_index].get("name") or "")
                if mid:
                    self._apply_model_switch(mid)
                    self._close_model_picker()
                    self._ack(f"已切换到 model: {mid}（下一轮请求生效，不持久化）")
                    return
            self._close_model_picker()
            event.app.invalidate()

        layout = Layout(root)
        layout.focus(self._input_window)
        composer_surface = _codex_surface_style(_terminal_background())
        app = Application(
            layout=layout,
            key_bindings=bindings,
            full_screen=False,
            mouse_support=False,
            min_redraw_interval=0.05,
            cursor=CursorShape.BLINKING_BEAM,
            style=Style.from_dict(
                {
                    "app": "",
                    "transcript": "",
                    "prompt": f"bold {composer_surface}".strip(),
                    "input": composer_surface,
                    "input-frame": composer_surface,
                    "input-placeholder": f"dim {composer_surface}".strip(),
                    "footer": "ansigray",
                    "footer-warn": "ansired bold",
                    "status": "ansigray",
                    "status-bullet": "ansiwhite bold",
                    "system": "ansigray",
                    "welcome-border": "ansigray",
                    "model-picker": "",
                    "model-picker-header": "",
                    "model-picker-title": "bold",
                    "model-picker-subtitle": "ansigray",
                    "model-picker-item": "",
                    "model-picker-selected": "bold",
                    "model-picker-current": "bold",
                    "model-picker-footer": "ansigray",
                }
            ),
        )
        self._app = app
        self._refresh_transcript()
        return app

    # ---- 输入提交 / 轮次 ----

    def _submit_text(self, user_input: str) -> None:
        text = user_input.strip()
        if not text:
            return

        if self._pending_interrupt is not None:
            self._handle_interrupt_answer(text)
            return

        action = self._handle_command(text)
        if action == "quit":
            self._exit_app()
            return
        if action != "send":
            return

        if self._has_active_turn():
            # streaming 时输入 → 排队下一轮（user entry 标记 queued 后落定）
            queued_entry = TranscriptEntry(role="user", content=text, status="queued")
            self._queued_inputs.append(QueuedInput(text=text, entry=queued_entry))
            self._create_background_task(self._commit_entry(queued_entry))
            return

        self._start_turn(text)

    def _start_turn(
        self,
        user_input: str,
        *,
        user_entry: TranscriptEntry | None = None,
        input_data: dict[str, Any] | None = None,
        is_resume: bool = False,
    ) -> None:
        self._turn_started_at = time.monotonic()

        async def _run():
            if not is_resume:
                if user_entry is not None:
                    user_entry.status = ""
                    # 队列的 user_entry 在 _submit_text 排队时已落定，不重复 commit（否则重复显示）
                    if user_entry not in self._entries:
                        await self._commit_entry(user_entry)
                    else:
                        self._refresh_transcript()
                else:
                    await self._commit_entry(TranscriptEntry(role="user", content=user_input))
                self.history.append({"role": "user", "content": user_input})
            elif user_entry is not None:
                user_entry.status = ""

            turn_input = input_data or self._build_input_data(user_input)
            assistant_entry = TranscriptEntry(role="assistant", content="", status="streaming")
            self._streaming_entry = assistant_entry
            self._refresh_transcript()
            await self._run_turn_async(turn_input, assistant_entry, is_resume=is_resume)

        self._create_background_task(_run())
        self._ensure_status_refresh()

    async def _run_turn_async(
        self,
        input_data: dict[str, Any],
        assistant_entry: TranscriptEntry,
        *,
        is_resume: bool = False,
    ) -> None:
        renderer = RichLiveRenderer(
            self,
            assistant_entry,
            show_thinking=self.show_thinking,
        )
        try:
            response, usage = await render_stream(self.runner, input_data, renderer=renderer)
        except InterruptPending as exc:
            self._clear_streaming()
            assistant_entry.status = ""
            # 中断时仍按原始事件顺序保留已产生的文本和工具调用。
            for entry in renderer.final_entries(renderer._last_full_text):
                await self._commit_entry(entry)
            if is_resume:
                await self._commit_entry(TranscriptEntry(role="system", content="该 runtime 暂不支持审批续跑，已取消"))
                self._clear_current_task()
                self._drain_queue()
            else:
                self._handle_interrupt(exc, input_data)
            return
        except asyncio.CancelledError:
            # 取消流式：保留已产生内容，并维持文本/工具的原始到达顺序。
            self._clear_streaming()
            assistant_entry.status = ""
            for entry in renderer.final_entries(renderer._last_full_text):
                await self._commit_entry(entry)
            await self._commit_entry(TranscriptEntry(role="system", content="已取消（保留已产生内容）"))
            self._clear_current_task()
            self._drain_queue()
            return
        finally:
            self._clear_current_task()

        assistant_entry.status = ""
        assistant_entry.content = response
        if usage:
            assistant_entry.usage = usage
            self._last_usage = usage
        self._clear_streaming()
        if response or assistant_entry.thinking:
            self.history.append({"role": "model", "content": response})
        for entry in renderer.final_entries(response):
            await self._commit_entry(entry)
        self._drain_queue()

    def _handle_interrupt(self, exc: InterruptPending, input_data: dict[str, Any]) -> None:
        self._pending_interrupt = (exc, input_data)
        info = exc.interrupt_info or {}
        tool_name = str(info.get("name") or info.get("server_label") or "敏感操作")

        async def _prompt():
            await self._commit_entry(
                TranscriptEntry(role="system", content=f"确认执行 {tool_name}? 输入 y 确认，其他内容取消")
            )

        self._create_background_task(_prompt())

    def _handle_interrupt_answer(self, user_input: str) -> None:
        pending = self._pending_interrupt
        self._pending_interrupt = None
        if pending is None:
            return
        _exc, input_data = pending
        if user_input.lower() in {"y", "yes"}:

            async def _resume():
                await self._commit_entry(TranscriptEntry(role="system", content="已确认，尝试续跑..."))
                self._start_turn(
                    "",
                    input_data={**input_data, "resume": True},
                    is_resume=True,
                )

            self._create_background_task(_resume())
        else:

            async def _cancel():
                await self._commit_entry(TranscriptEntry(role="system", content="已取消"))
                self._drain_queue()

            self._create_background_task(_cancel())

    def _drain_queue(self) -> None:
        if self._pending_interrupt is not None or self._has_active_turn() or not self._queued_inputs:
            return
        item = self._queued_inputs.pop(0)
        self._start_turn(item.text, user_entry=item.entry)

    def _build_input_data(self, user_input: str) -> dict[str, Any]:
        data: dict[str, Any] = {
            "input": user_input,
            "session_id": self.session_id,
            "history": list(self.history),
        }
        if str(getattr(self.runner, "api_format", "") or "").lower() == "responses":
            data["responses_conversation"] = True
        return data

    def _has_active_turn(self) -> bool:
        return self._active_task is not None and not self._active_task.done()

    def _clear_current_task(self) -> None:
        current = asyncio.current_task()
        if self._active_task is current:
            self._active_task = None
            self._turn_started_at = None

    def _create_background_task(self, coro) -> None:
        if self._app is not None:
            self._active_task = self._app.create_background_task(coro)
        else:
            self._active_task = asyncio.create_task(coro)

    def _ensure_status_refresh(self) -> None:
        """Refresh elapsed-time status only while a turn is actively running."""
        if self._app is None or not self._app.is_running:
            return
        if self._status_refresh_task is None or self._status_refresh_task.done():
            self._status_refresh_task = self._app.create_background_task(
                self._status_refresh_loop()
            )

    async def _status_refresh_loop(self) -> None:
        current_task = asyncio.current_task()
        try:
            while self._has_active_turn():
                await asyncio.sleep(0.5)
                if self._app is not None and self._app.is_running:
                    self._app.invalidate()
        finally:
            if self._status_refresh_task is current_task:
                self._status_refresh_task = None

    def _ack(self, content: str) -> None:
        """命令的系统提示落定到 transcript 列表。"""
        self._commit_entry_sync(TranscriptEntry(role="system", content=content))

    def _commit_entry_sync(self, entry: TranscriptEntry) -> None:
        """Record a history cell and schedule its one-time scrollback emission."""
        self._entries.append(entry)
        if self._app is not None and self._app.is_running:
            self._app.create_background_task(self._emit_history_entry(entry))
        self._refresh_transcript()

    async def _commit_entry(self, entry: TranscriptEntry) -> None:
        """Record and emit a history cell before resuming the live prompt."""
        self._entries.append(entry)
        await self._emit_history_entry(entry)
        self._refresh_transcript()

    def _handle_command(self, user_input: str) -> str:
        """分派命令。返回: 'quit'=退出, 'send'=发给 agent, 'handled'=命令已处理不发送。"""
        lower = user_input.lower()
        if lower in {"exit", "quit", "退出"}:
            return "quit"
        if user_input == "/new":
            if self._has_active_turn():
                self._ack("回复进行中，请先 Ctrl-C 取消再 /new")
                return "handled"
            self.session_id = uuid.uuid4().hex[:8]
            self.runner.session_id = self.session_id
            self.history = []
            self._queued_inputs = []
            self._pending_interrupt = None
            self._entries = []
            self._entry_ansi_cache.clear()
            self._welcome_ansi = None
            self._history_pager_active = False
            self._reset_scroll()
            self._clear_streaming()
            self._ack(f"新会话: {self.session_id}")
        elif user_input == "/clear":
            if self._has_active_turn():
                self._ack("回复进行中，请先 Ctrl-C 取消再 /clear")
                return "handled"
            self._entries = []
            self._entry_ansi_cache.clear()
            self._history_pager_active = False
            self._reset_scroll()
            self._clear_streaming()
            self._refresh_transcript()
            if self._app is not None and self._app.is_running:
                self._create_background_task(self._clear_terminal_scrollback())
        elif user_input == "/session":
            self._ack(f"session: {self.session_id}")
        elif user_input == "/model" or user_input.startswith("/model "):
            self._handle_model_command(user_input)
        elif user_input == "/tools":
            self._show_tool_details = not self._show_tool_details
            self._entry_ansi_cache.clear()
            self._ack(f"工具详情已{'展开' if self._show_tool_details else '折叠'}")
        elif user_input in {"?", "/help", "/?"}:
            self._ack(_help_text())
        elif user_input.startswith("/"):
            self._ack(f"未知命令: {user_input}（可用: /new /clear /session /model /tools ? exit）")
        else:
            return "send"  # 普通输入，发给 agent
        return "handled"

    def _handle_model_command(self, user_input: str) -> None:
        """/model:弹出交互式选择器(↑↓ Enter Esc)；/model <name>:直接切换(runnable 级,下轮生效)。"""
        arg = user_input[len("/model"):].strip()
        if arg:
            # 直接切换：改 runner.model，下轮请求带新 model
            self._apply_model_switch(arg)
            self._ack(f"已切换到 model: {arg}（下一轮请求生效，不持久化）")
            return
        # 无参：有可选列表 → 弹交互式选择器；否则文本提示
        available = getattr(self.runner, "available_models", None) or []
        if available:
            self._open_model_picker(available)
        else:
            self._ack(
                f"current model: {self._current_model_name()}\n"
                "（无可选模型列表；回复一次后显示真实名）\n"
                "切换: /model <name>"
            )

    def _apply_model_switch(self, model_id: str) -> None:
        """切换模型：改 runner.model + 从 available_models 更新 model_metadata。

        更新 metadata 让 footer 的 context window 用新模型的值；清 _observed_model
        避免旧回包观察名覆盖。runnable 级，下轮请求带新 model。
        """
        self.runner.model = model_id
        available = getattr(self.runner, "available_models", None) or []
        matched = next((m for m in available if str(m.get("id") or m.get("name") or "") == model_id), None)
        if matched:
            self.runner.model_metadata = matched
        # 清旧观察名，让 _current_model_name 用新 runner.model
        if getattr(self.runner, "_observed_model", None):
            self.runner._observed_model = None

    def _open_model_picker(self, models: list[dict[str, Any]]) -> None:
        self._model_picker_models = list(models)
        current = self._current_model_name()
        self._model_picker_index = 0
        for i, m in enumerate(self._model_picker_models):
            mid = str(m.get("id") or m.get("name") or "")
            if mid == current:
                self._model_picker_index = i
                break
        self._model_picker_active = True
        self._scroll_picker_to_selected()
        if self._app is not None:
            self._app.invalidate()

    def _close_model_picker(self) -> None:
        self._model_picker_active = False
        self._model_picker_models = []
        if self._picker_window is not None:
            self._picker_window.vertical_scroll = 0
        if self._app is not None:
            self._app.invalidate()

    def _scroll_picker_to_selected(self) -> None:
        """让选中项始终在 picker 可见区内（列表超长时跟随滚动）。

        picker 窗口封顶 12 行（见 _build_application 的 height lambda）。render_info
        首帧可能为 None，按 12 估算可见高度；渲染后 Window 会用设的 vertical_scroll。
        """
        w = self._picker_window
        if w is None or not self._model_picker_models:
            return
        ri = getattr(w, "render_info", None)
        visible = int(getattr(ri, "window_height", 0) or 0) or 12
        # 选中项偏上 1/3 处可见，避免紧贴边缘
        target = max(0, self._model_picker_index - max(1, visible // 3))
        max_scroll = max(0, len(self._model_picker_models) - visible)
        w.vertical_scroll = min(target, max_scroll)

    def _picker_cursor(self):
        """cursor 跟随选中项行号，驱动 Window 滚动让选中项可见。"""
        from prompt_toolkit.layout.screen import Point

        return Point(x=0, y=self._model_picker_index)

    def _model_picker_header_fragments(self):
        return [
            ("class:model-picker-title", "Select Model and Effort\n"),
            (
                "class:model-picker-subtitle",
                "Access legacy models by running codex -m <model_name> or in your config.toml\n\n",
            ),
        ]

    def _model_picker_footer_fragments(self):
        return [("class:model-picker-footer", "\nPress enter to confirm or esc to go back")]

    def _model_picker_fragments(self):
        """Codex-style numbered list with a selection chevron and current marker."""
        from prompt_toolkit.formatted_text import FormattedText

        current = self._current_model_name()
        frags: list[tuple[str, str]] = []
        for i, m in enumerate(self._model_picker_models):
            mid = str(m.get("id") or m.get("name") or "")
            is_cur_model = (mid == current)
            is_selected = (i == self._model_picker_index)
            marker = "›" if is_selected else " "
            line = f"{marker} {i + 1}. {mid}"
            if is_cur_model:
                line += " (current)"
            style = "class:model-picker-selected" if is_selected else "class:model-picker-item"
            frags.append((style, f"{line}\n"))
        return FormattedText(frags) if frags else FormattedText([("", "(no models)")])

    # ---- streaming / transcript 渲染 ----

    def _set_streaming(self, full_text: str) -> None:
        """streaming 文本更新到当前 streaming entry（动态区 = transcript 末尾的 streaming entry）。"""
        if self._streaming_entry is not None:
            self._streaming_entry.content = full_text
            self._streaming_entry.status = "streaming"
        self._refresh_transcript()

    def _clear_streaming(self) -> None:
        self._streaming_entry = None
        self._refresh_transcript()

    def _refresh_transcript(self) -> None:
        """Render only the mutable live tail; completed history lives in scrollback."""
        parts: list[str] = []
        if self._history_pager_active:
            parts = [
                self._history_entry_ansi(entry).rstrip("\n")
                for entry in self._entries
                if self._history_entry_ansi(entry).strip()
            ]
        elif self._streaming_entry is not None and (
            self._streaming_entry.content or self._streaming_entry.status
        ):
            ansi = _render_entry_ansi(self._streaming_entry, show_thinking=self.show_thinking)
            if ansi.strip():
                parts.append(ansi.rstrip("\n"))
        self._transcript_ansi = ("\n\n".join(parts) + "\n") if parts else ""
        # follow 快照（对标 Codex is_scrolled_to_bottom）：若当前在底才跟新内容到底，
        # 用户翻走（vertical_scroll < max_scroll）则保持位置不覆盖。这样鼠标滚轮/键盘
        # 翻后不被 streaming 刷新拽回底部。
        if self._transcript_window is not None:
            # prompt_toolkit 的内建鼠标滚轮直接修改 Window.vertical_scroll，不会经过
            # _scroll_transcript。先用上一帧记录识别这种外部滚动，再决定是否跟底。
            current_vs = int(getattr(self._transcript_window, "vertical_scroll", 0) or 0)
            if current_vs != self._user_scroll:
                self._user_scroll = current_vs
                self._pin_to_bottom = current_vs >= max(0, self._last_max_scroll - 1)
            max_scroll = self._max_scroll()
            if self._pin_to_bottom:
                self._transcript_window.vertical_scroll = max_scroll
                self._user_scroll = max_scroll
            else:
                preserved = min(current_vs, max_scroll)
                self._transcript_window.vertical_scroll = preserved
                self._user_scroll = preserved
            self._last_max_scroll = max_scroll
        if self._app is not None:
            self._app.invalidate()

    def _transcript_fragments(self):
        from prompt_toolkit.formatted_text import ANSI

        return ANSI(self._transcript_ansi)

    def _transcript_cursor(self):
        """cursor 跟随当前 vertical_scroll（视口顶行）。

        这样 prompt_toolkit 的 do_scroll 不钳制（cursor 总在视口顶可见），
        手动设的 vertical_scroll 保留。pin bottom → 在 _refresh_transcript/
        _scroll_transcript 里设 vertical_scroll=max_scroll 跟底；用户翻 → 设 user_scroll。
        （经验证：cursor=末行 时 do_scroll 跟底 OK，但 cursor=中部行时视口不动 →
        用户翻页不生效。改用 cursor 跟随视口顶，手动设 vertical_scroll 控制位置。）
        """
        from prompt_toolkit.layout.screen import Point

        vs = int(getattr(self._transcript_window, "vertical_scroll", 0) or 0)
        # render_info belongs to the previous frame. During streaming finalize,
        # clear, or resize it can have more lines than the current fragments and
        # would make prompt_toolkit index past UIContent.get_line().
        line_count = self._transcript_line_count()
        return Point(x=0, y=min(vs, line_count - 1))

    def _transcript_line_count(self) -> int:
        """Return the logical line count of the fragments for the next frame."""
        return max(1, self._transcript_ansi.count("\n") + 1)

    def _max_scroll(self) -> int:
        """算 max_scroll：显示行数 - window_height。

        Line count must come from the current transcript. ``render_info`` is a
        snapshot of the previous frame and can be stale while streaming content
        is replaced or cleared. Window height still comes from the last render,
        with a terminal-size fallback before the first frame.
        """
        if self._transcript_window is None:
            return 0
        ri = getattr(self._transcript_window, "render_info", None)
        height = int(getattr(ri, "window_height", 0) or 0)
        if height <= 0:
            import shutil
            height = max(10, (shutil.get_terminal_size(fallback=(80, 24)).lines or 24) - 6)
        line_count = self._transcript_line_count()
        return max(0, line_count - height)

    def _reset_scroll(self) -> None:
        """/new /clear 时重置滚动状态到 pin bottom 顶部。"""
        self._user_scroll = 0
        self._pin_to_bottom = True
        self._last_max_scroll = 0
        if self._transcript_window is not None:
            self._transcript_window.vertical_scroll = 0

    def _scroll_transcript(self, delta: int) -> None:
        """Scroll the live tail or open the retained-history pager on demand."""
        if self._transcript_window is None:
            return
        if not self._history_pager_active and self._entries:
            if delta >= 0:
                return
            self._history_pager_active = True
            self._pin_to_bottom = True
            self._refresh_transcript()
        max_scroll = self._max_scroll()
        current = max_scroll if self._pin_to_bottom else self._user_scroll
        if self._history_pager_active and delta > 0 and current + delta >= max_scroll:
            self._history_pager_active = False
            self._reset_scroll()
            self._refresh_transcript()
            return
        new_scroll = max(0, min(current + delta, max_scroll))
        self._user_scroll = new_scroll
        self._pin_to_bottom = new_scroll >= max_scroll
        self._transcript_window.vertical_scroll = new_scroll
        if self._app is not None:
            self._app.invalidate()

    # ---- footer / 输入框高度 ----

    def _current_model_name(self) -> str:
        """动态取模型名：metadata 到达后（_fetch_tui_model_metadata 挂载）优先用真实名。"""
        return _resolve_model_name(self.runner)

    def _status_fragments(self):
        if self._pending_interrupt is not None:
            return [
                ("class:status-bullet", "• "),
                ("class:footer-warn", "Approval required (type y then Enter to confirm)"),
            ]
        elapsed = 0
        if self._turn_started_at is not None:
            elapsed = max(0, int(time.monotonic() - self._turn_started_at))
        queued = f" · {len(self._queued_inputs)} queued" if self._queued_inputs else ""
        return [
            ("class:status-bullet", "• "),
            ("class:status", f"Working ({elapsed}s · Ctrl-C to interrupt{queued})"),
        ]

    def _footer_fragments(self):
        parts = [self._current_model_name()]
        try:
            short = "~/" + str(self.project_dir.relative_to(Path.home()))
        except ValueError:
            short = str(self.project_dir)
        ctx = self._context_percent()
        if ctx:
            parts.append(ctx.split(" · ", 1)[0])
        parts.append(short)
        return [("class:footer", "  " + " · ".join(parts) + "  ")]

    def _context_percent(self) -> str | None:
        """上下文窗口剩余占比，对标 Codex CLI 的 "Context 87% left · 12.3K used · 200K window"。

        算法与 codex-rs/tui/src/token_usage.rs 一致：
        - tokens_in_context = last_usage.total_tokens（当前上下文总大小）
        - effective_window = context_window - BASELINE_TOKENS(12000)
        - used = max(0, total_tokens - BASELINE_TOKENS)
        - remaining% = (effective_window - used) / effective_window * 100
        BASELINE 折扣避免把 system prompt 等固定基线算进"已用"。
        返回 "Context {pct}% left · {used}K used · {window}K window"（· 分隔，大写 K）。
        """
        meta = getattr(self.runner, "model_metadata", None) or {}
        if not isinstance(meta, dict):
            return None
        window = meta.get("context_window_tokens") or meta.get("max_input_tokens")
        last = self._last_usage or {}
        if not isinstance(last, dict):
            return None
        ctx_usage = last.get("last_usage") or last
        if not isinstance(ctx_usage, dict):
            return None
        tokens_in_context = ctx_usage.get("total_tokens") or ctx_usage.get("input_tokens")
        if not window or not tokens_in_context:
            return None
        try:
            window = int(window)
            tokens_in_context = int(tokens_in_context)
        except (TypeError, ValueError):
            return None
        if window <= 0:
            return None
        # 直接 used/window（不去 Codex BASELINE 12000：runtime 的 total_tokens 已含
        # 全部上下文，不需要再假设 12K 基线；BASELINE 会让小用量卡 100%）。
        # int() 不 round，避免 99.99% 进 100%。
        used = max(0, tokens_in_context)
        remaining = max(0, window - used)
        pct = int(remaining / window * 100)
        pct = max(0, min(100, pct))
        return (
            f"Context {pct}% left · {_format_token_count(tokens_in_context)} used"
            f" · {_format_token_count(window)} window"
        )

    def _input_display_height(self) -> int:
        if self._input_buffer is None:
            return 1
        try:
            import math
            import shutil

            from prompt_toolkit.utils import get_cwidth

            columns = max(20, shutil.get_terminal_size(fallback=(80, 24)).columns - 3)
            line_count = 0
            for line in (self._input_buffer.text or "").split("\n"):
                line_count += max(1, math.ceil(get_cwidth(line) / columns))
            return max(1, min(3, line_count))
        except Exception:
            return max(1, min(3, (self._input_buffer.text or "").count("\n") + 1))

    def _exit_app(self) -> None:
        if self._app is not None:
            self._app.exit()

    async def _clear_terminal_scrollback(self) -> None:
        if self._app is None or not self._app.is_running:
            return
        from prompt_toolkit.application import run_in_terminal

        def _write_clear() -> None:
            import sys

            sys.stdout.write(_terminal_clear_sequence())
            sys.stdout.flush()

        await run_in_terminal(_write_clear)


def _resolve_model_name(runner) -> str:
    import os

    # 优先 model_metadata["id"]（catalog fetch），其次 _observed_model（流式回包观察到的真实名），
    # 其次 runner.model（用户 --model 指定），最后 MODEL_NAME 环境变量。
    meta = getattr(runner, "model_metadata", None)
    if isinstance(meta, dict) and meta.get("id"):
        return str(meta["id"])
    observed = getattr(runner, "_observed_model", None)
    if observed:
        return observed
    return getattr(runner, "model", None) or os.getenv("MODEL_NAME") or "unknown"


def _help_text() -> str:
    return "命令: /new 新会话 · /clear 清屏 · /session 查看 · /tools 展开工具 · exit 退出 · Ctrl-C 中断/退出"


def _welcome_block(session_id: str, model_name: str, project_dir: Path, *, show_help: bool) -> str:
    """Render the compact Codex-style session card and one-line tip."""
    try:
        from ksadk.version import VERSION
        version = VERSION
    except Exception:
        version = ""
    try:
        short = "~/" + str(project_dir.relative_to(Path.home()))
    except ValueError:
        short = str(project_dir)
    title = f">_ KsADK (v{version})" if version else ">_ KsADK"
    inner_lines = [
        title,
        "",
        f"model:     {model_name}   /model to change",
        f"directory: {short}",
        f"session:   {session_id}",
    ]
    # 按终端宽度算框宽，留 transcript 左缩进 2 + 右 margin 2，上限 56（Codex 同款）。
    try:
        import shutil
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    except Exception:
        cols = 80
    max_inner = max(20, cols - 2 - 2 - 2)  # 减去左缩进2 + 两侧│ + 右margin2
    longest = max(len(s) for s in inner_lines)
    inner = min(max_inner, max(20, longest))  # 内容能放下且不超终端
    inner_lines = [s[:inner] for s in inner_lines]  # 防溢出截断

    def _pad(s: str) -> str:
        # CJK 宽度近似：用 len 简化（圆角框对齐以纯文本宽度为准，rich 不再二次渲染框线）
        return s + " " * max(0, inner - len(s))

    # Middle rows include one padding cell on both sides of the content, so
    # the horizontal rules must span ``inner + 2`` cells as well.
    outer_inner = inner + 2
    top = f"╭{'─' * outer_inner}╮"
    bottom = f"╰{'─' * outer_inner}╯"
    mid = [f"│ {_pad(line)} │" for line in inner_lines]
    block = [top, *mid, bottom]

    lines = ["", *block, ""]
    if show_help:
        lines.append("Tip: describe a task, or type /help to see available commands.")
    lines.append("")
    return "\n".join(lines)


def _normalize_markdown(source: str) -> str:
    """Normalize common model markdown typos without touching fenced code."""
    import re

    source_lines = str(source).replace("\r\n", "\n").split("\n")
    if (
        len(source_lines) >= 2
        and source_lines[0].strip().lower() in {"```md", "```markdown"}
        and source_lines[-1].strip() == "```"
    ):
        source_lines = source_lines[1:-1]

    lines: list[str] = []
    in_fence = False
    for raw_line in source_lines:
        line = raw_line
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            lines.append(line)
            continue
        if not in_fence:
            # Models frequently emit ``###1.`` for numbered cards. Treat it as
            # a list item, otherwise Rich displays the hashes literally.
            line = re.sub(r"^(\s*)#{1,6}\s*(\d+)[.)]\s*", r"\1\2. ", line)
            # Common heading/list omissions: ``##标题`` and ``1) item``.
            line = re.sub(r"^(\s*)(#{1,6})(\S)", r"\1\2 \3", line)
            line = re.sub(r"^(\s*)(\d+)[)]\s*", r"\1\2. ", line)
            # Some model responses concatenate numbered emoji cards on one line
            # (``...技能2. 📄 ...3. 🌐 ...``). Split only when the marker is
            # followed by an icon/CJK lead, avoiding decimal numbers in prose.
            lead = r"[\u2600-\u27bf\U0001f000-\U0001faff\u4e00-\u9fff]"
            line = re.sub(
                rf"(?<=[^\d\s])\s*(?=\d{{1,2}}[.)]?\s+{lead})",
                "\n",
                line,
            )
            line = re.sub(
                rf"^(\s*)(\d{{1,2}})\s+(?={lead})",
                r"\1\2. ",
                line,
                flags=re.MULTILINE,
            )
        lines.append(line)
    return "\n".join(lines)


def _render_entry_ansi(
    entry: TranscriptEntry,
    *,
    show_thinking: bool,
    show_tool_details: bool = True,
) -> str:
    """用 rich 把单条 entry 渲染成带前景色的 ANSI 字符串（去背景，适配浅/深色终端）。"""
    try:
        from rich.console import Console
        from rich.markdown import Markdown
        from rich.text import Text
    except ImportError:
        return _render_entry_plain(entry, show_thinking=show_thinking)

    import io
    import shutil

    width = 100
    try:
        width = max(60, shutil.get_terminal_size(fallback=(100, 24)).columns - 2)
    except Exception:
        pass

    console = Console(
        color_system="standard",
        force_terminal=True,
        file=io.StringIO(),
        record=True,
        width=width,
        legacy_windows=False,
    )
    body = entry.content or ("..." if entry.role == "assistant" and entry.status else "")

    if entry.role == "system":
        if body:
            console.print(Text(str(body), style="dim"))
        return console.export_text(styles=True).rstrip() + "\n"

    if entry.role == "separator":
        # tool 与 assistant 间横线分隔（对标 Codex FinalMessageSeparator）
        console.print(Text(str(body or "─" * 40), style="dim"))
        return console.export_text(styles=True).rstrip() + "\n"

    if entry.role == "user":
        suffix = f" · {entry.status}" if entry.status else ""
        surface = _codex_surface_rgb(_terminal_background())
        surface_style = "bold"
        if surface is not None:
            surface_style += f" on #{surface[0]:02x}{surface[1]:02x}{surface[2]:02x}"
        lines = str(body).splitlines() or [""]
        for index, line in enumerate(lines):
            prefix = "› " if index == 0 else "  "
            text = Text(f"{prefix}{line}{suffix if index == 0 else ''}", style=surface_style)
            text.pad_right(max(0, width - text.cell_len))
            console.print(text, no_wrap=True, overflow="crop")
        return console.export_text(styles=True).rstrip() + "\n"

    if entry.role == "assistant":
        # streaming 状态由 footer 表达，不在内容里加 "· streaming" 标记
        suffix = f" · {entry.status}" if entry.status and entry.status != "streaming" else ""
        if entry.thinking and show_thinking:
            console.print(Text("* thinking", style="yellow"))
            console.print(Markdown(_normalize_markdown(entry.thinking)))
        console.print(Text("• ", style="dim"), end="")
        if body:
            if entry.status == "streaming":
                # streaming 期用纯文本（不 rich Markdown），避免每 token 重渲增长的长内容卡顿。
                # 落定后（status=""）才 Markdown 渲染完整格式（表格/代码块等）。
                console.print(Text(str(body)))
            else:
                console.print(Markdown(_normalize_markdown(f"{body}{suffix}")))
        else:
            console.print(Text(suffix.lstrip(), style="dim") if suffix else Text("...", style="dim"))
        return _strip_ansi_backgrounds(console.export_text(styles=True)).rstrip() + "\n"

    if entry.role == "tool":
        # 对标 Codex exec_cell：单 bullet 表状态（running dim/完成绿），文字
        # Running→Ran + 工具名。结果折叠 5 行 head+… +N lines+tail。
        parts = (entry.content or "").split("\n", 1)
        tool_name = parts[0].split(" [")[0] if parts else ""
        output = parts[1] if len(parts) > 1 else ""
        status = entry.status or ""
        done = status in {"result", "completed"}
        bullet = Text("• ", style="green bold" if done else "dim")
        title = "Ran" if done else "Running"
        console.print(bullet, end="")
        console.print(Text(f"{title} {tool_name}", style="bold" if done else ""))
        if output and show_tool_details:
            # 尝试 JSON 美化（单行长 JSON → 多行可读），再按显示行数折叠
            display_output = output
            try:
                import json as _json
                parsed = _json.loads(output)
                # Tool adapters often JSON-encode an already serialized result.
                # Unwrap that second layer so structured output folds by fields
                # instead of rendering as one escaped line.
                if isinstance(parsed, str):
                    nested = parsed.strip()
                    # Some adapters wrap the JSON in a repr-like
                    # ``content='...' name='...'`` envelope.
                    if nested.startswith("content="):
                        import ast as _ast

                        quote = nested[len("content="):len("content=") + 1]
                        if quote in {"'", '"'}:
                            body_start = len("content=") + 1
                            body_end = body_start
                            while body_end < len(nested):
                                if nested[body_end] == quote:
                                    backslashes = 0
                                    cursor = body_end - 1
                                    while cursor >= body_start and nested[cursor] == "\\":
                                        backslashes += 1
                                        cursor -= 1
                                    if backslashes % 2 == 0:
                                        break
                                body_end += 1
                            try:
                                nested = _ast.literal_eval(
                                    quote + nested[body_start:body_end] + quote
                                ).strip()
                            except (SyntaxError, ValueError):
                                pass
                    if nested.startswith(("{", "[")):
                        parsed = _json.loads(nested)
                display_output = _json.dumps(parsed, ensure_ascii=False, indent=2)
            except Exception:
                pass
            # 估算显示行数：每行按 console width 折算（长字符串占多行）
            import shutil as _su
            cw = max(40, (_su.get_terminal_size(fallback=(80, 24)).columns or 80) - 6)
            logical_lines = display_output.split("\n")
            # 估算显示行数：按显示宽度（CJK 占 2）折算，非 len
            from prompt_toolkit.utils import get_cwidth

            def _width(s: str) -> int:
                return get_cwidth(s)

            logical_lines = display_output.split("\n")
            display_line_count = sum(max(1, -(-_width(ln) // cw)) for ln in logical_lines)
            # 逻辑行 > 6 才 head/tail 折叠（避免逻辑行少时 head/tail 重叠重复）；
            # 逻辑行 ≤ 6 但显示行长（长字符串）→ 不折叠，靠每行截断控制宽度。
            if display_line_count > 8 and len(logical_lines) > 6:
                head, tail = logical_lines[:3], logical_lines[-3:]
                shown = head + [f"… +{display_line_count-6} lines (PgUp to scroll)"] + tail
            else:
                shown = logical_lines
            for ln in shown:
                # 长逻辑行按显示宽度截断（CJK 占 2，避免溢出）
                if _width(ln) > cw:
                    # 逐步截断到 cw 宽度
                    cut = cw - 1
                    while cut > 0 and _width(ln[:cut]) > cw - 1:
                        cut -= 1
                    ln = ln[:cut] + "…"
                console.print(Text(f"  └ {ln}", style="dim"))
        return console.export_text(styles=True).rstrip() + "\n"

    if entry.role == "error":
        console.print(Text(f"! {body}", style="red bold"))
        return console.export_text(styles=True).rstrip() + "\n"

    if body:
        console.print(Text(str(body)))
    return console.export_text(styles=True).rstrip() + "\n"


def _render_entry_plain(entry: TranscriptEntry, *, show_thinking: bool) -> str:
    """rich 不可用时的纯文本回退。"""
    body = entry.content or ("..." if entry.role == "assistant" and entry.status else "")
    if entry.thinking and show_thinking:
        body = f"Thinking:\n{entry.thinking}\n\n{body}".strip()
    suffix = f" · {entry.status}" if entry.status and entry.role in {"user", "assistant"} else ""
    if entry.role == "user":
        return _prefix_block(f"› {body}{suffix}", continuation="  ") + "\n"
    if entry.role == "assistant":
        return _prefix_block(f"• {body}{suffix}", continuation="  ") + "\n"
    if entry.role == "tool":
        return _prefix_block(f"↳ {body}", continuation="  ") + "\n"
    if entry.role == "error":
        return f"! {body}\n"
    return f"{body}\n"


def _prefix_block(text: str, *, continuation: str) -> str:
    lines = str(text).splitlines() or [""]
    rendered = [lines[0]]
    rendered.extend(f"{continuation}{line}" if line else "" for line in lines[1:])
    return "\n".join(rendered)


def _compact_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return str(value)


def _format_token_count(value: Any) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return str(number)


def _strip_ansi_backgrounds(text: str) -> str:
    """只剥 ANSI 背景色（40/48），保留前景色——避免浅色终端背景色糊住文字。"""
    import re

    def _clean(match: "re.Match[str]") -> str:
        params = match.group(1).split(";")
        cleaned: list[str] = []
        index = 0
        while index < len(params):
            code = params[index]
            if code == "40" or code == "49":
                index += 1
                continue
            if code == "48":
                mode = params[index + 1] if index + 1 < len(params) else ""
                if mode == "2":
                    index += 5
                    continue
                if mode == "5":
                    index += 3
                    continue
            cleaned.append(code)
            index += 1
        return f"\x1b[{';'.join(cleaned)}m" if cleaned else ""

    return re.sub(r"\x1b\[([0-9;]*)m", _clean, text)


def run_tui(runner, *, show_thinking: bool = False, project_dir: str = ".", no_alt_screen: bool = False) -> None:
    """TUI 入口：被 cmd_invoke._invoke_tui / cmd_run._run_custom 调用。

    ``no_alt_screen`` 作为兼容参数保留；TUI 现在默认使用 Codex-style inline viewport。
    """
    InteractionLoop(
        runner,
        show_thinking=show_thinking,
        project_dir=project_dir,
        no_alt_screen=no_alt_screen,
    ).run()
