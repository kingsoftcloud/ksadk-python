"""
AssistantMessage - 助手消息组件

支持流式 MarkdownStream 渲染（与 deepagents-cli 一致）
"""

from textual.containers import Vertical
from textual.widgets import Markdown
from textual.widgets._markdown import MarkdownStream
from typing import Any, Optional


class AssistantMessage(Vertical):
    """助手消息组件
    
    使用 MarkdownStream 实现流式 Markdown 渲染
    """
    
    DEFAULT_CSS = """
    AssistantMessage {
        height: auto;
        padding: 0 1;
        margin: 1 0 0 0;
        color: #ffffff;
    }

    AssistantMessage Markdown {
        padding: 0;
        margin: 0;
        color: #ffffff;
    }

    /* 确保 Markdown 的所有子组件都有正确的颜色 */
    AssistantMessage Markdown * {
        color: #ffffff;
    }
    """

    def __init__(self, content: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._content = content
        self._markdown: Optional[Markdown] = None
        self._stream: Optional[MarkdownStream] = None

    def compose(self):
        """构建助手消息布局"""
        yield Markdown("", id="assistant-content")

    def on_mount(self) -> None:
        """缓存 markdown widget 引用"""
        self._markdown = self.query_one("#assistant-content", Markdown)

    def _get_markdown(self) -> Markdown:
        """获取 markdown widget"""
        if self._markdown is None:
            self._markdown = self.query_one("#assistant-content", Markdown)
        return self._markdown

    def _ensure_stream(self) -> MarkdownStream:
        """确保 stream 已初始化"""
        if self._stream is None:
            self._stream = Markdown.get_stream(self._get_markdown())
        return self._stream

    async def append_content(self, text: str) -> None:
        """追加内容（流式渲染）"""
        if not text:
            return
        self._content += text
        stream = self._ensure_stream()
        await stream.write(text)

    async def stop_stream(self) -> None:
        """停止流式渲染"""
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None

    async def set_content(self, content: str) -> None:
        """设置完整内容"""
        await self.stop_stream()
        self._content = content
        if self._markdown:
            await self._markdown.update(content)

    @property
    def content(self) -> str:
        return self._content

