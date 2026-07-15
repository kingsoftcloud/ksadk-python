"""
KsADK TUI - prompt_toolkit 全屏交互（对标 Codex CLI）

- 全屏 alternate screen，transcript 区（FormattedTextControl + ANSI 保留 rich 颜色）
  + 底部输入框 + footer 状态栏
- 滚动：PageUp/PgDn/↑↓（cursor 跟随视口顶 + 手动 vertical_scroll）
- 流式：streaming 期动态区累积，turn 结束落定整条 rich 渲染（历史 ANSI 缓存）
- 命令：/new /clear /session /model（模型热切 picker）? exit；Ctrl-C 取消流式/退出
"""

from ksadk.tui.loop import (
    InteractionLoop,
    InterruptPending,
    RichLiveRenderer,
    TranscriptEntry,
    render_stream,
    run_tui,
)

__all__ = [
    "InteractionLoop",
    "InterruptPending",
    "RichLiveRenderer",
    "TranscriptEntry",
    "render_stream",
    "run_tui",
]
