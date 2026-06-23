"""ADK wrapper for platform save_memory tool."""

from __future__ import annotations

import logging

from ksadk.memory.tool import save_memory as _save_memory

logger = logging.getLogger(__name__)


def save_memory(content: str) -> dict:
    """保存一条长期记忆。"""

    result = _save_memory(content)
    return {
        "result": result.get("message", ""),
        **result,
    }


def create_adk_tool():
    try:
        from google.adk.tools import FunctionTool

        return FunctionTool(func=save_memory)
    except ImportError:
        logger.warning(
            "google-adk not installed, returning raw function as tool. Install with: pip install ksadk[adk]"
        )
        return save_memory
