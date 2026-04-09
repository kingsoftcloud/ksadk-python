"""KsADK 知识库集成模块

提供金山云知识库 (AICP RetrieveKnowledge) 的集成能力。
用户通过环境变量配置即可自动启用知识库检索。

环境变量:
    KSADK_KB_DATASET_ID: 知识库 ID (必填，存在即启用)
    KSADK_KB_ACCESS_KEY: AK (可选，默认取 KSYUN_ACCESS_KEY)
    KSADK_KB_SECRET_KEY: SK (可选，默认取 KSYUN_SECRET_KEY)
    KSADK_KB_REGION: 区域 (默认 cn-beijing-6)
    KSADK_KB_ENDPOINT: API 端点 (默认 aicp.api.ksyun.com)
    KSADK_KB_TOP_K: 返回结果数 (默认 5)
    KSADK_KB_SEARCH_METHOD: 检索方法 (默认 intelligence_search)

使用方式:
    # ADK: 设置环境变量后自动注入，无需代码改动

    # LangGraph / LangChain / DeepAgents:
    # 1) 仅配 env，平台会在调用前自动注入知识库上下文
    # 2) 也支持一行 import 手动加入 tools
    from ksadk.knowledge_base import search_knowledge_base
    tools = [search_knowledge_base, ...]

    # 直接使用通用函数
    from ksadk.knowledge_base import search_knowledge
    result = search_knowledge("你的问题")
"""

from ksadk.knowledge_base.client import KnowledgeBaseClient, KnowledgeBaseResult
from ksadk.knowledge_base.tool import search_knowledge

__all__ = [
    "KnowledgeBaseClient",
    "KnowledgeBaseResult",
    "search_knowledge",
    "search_knowledge_base",
    "create_adk_tool",
    "create_langchain_tool",
]


def create_adk_tool():
    """创建 ADK 知识库工具"""
    from ksadk.knowledge_base.adk_tool import create_adk_tool as _create

    return _create()


def create_langchain_tool():
    """创建 LangChain 知识库工具"""
    from ksadk.knowledge_base.langchain_tool import create_langchain_tool as _create

    return _create()


# LangChain/LangGraph 用户可直接 import 使用
def __getattr__(name):
    if name == "search_knowledge_base":
        from ksadk.knowledge_base.langchain_tool import search_knowledge_base

        return search_knowledge_base
    raise AttributeError(f"module 'ksadk.knowledge_base' has no attribute {name!r}")
