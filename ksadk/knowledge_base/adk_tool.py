"""ADK 知识库工具包装

将通用 search_knowledge 函数包装为 Google ADK 可用的工具。
由 ADKRunner 在检测到知识库配置时自动注入到 Agent。

注意: 此模块依赖 google-adk，仅在 ADK 框架下使用。
"""

import logging

from ksadk.knowledge_base.tool import search_knowledge

logger = logging.getLogger(__name__)


def search_knowledge_base(query: str) -> dict:
    """搜索知识库获取相关信息

    当用户的问题需要查找专业知识、文档内容或特定领域信息时，
    使用此工具从知识库中检索相关内容。

    Args:
        query: 检索关键词或问题

    Returns:
        包含检索结果的字典
    """
    result = search_knowledge(query)
    return {"result": result}


def create_adk_tool():
    """创建 ADK FunctionTool

    Returns:
        可直接注入到 ADK Agent 的工具对象
    """
    try:
        from google.adk.tools import FunctionTool

        return FunctionTool(func=search_knowledge_base)
    except ImportError:
        logger.warning(
            "google-adk not installed, returning raw function as tool. "
            "Install with: pip install ksadk[adk]"
        )
        return search_knowledge_base
