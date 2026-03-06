"""LangChain / LangGraph 知识库工具包装

将通用 search_knowledge 函数包装为 LangChain Tool。
用户在 LangGraph/LangChain 的 agent 中一行 import 即可使用。

使用示例:
    from ksadk.knowledge_base.langchain_tool import search_knowledge_base
    tools = [search_knowledge_base, ...]
"""

import logging

from ksadk.knowledge_base.tool import search_knowledge

logger = logging.getLogger(__name__)


def create_langchain_tool():
    """创建 LangChain Tool

    Returns:
        LangChain @tool 装饰的函数
    """
    try:
        from langchain_core.tools import tool

        @tool
        def search_knowledge_base(query: str) -> str:
            """搜索知识库获取相关信息。

            当需要查找专业知识、文档内容或特定领域信息时使用此工具。
            会自动从已配置的金山云知识库中检索最相关的内容。

            Args:
                query: 检索关键词或问题
            """
            return search_knowledge(query)

        return search_knowledge_base

    except ImportError:
        logger.warning(
            "langchain-core not installed, returning raw function. "
            "Install with: pip install langchain-core"
        )

        def search_knowledge_base(query: str) -> str:
            """搜索知识库获取相关信息"""
            return search_knowledge(query)

        return search_knowledge_base


# 预创建实例，方便直接 import 使用
search_knowledge_base = create_langchain_tool()
