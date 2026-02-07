"""
UserMessage - 用户消息组件
"""

from textual.widgets import Static
from rich.text import Text
from typing import Any

from .base import MessageWidget


class UserMessage(MessageWidget):
    """用户消息组件
    
    显示用户输入，左边框绿色
    """
    
    DEFAULT_CSS = """
    UserMessage {
        height: auto;
        padding: 0 1;
        margin: 1 0 0 0;
        background: transparent;
        border-left: wide #10b981;
    }
    """

    def __init__(self, content: str, **kwargs: Any) -> None:
        super().__init__(content=content, **kwargs)
        self._content = content

    def compose(self):
        """构建用户消息布局"""
        text = Text()
        text.append("> ", style="bold #10b981")
        text.append(self._content, style="#ffffff")
        yield Static(text)
