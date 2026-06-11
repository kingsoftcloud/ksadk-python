"""LangChain / LangGraph long-term memory tool wrappers."""

from __future__ import annotations

import logging

from ksadk.memory.tool import load_memory as _load_memory
from ksadk.memory.tool import save_memory as _save_memory

logger = logging.getLogger(__name__)


def create_langchain_tools():
    try:
        from langchain_core.tools import tool

        @tool
        def load_memory_tool(query: str) -> str:
            """检索当前用户的长期记忆。"""

            return _load_memory(query)

        @tool
        def save_memory_tool(content: str) -> dict:
            """保存一条长期记忆。"""

            return _save_memory(content)

        load_memory_tool.name = "load_memory"
        save_memory_tool.name = "save_memory"
        return load_memory_tool, save_memory_tool
    except ImportError:
        logger.warning(
            "langchain-core not installed, returning raw functions. Install with: pip install langchain-core"
        )
        return _load_memory, _save_memory


load_memory, save_memory = create_langchain_tools()
