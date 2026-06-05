from __future__ import annotations

from collections.abc import Callable


def as_tool(func: Callable):
    try:
        from langchain_core.tools import tool
    except Exception:
        return func
    return tool(func)
