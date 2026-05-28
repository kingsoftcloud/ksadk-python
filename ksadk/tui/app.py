"""
KsADK Agent TUI - 简洁交互界面

基本功能：
- 对话交互
- 思考过程显示（通过 --show-thinking 参数控制）
- 工具调用显示
- 退出：输入 exit/quit 或 Ctrl+C/Ctrl+D
"""

from __future__ import annotations

import asyncio
import os
import uuid
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static
from textual.events import MouseUp

from rich.panel import Panel

from ksadk.tui.clipboard import copy_selection_to_clipboard
from ksadk.tui.widgets.user import UserMessage
from ksadk.tui.widgets.assistant import AssistantMessage
from ksadk.tui.widgets.thinking import ThinkingMessage
from ksadk.tui.widgets.system import SystemMessage
from ksadk.tui.widgets.chat_input import ChatInput

if TYPE_CHECKING:
    from ksadk.runners.base_runner import BaseRunner


def _clean_response(text: str) -> str:
    """清理 LLM 响应中的内部调试信息"""
    text = re.sub(r'\[Tool Result:.*?\]', '', text, flags=re.DOTALL)
    text = re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL)
    text = re.sub(r"name='[^']*'\s*tool_call_id='[^']*'", '', text)
    return text.strip()


class ApprovalScreen(ModalScreen[bool]):
    """敏感操作确认弹窗"""

    BINDINGS = [
        Binding("y", "approve", "确认"),
        Binding("n", "reject", "取消"),
        Binding("escape", "reject", "取消"),
    ]

    def __init__(self, tool_name: str, args: Dict[str, Any], **kwargs):
        super().__init__(**kwargs)
        self.tool_name = tool_name
        self.tool_args = args

    def compose(self) -> ComposeResult:
        args_str = "\n".join(f"  {k}: {v}" for k, v in self.tool_args.items()) if self.tool_args else "  (无参数)"
        yield Container(
            Static(
                Panel(
                    f"[bold yellow]⚠️  需要您确认敏感操作[/]\n\n"
                    f"[bold]操作:[/] {self.tool_name}\n"
                    f"[bold]参数:[/]\n{args_str}\n\n"
                    f"[green]y[/] 确认  [red]n[/] 取消",
                    title="🔒 确认",
                    border_style="yellow",
                ),
            ),
            id="approval-dialog",
        )

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_reject(self) -> None:
        self.dismiss(False)

    CSS = """
    #approval-dialog {
        align: center middle;
        width: 60;
        height: auto;
    }
    """


