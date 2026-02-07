"""
TUI Widgets - 消息组件

提供各类消息组件，用于 TUI 渲染。
参考 deepagents-cli 的消息组件设计。
"""

from .base import MessageWidget
from .user import UserMessage
from .assistant import AssistantMessage
from .thinking import ThinkingMessage
from .tool_call import ToolCallMessage
from .system import SystemMessage

__all__ = [
    "MessageWidget",
    "UserMessage",
    "AssistantMessage",
    "ThinkingMessage",
    "ToolCallMessage",
    "SystemMessage",
]
