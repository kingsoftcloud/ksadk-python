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

from ksadk.knowledge_base.client import KnowledgeBaseClient

logger = logging.getLogger(__name__)

# 单例客户端
_client: Optional[KnowledgeBaseClient] = None


def _get_or_create_client() -> KnowledgeBaseClient:
    """获取或创建知识库客户端 (单例)"""
    global _client
    if _client is None:
        _client = KnowledgeBaseClient.from_env()
        logger.info("KnowledgeBaseClient singleton created from environment")
    return _client


def _format_results(results) -> str:
    """将检索结果格式化为 LLM 可读的文本"""
    if not results:
        return "未找到相关知识库内容。"

    formatted_parts = []
    for i, r in enumerate(results, 1):
        part = f"[{i}] "
        if r.document_name:
            part += f"(来源: {r.document_name}) "
        part += r.content
        if r.answer:
            part += f"\n    答案: {r.answer}"
        formatted_parts.append(part)

    return "\n\n".join(formatted_parts)


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
        client = _get_or_create_client()
        results = client.search(query, top_k)
        return _format_results(results)
    except Exception as e:
        logger.error(f"search_knowledge failed: {e}")
        return f"知识库检索失败: {e}"
