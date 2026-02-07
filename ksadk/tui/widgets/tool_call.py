"""
ToolCallMessage - 工具调用消息组件

显示 Tool Call 状态和参数。
"""

from textual.widgets import Static
from textual.reactive import reactive
from textual.containers import Vertical
from typing import Any, Dict, Optional
import json

from .base import MessageWidget


class ToolStatus:
    """Tool Call 状态"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    REJECTED = "rejected"


class ToolCallMessage(MessageWidget):
    """工具调用消息组件
    
    用于显示 Tool Call 的状态和参数：
    - pending: 等待执行（黄色）
    - running: 执行中（蓝色动画）
    - success: 成功（绿色）
    - error: 失败（红色）
    - rejected: 被拒绝（灰色）
    """
    
    DEFAULT_CSS = """
    ToolCallMessage {
        width: 100%;
        padding: 1;
        margin: 0 0 1 0;
        border: round $secondary;
        background: $surface;
        color: #ffffff;
    }
    
    ToolCallMessage.pending {
        border: round $warning;
    }
    
    ToolCallMessage.running {
        border: round $primary;
    }
    
    ToolCallMessage.success {
        border: round $success;
    }
    
    ToolCallMessage.error {
        border: round $error;
    }
    
    ToolCallMessage.rejected {
        border: round $surface-darken-2;
        opacity: 0.7;
    }
    
    ToolCallMessage .tool-header {
        text-style: bold;
        color: #ffffff;
    }
    
    ToolCallMessage .tool-args {
        color: #aaaaaa;
        margin-left: 2;
    }
    
    ToolCallMessage .tool-output {
        margin-top: 1;
        padding: 1;
        background: $surface-darken-1;
        color: #ffffff;
    }
    """
    
    status: reactive[str] = reactive(ToolStatus.PENDING)
    output: reactive[str] = reactive("")
    
    def __init__(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_id: Optional[str] = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self._tool_name = tool_name
        self._args = args
        self._tool_id = tool_id
        self.add_class("pending")
    
    def compose(self):
        """构建组件"""
        status_icon = self._get_status_icon()
        yield Static(
            f"{status_icon} {self._tool_name}",
            classes="tool-header"
        )
        
        # 显示参数（截断过长的值）
        args_display = self._format_args(self._args)
        yield Static(args_display, classes="tool-args")
        
        # 输出区域
        if self.output:
            yield Static(self.output, classes="tool-output")
    
    def _get_status_icon(self) -> str:
        """获取状态图标"""
        icons = {
            ToolStatus.PENDING: "⏳",
            ToolStatus.RUNNING: "🔄",
            ToolStatus.SUCCESS: "✅",
            ToolStatus.ERROR: "❌",
            ToolStatus.REJECTED: "🚫",
        }
        return icons.get(self.status, "⏳")
    
    def _format_args(self, args: Dict[str, Any], max_len: int = 100) -> str:
        """格式化参数显示"""
        try:
            args_str = json.dumps(args, ensure_ascii=False, indent=2)
            if len(args_str) > max_len:
                args_str = args_str[:max_len] + "..."
            return args_str
        except Exception:
            return str(args)[:max_len]
    
    def _update_status(self, new_status: str) -> None:
        """更新状态"""
        self.remove_class(self.status)
        self.status = new_status
        self.add_class(new_status)
        self.refresh()
    
    def set_running(self) -> None:
        """设置为运行中"""
        self._update_status(ToolStatus.RUNNING)
    
    def set_success(self, output: str = "") -> None:
        """设置为成功"""
        self.output = output
        self._update_status(ToolStatus.SUCCESS)
    
    def set_error(self, error: str = "") -> None:
        """设置为失败"""
        self.output = f"Error: {error}"
        self._update_status(ToolStatus.ERROR)
    
    def set_rejected(self) -> None:
        """设置为被拒绝"""
        self._update_status(ToolStatus.REJECTED)
