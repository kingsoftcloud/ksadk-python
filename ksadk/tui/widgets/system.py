"""
SystemMessage - 系统消息组件

用于显示系统通知、错误等。
"""

from textual.widgets import Static
from typing import Optional

from .base import MessageWidget


class SystemMessage(MessageWidget):
    """系统消息组件
    
    用于显示系统级通知：
    - 信息提示
    - 警告
    - 错误
    """
    
    DEFAULT_CSS = """
    SystemMessage {
        width: 100%;
        padding: 0 1;
        margin: 0 0 1 0;
        color: #bbbbbb;
        background: $surface;
    }
    
    SystemMessage.info {
        color: #66ccff;
    }

    SystemMessage.info * {
        color: #66ccff;
    }
    
    SystemMessage.warning {
        color: #ffcc00;
    }

    SystemMessage.warning * {
        color: #ffcc00;
    }
    
    SystemMessage.error {
        color: #ff6666;
    }

    SystemMessage.error * {
        color: #ff6666;
    }
    
    SystemMessage.success {
        color: #66ff66;
    }

    SystemMessage.success * {
        color: #66ff66;
    }
    """
    
    def __init__(
        self,
        content: str,
        level: str = "info",
        **kwargs
    ) -> None:
        super().__init__(content=content, **kwargs)
        self._level = level
        self.add_class(level)
    
    def compose(self):
        """构建组件"""
        icon = self._get_icon()
        yield Static(f"{icon} {self.content}")
    
    def _get_icon(self) -> str:
        """获取图标"""
        icons = {
            "info": "ℹ️",
            "warning": "⚠️",
            "error": "❌",
            "success": "✅",
        }
        return icons.get(self._level, "ℹ️")