class AgentTUI(App):
    """KsADK Agent TUI - 简洁交互界面"""

    CSS = """
    Screen {
        background: $background;
    }

    #main-scroll {
        height: 100%;
        padding: 0 1;
    }

    #title-bar {
        height: 1;
        color: $primary;
        padding: 0;
    }

    #welcome-area {
        height: auto;
        padding: 2 0;
        content-align: center middle;
        text-align: center;
    }

    #chat-log {
        height: auto;
        background: transparent;
        padding: 0;
    }

    #input-area {
        height: auto;
        padding: 1 0;
    }

    #hint {
        height: 1;
        color: $text-muted;
        padding: 0 0 0 2;
    }

    .started #welcome-area {
        display: none;
    }
    """

    BINDINGS = [
        Binding("escape", "interrupt", "中断", show=False, priority=True),
        Binding("ctrl+c", "quit_or_interrupt", "退出/中断", show=False),
        Binding("ctrl+d", "quit_app", "退出", show=False, priority=True),
        Binding("ctrl+q", "quit_app", "退出", show=False),
    ]

    TITLE = "KsADK"

    def __init__(
        self,
        runner: "BaseRunner",
        show_thinking: bool = False,
        project_dir: str = ".",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.runner = runner
        self.show_thinking = show_thinking
        self.project_dir = Path(project_dir).resolve()

        self.session_id = getattr(runner, "session_id", None) or str(uuid.uuid4())[:8]
        if getattr(self.runner, "session_id", None) is None:
            self.runner.session_id = self.session_id
        self.history: List[Dict[str, str]] = []
        self._is_streaming = False
        self._started = False
        self.model_name = os.getenv("MODEL_NAME", "unknown")

        try:
            from ksadk import __version__
            self.version = __version__
        except ImportError:
            self.version = "0.2.0"

    def compose(self) -> ComposeResult:
        """构建 UI 布局"""
        with VerticalScroll(id="main-scroll"):
            yield Static(f"─ KsADK v{self.version} ─", id="title-bar")

            yield Container(
                Static(
                    f"[bold]Welcome![/]\n"
                    f"🤖\n"
                    f"[dim]{self.model_name} · Interactive Mode[/]\n"
                    f"[dim]{self._short_path()}[/]",
                    id="welcome-content",
                ),
                id="welcome-area",
            )

            with Vertical(id="chat-log"):
                pass

            yield Container(
                ChatInput(cwd=self.project_dir, id="chat-input-widget"),
                Static("Ctrl+C 退出", id="hint"),
                id="input-area",
            )

    def _short_path(self) -> str:
        home = Path.home()
        try:
            return "~/" + str(self.project_dir.relative_to(home))
        except ValueError:
            return str(self.project_dir)

    def on_mount(self) -> None:
        self.query_one("#chat-input-widget", ChatInput).focus_input()

    @on(ChatInput.Submitted)
    async def handle_input(self, event: ChatInput.Submitted) -> None:
        """处理用户输入"""
        user_input = event.value.strip()

        if not user_input:
            return

        # 检查退出
        if user_input.lower() in ("exit", "quit", "退出"):
            self.exit()
            return

        # 隐藏欢迎区域
        if not self._started:
            self._started = True
            self.add_class("started")

        asyncio.create_task(self._stream_response(user_input))

    async def _stream_response(self, user_input: str) -> None:
        """流式获取 Agent 响应"""
        chat_log = self.query_one("#chat-log", Vertical)
        main_scroll = self.query_one("#main-scroll", VerticalScroll)

        self._is_streaming = True

        input_data = {
            "input": user_input,
            "session_id": self.session_id,
            "history": self.history,
        }

        thinking_msg: Optional[ThinkingMessage] = None
        assistant_msg: Optional[AssistantMessage] = None
        full_response_text = ""

        try:
            await chat_log.mount(UserMessage(user_input))
            main_scroll.scroll_end(animate=False)

            async for chunk in self.runner.stream(input_data):
                if not self._is_streaming:
                    if thinking_msg:
                        thinking_msg.stop()
                    if assistant_msg:
                        await assistant_msg.stop_stream()
                    await chat_log.mount(SystemMessage("已中断", level="warning"))
                    break

                chunk_type = chunk.get("type", "text")

                # 思考过程
                if chunk_type == "thinking":
                    delta = chunk.get("delta", "")
                    if delta and self.show_thinking:
                        if not thinking_msg:
                            thinking_msg = ThinkingMessage()
                            await chat_log.mount(thinking_msg)
                            main_scroll.scroll_end(animate=False)
                        await thinking_msg.append_content(delta)
                    continue

                if thinking_msg and thinking_msg.is_active:
                    thinking_msg.stop()

                # 中断/确认
                if chunk_type == "interrupt":
                    await self._handle_interrupt(chunk)
                    return

                # 工具调用 - 跳过不显示
                if chunk_type == "tool_call":
                    continue

                # 文本响应
                delta = chunk.get("output", "") or chunk.get("delta", "")
                if delta:
                    if not assistant_msg:
                        assistant_msg = AssistantMessage()
                        await chat_log.mount(assistant_msg)
                        main_scroll.scroll_end(animate=False)
                    await assistant_msg.append_content(delta)
                    full_response_text += delta

            if thinking_msg:
                thinking_msg.stop()
            if assistant_msg:
                await assistant_msg.stop_stream()

            cleaned_response = _clean_response(full_response_text)
            if cleaned_response:
                self.history.append({"role": "user", "content": user_input})
                self.history.append({"role": "model", "content": cleaned_response})
            elif self._is_streaming and not full_response_text:
                result = await self.runner.invoke(input_data)
                response_text = result.get("output", "")
                cleaned_response = _clean_response(response_text)

                if cleaned_response:
                    assistant_msg = AssistantMessage()
                    await chat_log.mount(assistant_msg)
                    await assistant_msg.append_content(cleaned_response)
                    await assistant_msg.stop_stream()
                    self.history.append({"role": "user", "content": user_input})
                    self.history.append({"role": "model", "content": cleaned_response})
                else:
                    await chat_log.mount(SystemMessage("(无响应)", level="warning"))

        except asyncio.CancelledError:
            await chat_log.mount(SystemMessage("已取消", level="warning"))
        except Exception as e:
            await chat_log.mount(SystemMessage(f"错误: {e}", level="error"))
        finally:
            self._is_streaming = False
            main_scroll.scroll_end(animate=False)

    async def _handle_interrupt(self, interrupt_chunk: Dict[str, Any]) -> None:
        """处理敏感操作确认"""
        interrupt_info = interrupt_chunk.get("interrupt_info", {})

        if isinstance(interrupt_info, dict):
            tool_name = interrupt_info.get("tool", "未知操作")
            args = interrupt_info.get("args", {})
        elif isinstance(interrupt_info, list) and interrupt_info:
            first = interrupt_info[0]
            tool_name = str(getattr(first, "value", first))
            args = {}
        else:
            tool_name = str(interrupt_info)
            args = {}

        approved = await self.push_screen_wait(ApprovalScreen(tool_name, args))
        chat_log = self.query_one("#chat-log", Vertical)

        if approved:
            await chat_log.mount(SystemMessage("✓ 已确认，继续执行...", level="success"))

            resume_data = {
                "input": "确认",
                "session_id": interrupt_chunk.get("session_id", self.session_id),
                "history": self.history,
                "resume": True,
            }

            assistant_msg = AssistantMessage()
            await chat_log.mount(assistant_msg)

            try:
                async for chunk in self.runner.stream(resume_data):
                    delta = chunk.get("output", "") or chunk.get("delta", "")
                    if delta:
                        await assistant_msg.append_content(delta)
                await assistant_msg.stop_stream()
                if assistant_msg.content:
                    self.history.append({"role": "model", "content": assistant_msg.content})
            except Exception as e:
                await chat_log.mount(SystemMessage(f"错误: {e}", level="error"))
        else:
            await chat_log.mount(SystemMessage("✗ 已取消操作", level="warning"))

    def action_interrupt(self) -> None:
        self._is_streaming = False

    def action_quit_or_interrupt(self) -> None:
        if self._is_streaming:
            self.action_interrupt()
        else:
            self.exit()

    def action_quit_app(self) -> None:
        self.exit()

    def on_mouse_up(self, event: MouseUp) -> None:
        copy_selection_to_clipboard(self)


def run_tui(
    runner: "BaseRunner",
    show_thinking: bool = False,
    project_dir: str = ".",
) -> None:
    """启动 TUI 应用"""
    app = AgentTUI(
        runner=runner,
        show_thinking=show_thinking,
        project_dir=project_dir,
    )
    app.run()
