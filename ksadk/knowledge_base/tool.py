"""通用知识库检索函数

提供跨框架的 search_knowledge 函数，由各框架工具包装调用。
客户端实例通过单例模式懒加载，自动从环境变量配置。

使用示例:
    from ksadk.knowledge_base.tool import search_knowledge
    result = search_knowledge("如何配置知识库？")
    print(result)
"""

import logging
from typing import Optional

from ksadk.knowledge_base.service import KnowledgeBaseService

logger = logging.getLogger(__name__)

# 单例客户端
_service: Optional[KnowledgeBaseService] = None


def _get_or_create_service() -> KnowledgeBaseService:
    """获取或创建知识库 service (单例)"""
    global _service
    if _service is None:
        _service = KnowledgeBaseService.from_env()
        logger.info("KnowledgeBaseService singleton created from environment")
    return _service


def search_knowledge(query: str, top_k: Optional[int] = None) -> str:
    """检索知识库

    从环境变量自动配置的知识库中检索相关内容。
    返回格式化的文本结果，供 LLM 参考。

    Args:
        query: 检索关键词/问题
        top_k: 返回结果数 (可选，覆盖环境变量配置)

    Returns:
        格式化的检索结果文本
    """
    try:
        service = _get_or_create_service()
        return service.search_text(query, top_k)
    except Exception as e:
        logger.error(f"search_knowledge failed: {e}")
        return f"知识库检索失败: {e}"
