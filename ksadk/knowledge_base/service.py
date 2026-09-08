"""Platform-level knowledge base service helpers."""

from __future__ import annotations

import logging
from typing import Optional

from ksadk.knowledge_base.client import KnowledgeBaseClient, KnowledgeBaseResult

logger = logging.getLogger(__name__)


def format_knowledge_results(results: list[KnowledgeBaseResult]) -> str:
    if not results:
        return "未找到相关知识库内容。"

    formatted_parts: list[str] = []
    for i, result in enumerate(results, 1):
        part = f"[{i}] "
        if result.document_name:
            part += f"(来源: {result.document_name}) "
        part += result.content
        if result.answer:
            part += f"\n    答案: {result.answer}"
        formatted_parts.append(part)

    return "\n\n".join(formatted_parts)


class KnowledgeBaseService:
    def __init__(self, client: KnowledgeBaseClient | None = None):
        self._client = client

    @classmethod
    def from_env(cls) -> "KnowledgeBaseService":
        return cls(KnowledgeBaseClient.from_env())

    @staticmethod
    def is_configured() -> bool:
        return KnowledgeBaseClient.is_configured()

    def _get_client(self) -> KnowledgeBaseClient:
        if self._client is None:
            self._client = KnowledgeBaseClient.from_env()
        return self._client

    @property
    def last_error(self) -> str:
        """最近一次检索失败原因（成功前置空，失败填充；含响应解析失败）。

        供 ``build_context`` 区分"后端吞错/解析失败返空"与"真无结果"。
        客户端尚未懒加载时视为无错误。
        """
        return str(getattr(self._client, "last_error", "") or "")

    def search(self, query: str, top_k: Optional[int] = None) -> list[KnowledgeBaseResult]:
        return self._get_client().search(query, top_k)

    def search_text(self, query: str, top_k: Optional[int] = None) -> str:
        try:
            return format_knowledge_results(self.search(query, top_k))
        except Exception as exc:
            logger.error("search_knowledge failed: %s", exc)
            return f"知识库检索失败: {exc}"

    def build_context(
        self,
        query: str,
        top_k: Optional[int] = None,
    ) -> dict[str, str] | None:
        """构造环境知识库上下文。失败时返回 ``formatted_text=""`` + 独立 ``error`` 字段，
        不把错误字符串塞进 ``formatted_text``（避免错误伪装成知识库正文注入模型）。

        - 检索抛错（网络/鉴权失败）→ except 捕获，返 ``error`` 字段。
        - 响应解析失败返空列表（``_parse_response``）→ client ``last_error`` 非空，
          返回 ``error`` 字段。
        - 真无结果（检索正常返空）→ ``formatted_text`` 为"未找到…"（语义真实，可注入）。
        """
        normalized = str(query or "").strip()
        if not normalized or not self.is_configured():
            return None
        try:
            results = self.search(normalized, top_k)
        except Exception as exc:
            logger.error("search_knowledge failed: %s", exc)
            return {"query": normalized, "formatted_text": "", "error": str(exc)}
        client_error = self.last_error
        if not results and client_error:
            return {"query": normalized, "formatted_text": "", "error": client_error}
        return {
            "query": normalized,
            "formatted_text": format_knowledge_results(results),
        }
