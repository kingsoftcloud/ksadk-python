"""剪贴板工具 - 支持鼠标选择复制"""

from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.app import App

_PREVIEW_MAX_LENGTH = 40


def _copy_osc52(text: str) -> None:
    """使用 OSC 52 转义序列复制（支持 SSH/tmux）"""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    osc52_seq = f"\033]52;c;{encoded}\a"
    if os.environ.get("TMUX"):
        osc52_seq = f"\033Ptmux;\033{osc52_seq}\033\\"

    try:
        with open("/dev/tty", "w") as tty:
            tty.write(osc52_seq)
            tty.flush()
    except Exception:
        raise RuntimeError("OSC52 复制失败")


def _shorten_preview(texts: list[str]) -> str:
    """缩短文本预览"""
    dense_text = "⏎".join(texts).replace("\n", "⏎")
    if len(dense_text) > _PREVIEW_MAX_LENGTH:
        return f"{dense_text[: _PREVIEW_MAX_LENGTH - 1]}…"
    return dense_text


def _clipboard_copy_methods(app: App):
    copy_methods = []
    if os.name != "nt":
        copy_methods.append(_copy_osc52)

    try:
        import pyperclip
        copy_methods.append(pyperclip.copy)
    except ImportError:
        pass

    copy_methods.append(app.copy_to_clipboard)
    return copy_methods


def copy_selection_to_clipboard(app: App) -> None:
    """复制选中文本到剪贴板
    
    遍历所有 widgets 获取选中文本并复制到系统剪贴板
    """
    selected_texts = []

    for widget in app.query("*"):
        if not hasattr(widget, "text_selection") or not widget.text_selection:
            continue

        selection = widget.text_selection

        try:
            result = widget.get_selection(selection)
        except Exception:
            continue

        if not result:
            continue

        selected_text, _ = result
        if selected_text.strip():
            selected_texts.append(selected_text)

    if not selected_texts:
        return

    combined_text = "\n".join(selected_texts)

    for copy_fn in _clipboard_copy_methods(app):
        try:
            copy_fn(combined_text)
            app.notify(
                f'"{_shorten_preview(selected_texts)}" 已复制',
                severity="information",
                timeout=2,
            )
            return
        except Exception:
            continue

    # 所有方式都失败
    app.notify(
        "复制失败 - 剪贴板不可用",
        severity="warning",
        timeout=3,
    )
