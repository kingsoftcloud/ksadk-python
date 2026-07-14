"""Agent TUI based on a full-screen prompt_toolkit application.

Transcript 使用 ANSI 保留 rich 颜色和 Markdown 落定格式，底部提供可编辑输入框
和状态栏。支持键盘/鼠标滚动、流式跟底、输入排队及 ``--no-alt-screen``。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from prompt_toolkit.completion import Completer
from ksadk.tui.stream_render import clean_response, extract_stream_delta


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

    streaming 期间：assistant 文本 + 到达的 tool 行一起在动态区累积显示
    （tool 就地出现在文本流里，用户能看到工具在跑）。
    turn 结束：整条 assistant 文本落定为 assistant entry，各 tool 行按到达
    顺序追加落定为 tool entries（穿插位置以 streaming 期动态区为准，落定后
    tool 跟在 assistant 文本之后——对“先思考再调工具”的 agent 顺序自然）。
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

    def _compose_streaming(self) -> str:
        parts = []
        if self.assistant_entry is not None and self.assistant_entry.content:
            parts.append(self.assistant_entry.content)
        for te in self._tool_entries:
            # streaming 期 tool 就地显示（对标 Codex 单 bullet）：• tool_name [status]
            name = (te.content or "").split("\n", 1)[0]
            parts.append(f"• {name}")
        return "\n".join(parts)

    async def on_text(self, full_text: str) -> None:
        if self.loop is not None and self.assistant_entry is not None:
            self.assistant_entry.content = full_text
            self.assistant_entry.status = "streaming"
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
        replaced = False
        for i, te in enumerate(self._tool_entries):
            existing_key = te._tool_call_id or (te.content.split(" [")[0] if " [" in te.content else te.content)
            if existing_key == merge_key:
                self._tool_entries[i] = entry
                replaced = True
                break
        if not replaced:
            self._tool_entries.append(entry)
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


class InteractionLoop:
    """Full-screen prompt_toolkit interaction loop."""

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
        self._entry_ansi_cache: dict[int, str] = {}  # 历史 entries ANSI 缓存（避免 streaming 全量重渲）
        self._last_usage: dict[str, Any] | None = None
        self._showed_help = False  # 首次运行显示帮助列表
        # 交互式模型选择器状态
        self._model_picker_active = False
        self._model_picker_index = 0
        self._model_picker_models: list[dict[str, Any]] = []

    def run(self) -> None:
        asyncio.run(self.run_async())

    async def run_async(self) -> None:
        app = self._build_application()
        await app.run_async()

    def _build_application(self):
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import (
            Float,
            FloatContainer,
            HSplit,
            VSplit,
            Window,
        )
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.layout.menus import CompletionsMenu
        from prompt_toolkit.styles import Style

        self._input_buffer = Buffer(
            completer=SlashCommandCompleter(runner=self.runner),
            complete_while_typing=True,
            multiline=True,
            history=InMemoryHistory(),
        )

        # transcript 区：FormattedTextControl + ANSI 保留 rich 颜色。wrap_lines=True
        # 下 prompt_toolkit 用 cursor 驱动滚动（_scroll_when_linewrapping），get_vertical_scroll
        # 回调在该模式不生效。用 get_cursor_position：pin bottom → cursor 末行（跟底），
        # 用户翻上去 → cursor 视口顶（保持）。左缩进 2 列。
        # transcript：wrap_lines=False（rich 已按窗口 width 预折行）+ get_vertical_scroll
        # transcript：wrap_lines=True（prompt_toolkit 按当前宽度折行，resize 适配）+
        # get_cursor_position 驱动滚动（pin bottom → cursor 末行跟底；用户翻 → cursor 视口顶）。
        # wrap_lines=False 会让 rich 固定 width 折行，resize 缩小内容消失，不可用。
        self._transcript_window = Window(
            FormattedTextControl(
                self._transcript_fragments,
                get_cursor_position=self._transcript_cursor,
                show_cursor=False,
            ),
            wrap_lines=True,
            allow_scroll_beyond_bottom=True,
            style="class:transcript",
        )
        self._transcript_window_left = Window(width=2, char=" ", style="class:transcript")
        transcript_row = VSplit([self._transcript_window_left, self._transcript_window])

        # 输入框上分隔线（可视分隔 transcript 与输入框，对标 Codex bottom pane 边界）
        separator = Window(char="─", height=1, style="class:separator")

        # 输入框：左 › 提示符 + BufferControl。高度 = 文字行数（min 1），不强制 min 3
        # （Codex 的 3 行含 padding；我们用分隔线代替上 padding，故 1 行内容即可）。
        prompt_window = Window(
            FormattedTextControl(lambda: [("class:prompt", "› ")]),
            width=2,
            dont_extend_width=True,
        )
        # 输入框高度 = 文字行数（min 1,封顶 4），max 紧贴 preferred，避免 HSplit 在
        # transcript 内容少时把多余空间塞给输入框撑高它（Codex 输入框也只随内容长）。
        self._input_height = lambda: Dimension(min=1, preferred=self._input_display_height(), max=max(1, self._input_display_height()))
        input_window = Window(
            BufferControl(buffer=self._input_buffer),
            height=self._input_height,
            wrap_lines=True,
            style="class:input",
        )
        input_row = VSplit([prompt_window, input_window], style="class:input-frame")

        # 输入框下分隔线（输入框与 footer 之间的可视边界，和上分隔线对称）
        separator_bottom = Window(char="─", height=1, style="class:separator")

        # footer：模型 · 目录 · session · streaming · Context n% left · used · window
        footer = Window(
            FormattedTextControl(self._footer_fragments),
            height=1,
            style="class:footer",
        )

        body = HSplit(
            [
                transcript_row,
                separator,
                input_row,
                separator_bottom,
                footer,
            ],
            style="class:app",
        )
        # 模型选择器浮层：ConditionalContainer 只在 _model_picker_active 时显示
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.layout.containers import ConditionalContainer
        from prompt_toolkit.widgets import Frame

        self._picker_condition = Condition(lambda: self._model_picker_active)
        # picker 窗口：get_cursor_position 跟随选中项行号，让 Window 自动把视口滚到
        # 选中项可见（同 transcript 的 cursor 驱动滚动原理）。封顶 8 行。
        self._picker_window = Window(
            FormattedTextControl(
                self._model_picker_fragments,
                get_cursor_position=self._picker_cursor,
                show_cursor=False,
            ),
            width=44,
            height=lambda: Dimension(preferred=min(8, max(3, len(self._model_picker_models) + 2))),
            wrap_lines=False,
            allow_scroll_beyond_bottom=True,
            style="class:model-picker",
        )
        picker_float = Float(
            # 贴输入框上方：距底部 2 行（footer 1 + 下分隔线 1），左侧缩进 2 对齐 ›
            bottom=2,
            left=2,
            content=ConditionalContainer(
                Frame(self._picker_window, title="select model (↑↓ Enter Esc)"),
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
            self._input_buffer.text = ""
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

        # 输入为空时 ↑/↓ 逐行滚 transcript；输入非空时保留 Buffer 的光标/历史行为。
        transcript_arrow_filter = Condition(
            lambda: not self._model_picker_active and not self._input_buffer.text
        )

        @bindings.add("up", filter=transcript_arrow_filter)
        def _scroll_line_up(event) -> None:
            self._scroll_transcript(-1)
            event.app.invalidate()

        @bindings.add("down", filter=transcript_arrow_filter)
        def _scroll_line_down(event) -> None:
            self._scroll_transcript(1)
            event.app.invalidate()

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
        layout.focus(input_window)
        app = Application(
            layout=layout,
            key_bindings=bindings,
            full_screen=not getattr(self, "_no_alt_screen", False),
            mouse_support=False,  # 避免 mouse_support=True 捕获鼠标导致终端原生选择/复制失效；滚动用 PageUp/PgDn/↑↓
            style=Style.from_dict(
                {
                    "app": "",
                    "transcript": "",
                    "prompt": "ansicyan bold",
                    "input": "",
                    "input-frame": "",
                    "separator": "ansibrightblack",
                    "footer": "ansigray",
                    "footer-warn": "ansired bold",
                    "system": "ansigray",
                    "welcome-border": "ansigray",
                    "model-picker": "bg:ansiblue",
                    "model-picker-item": "",
                    "model-picker-selected": "bg:ansicyan fg:ansiblack bold",
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
            # 中断时仍把已流式到达的 tool 调用落定（用户能看到中断前发生了什么）。
            for te in renderer._tool_entries:
                await self._commit_entry(te)
            if is_resume:
                await self._commit_entry(TranscriptEntry(role="system", content="该 runtime 暂不支持审批续跑，已取消"))
                self._clear_current_task()
                self._drain_queue()
            else:
                self._handle_interrupt(exc, input_data)
            return
        except asyncio.CancelledError:
            # 取消流式：保留已产生的 assistant 内容（落定），只停后续输出。
            # 先清动态区（_streaming_entry），再落定 assistant，避免 _refresh_transcript
            # 同时渲染已落定 entry + streaming entry 导致重复显示。
            self._clear_streaming()
            assistant_entry.status = ""
            if assistant_entry.content:
                await self._commit_entry(assistant_entry)
            for te in renderer._tool_entries:
                await self._commit_entry(te)
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
            await self._commit_entry(assistant_entry)
        # tool 调用按到达顺序落定（跟在 assistant 文本之后）。首个 tool 前插横线
        # 分隔符（对标 Codex FinalMessageSeparator：assistant 文本 → ─ → tool）。
        if renderer._tool_entries and (response or assistant_entry.thinking):
            await self._commit_entry(TranscriptEntry(role="separator", content="─" * 40))
        for te in renderer._tool_entries:
            await self._commit_entry(te)
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

    def _create_background_task(self, coro) -> None:
        if self._app is not None:
            self._active_task = self._app.create_background_task(coro)
        else:
            self._active_task = asyncio.create_task(coro)

    def _ack(self, content: str) -> None:
        """命令的系统提示落定到 transcript 列表。"""
        self._commit_entry_sync(TranscriptEntry(role="system", content=content))

    def _commit_entry_sync(self, entry: TranscriptEntry) -> None:
        """把一条 entry 追加到 transcript 列表并刷新（全屏渲染由 _refresh_transcript 统一）。"""
        self._entries.append(entry)
        self._refresh_transcript()

    async def _commit_entry(self, entry: TranscriptEntry) -> None:
        """async 包装（兼容 _run_turn_async 的 await 调用点）。"""
        self._commit_entry_sync(entry)

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
            self._reset_scroll()
            self._clear_streaming()
            self._ack(f"新会话: {self.session_id}")
        elif user_input == "/clear":
            if self._has_active_turn():
                self._ack("回复进行中，请先 Ctrl-C 取消再 /clear")
                return "handled"
            self._entries = []
            self._entry_ansi_cache.clear()
            self._reset_scroll()
            self._clear_streaming()
            self._refresh_transcript()
            self._ack("已清空当前会话视图")
        elif user_input == "/session":
            self._ack(f"session: {self.session_id}")
        elif user_input == "/model" or user_input.startswith("/model "):
            self._handle_model_command(user_input)
        elif user_input in {"?", "/help", "/?"}:
            self._ack(_help_text())
        elif user_input.startswith("/"):
            self._ack(f"未知命令: {user_input}（可用: /new /clear /session /model ? exit）")
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

        picker 窗口封顶 8 行（见 _build_application 的 height lambda）。render_info
        首帧可能为 None，按 8 估算可见高度；渲染后 Window 会用设的 vertical_scroll。
        """
        w = self._picker_window
        if w is None or not self._model_picker_models:
            return
        ri = getattr(w, "render_info", None)
        visible = int(getattr(ri, "window_height", 0) or 0) or 8
        # 选中项偏上 1/3 处可见，避免紧贴边缘
        target = max(0, self._model_picker_index - max(1, visible // 3))
        max_scroll = max(0, len(self._model_picker_models) - visible)
        w.vertical_scroll = min(target, max_scroll)

    def _picker_cursor(self):
        """cursor 跟随选中项行号，驱动 Window 滚动让选中项可见。"""
        from prompt_toolkit.layout.screen import Point

        return Point(x=0, y=self._model_picker_index)
    def _model_picker_fragments(self):
        """浮层列表：高亮当前选中项，当前模型标 *。"""
        from prompt_toolkit.formatted_text import FormattedText

        current = self._current_model_name()
        frags: list[tuple[str, str]] = []
        for i, m in enumerate(self._model_picker_models):
            mid = str(m.get("id") or m.get("name") or "")
            disp = str(m.get("display_name") or mid)
            is_cur_model = (mid == current)
            is_selected = (i == self._model_picker_index)
            marker = "▶" if is_selected else " "
            line = f"{marker} {mid}"
            if disp != mid:
                line += f"  {disp}"
            if is_cur_model:
                line += "  *"
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
        """渲染 entries + streaming entry → _transcript_ansi，刷新视图。

        历史 entries 落定后 content 不变，缓存其 ANSI（避免 streaming 每 token 全量重渲，
        导致满页后渲染卡顿/不输出）。streaming entry 每帧重渲。
        """
        parts: list[str] = []
        for entry in self._entries:
            if entry.role == "streaming":
                ansi = _render_entry_ansi(entry, show_thinking=self.show_thinking)
            else:
                key = id(entry)
                ansi = self._entry_ansi_cache.get(key)
                if ansi is None:
                    ansi = _render_entry_ansi(entry, show_thinking=self.show_thinking)
                    self._entry_ansi_cache[key] = ansi
            if ansi and ansi.strip():
                parts.append(ansi.rstrip("\n"))
        if self._streaming_entry is not None and (self._streaming_entry.content or self._streaming_entry.status):
            ansi = _render_entry_ansi(self._streaming_entry, show_thinking=self.show_thinking)
            if ansi.strip():
                parts.append(ansi.rstrip("\n"))
        # 首次运行显示 welcome 圆角框 + 帮助列表
        if not self._entries and not self._streaming_entry:
            parts.append(_welcome_block(self.session_id, self._current_model_name(), self.project_dir, show_help=not self._showed_help))
            self._showed_help = True
        self._transcript_ansi = "\n\n".join(parts) + "\n"
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
        # 显示行数（含折行）：优先 render_info.ui_content.line_count，否则逻辑行
        line_count = 0
        ri = getattr(self._transcript_window, "render_info", None)
        ui_content = getattr(ri, "ui_content", None) if ri else None
        if ui_content is not None:
            line_count = int(getattr(ui_content, "line_count", 0) or 0)
        if line_count <= 0:
            line_count = max(1, self._transcript_ansi.count("\n"))
        return Point(x=0, y=min(vs, line_count - 1))

    def _max_scroll(self) -> int:
        """算 max_scroll：显示行数 - window_height。

        优先用 render_info.ui_content.line_count（含折行的显示行数，准确）；
        否则回退逻辑行 _transcript_ansi.count。render_info 有值时用 window_height，
        否则按终端行数估算。
        """
        if self._transcript_window is None:
            return 0
        ri = getattr(self._transcript_window, "render_info", None)
        height = int(getattr(ri, "window_height", 0) or 0)
        if height <= 0:
            import shutil
            height = max(10, (shutil.get_terminal_size(fallback=(80, 24)).lines or 24) - 6)
        # 显示行数：render_info.ui_content.line_count（含折行），否则逻辑行
        line_count = 0
        ui_content = getattr(ri, "ui_content", None) if ri else None
        if ui_content is not None:
            line_count = int(getattr(ui_content, "line_count", 0) or 0)
        if line_count <= 0:
            line_count = max(1, self._transcript_ansi.count("\n"))
        return max(0, line_count - height)

    def _reset_scroll(self) -> None:
        """/new /clear 时重置滚动状态到 pin bottom 顶部。"""
        self._user_scroll = 0
        self._pin_to_bottom = True
        self._last_max_scroll = 0
        if self._transcript_window is not None:
            self._transcript_window.vertical_scroll = 0

    def _scroll_transcript(self, delta: int) -> None:
        """PageUp/PgDn 滚动。手动设 vertical_scroll（cursor 跟随视口顶，不钳制）。"""
        if self._transcript_window is None:
            return
        max_scroll = self._max_scroll()
        current = max_scroll if self._pin_to_bottom else self._user_scroll
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

    def _footer_fragments(self):
        if self._pending_interrupt is not None:
            return [("class:footer-warn", " approval pending: type y then Enter to confirm, else cancel ")]
        parts = [f"KsADK · {self._current_model_name()}"]
        try:
            short = "~/" + str(self.project_dir.relative_to(Path.home()))
        except ValueError:
            short = str(self.project_dir)
        parts.append(short)
        parts.append(f"session {self.session_id}")
        if self._has_active_turn():
            parts.append("streaming")
        if self._queued_inputs:
            parts.append(f"queued {len(self._queued_inputs)}")
        ctx = self._context_percent()
        if ctx:
            parts.append(ctx)
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
    return os.getenv("MODEL_NAME") or getattr(runner, "model", None) or "unknown"


def _help_text() -> str:
    return "命令: /new 新会话 · /clear 清屏 · /session 查看 · exit 退出 · Ctrl-C 中断/退出"


def _welcome_block(session_id: str, model_name: str, project_dir: Path, *, show_help: bool) -> str:
    """启动屏：圆角框（对标 Codex 首次运行 session.rs:32-72）含 model/session/dir/version，
    下方 2 空格缩进命令帮助列表。框宽按终端实际列数算，留左缩进2+右margin，不溢出。"""
    try:
        from ksadk.version import VERSION
        version = VERSION
    except Exception:
        version = ""
    try:
        short = "~/" + str(project_dir.relative_to(Path.home()))
    except ValueError:
        short = str(project_dir)
    title = f">_ KsADK v{version}" if version else ">_ KsADK"
    inner_lines = [
        title,
        "",
        f"model: {model_name}",
        f"session: {session_id}",
        f"directory: {short}",
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

    top = f"╭{'─' * inner}╮"
    bottom = f"╰{'─' * inner}╯"
    mid = [f"│ {_pad(line)} │" for line in inner_lines]
    block = [top, *mid, bottom]

    lines = ["", *block, ""]
    if show_help:
        lines.append("  To get started, describe a task or try:")
        lines.append("")
        for cmd, desc in [
            ("/new", "start a new session"),
            ("/clear", "clear transcript"),
            ("/session", "show session id"),
            ("/model", "show/switch model (/model <name>)"),
            ("exit", "quit"),
        ]:
            lines.append(f"  {cmd} - {desc}")
        lines.append("")
        lines.append("  Enter send · Esc+Enter newline · PgUp/PgDn scroll · Ctrl-C cancel/exit")
    lines.append("")
    return "\n".join(lines)


def _render_entry_ansi(entry: TranscriptEntry, *, show_thinking: bool) -> str:
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
        console.print(Text(f"› {body}{suffix}", style="bold"))
        return console.export_text(styles=True).rstrip() + "\n"

    if entry.role == "assistant":
        # streaming 状态由 footer 表达，不在内容里加 "· streaming" 标记
        suffix = f" · {entry.status}" if entry.status and entry.status != "streaming" else ""
        if entry.thinking and show_thinking:
            console.print(Text("* thinking", style="yellow"))
            console.print(Markdown(entry.thinking))
        console.print(Text("● ", style="cyan bold"), end="")
        if body:
            if entry.status == "streaming":
                # streaming 期用纯文本（不 rich Markdown），避免每 token 重渲增长的长内容卡顿。
                # 落定后（status=""）才 Markdown 渲染完整格式（表格/代码块等）。
                console.print(Text(str(body)))
            else:
                console.print(Markdown(f"{body}{suffix}"))
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
        if output:
            # 尝试 JSON 美化（单行长 JSON → 多行可读），再按显示行数折叠
            display_output = output
            try:
                import json as _json
                parsed = _json.loads(output)
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
        return _prefix_block(f"● {body}{suffix}", continuation="  ") + "\n"
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

    no_alt_screen=True 时不进 alternate screen（对标 Codex --no-alt-screen，保留 scrollback）。
    """
    InteractionLoop(
        runner,
        show_thinking=show_thinking,
        project_dir=project_dir,
        no_alt_screen=no_alt_screen,
    ).run()
