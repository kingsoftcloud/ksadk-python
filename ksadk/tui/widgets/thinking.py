"""
ThinkingMessage - 思考过程组件

带 spinner 动画的思考过程展示
"""

from textual.widgets import Static
from textual.reactive import reactive
from textual.timer import Timer
from typing import ClassVar, Any
from time import time

from .base import MessageWidget


class ThinkingMessage(MessageWidget):
    """思考过程组件
    
    显示 AI 思考过程，支持：
    - spinner 动画
    - 实时计时
    - 流式内容追加
    - 可折叠
    """
    
    DEFAULT_CSS = """
    ThinkingMessage {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
        border-left: wide #7777aa;
        color: #ffffff;
        background: $surface;
    }
    
    ThinkingMessage .thinking-header {
        color: #cccccc;
        width: 100%;
    }
    
    ThinkingMessage .thinking-content {
        color: #ffffff;
        margin-left: 2;
        width: 100%;
    }

    /* 确保所有子组件都有正确的颜色 */
    ThinkingMessage * {
        color: #ffffff;
    }
    """
    
    # Spinner 动画帧
    _SPINNER_FRAMES: ClassVar[tuple[str, ...]] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    
    is_active: reactive[bool] = reactive(True)
    
    def __init__(self, content: str = "", **kwargs: Any) -> None:
        super().__init__(content=content, **kwargs)
        self._thinking_content = content
        self._spinner_position = 0
        self._start_time: float = time()
        self._animation_timer: Timer | None = None
        self._header_widget: Static | None = None
        self._content_widget: Static | None = None
    
    def compose(self):
        """构建思考消息布局"""
        self._header_widget = Static("🧠 Thinking...", classes="thinking-header")
        yield self._header_widget
        
        self._content_widget = Static(self._thinking_content, classes="thinking-content")
        yield self._content_widget
    
    def on_mount(self) -> None:
        """启动动画"""
        if self.is_active:
            self._animation_timer = self.set_interval(0.1, self._update_animation)
    
    def _update_animation(self) -> None:
        """更新 spinner 动画"""
        if not self.is_active or self._header_widget is None:
            return
        
        frame = self._SPINNER_FRAMES[self._spinner_position]
        self._spinner_position = (self._spinner_position + 1) % len(self._SPINNER_FRAMES)
        
        elapsed = int(time() - self._start_time)
        self._header_widget.update(f"[bold #ffffff]{frame}[/] [italic #dddddd]Thinking... ({elapsed}s)[/]")
    
    async def append_content(self, delta: str) -> None:
        """追加思考内容"""
        self._thinking_content += delta
        if self._content_widget:
            self._content_widget.update(self._thinking_content)
    
    def stop(self) -> None:
        """停止思考动画"""
        self.is_active = False
        if self._animation_timer:
            self._animation_timer.stop()
            self._animation_timer = None
        
        if self._header_widget:
            elapsed = int(time() - self._start_time)
            self._header_widget.update(f"[bold #ffffff]💭[/] [italic #aaaaaa]Thought for {elapsed}s[/]")
    
    @property
    def thinking_content(self) -> str:
        return self._thinking_content
