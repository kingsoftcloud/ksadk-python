"""
Base Widget - 消息组件基类
"""

from textual.widget import Widget
from textual.reactive import reactive
from typing import Any, Optional


class MessageWidget(Widget):
    """消息组件基类
    
    所有消息组件的抽象基类，提供基础的消息渲染能力。
    """
    
    DEFAULT_CSS = """
    MessageWidget {
        width: 100%;
        padding: 0 1;
        margin: 0 0 1 0;
        color: #ffffff;
    }
    """
    
    content: reactive[str] = reactive("")
    
    def __init__(
        self, 
        content: str = "", 
        *,
        name: Optional[str] = None,
        id: Optional[str] = None,
        classes: Optional[str] = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.content = content
        self._is_streaming = False
    
    async def append_content(self, delta: str) -> None:
        """追加内容（流式渲染）
        
        Args:
            delta: 增量内容
        """
        self._is_streaming = True
        self.content += delta
    
    async def stop_stream(self) -> None:
        """停止流式渲染"""
        self._is_streaming = False
    
    def watch_content(self, new_content: str) -> None:
        """监听内容变化，触发重新渲染"""
        self.refresh()
