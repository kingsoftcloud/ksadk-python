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

    def search(self, query: str, top_k: Optional[int] = None) -> list[KnowledgeBaseResult]:
        return self._get_client().search(query, top_k)

    def search_text(self, query: str, top_k: Optional[int] = None) -> str:
        try:
            return format_knowledge_results(self.search(query, top_k))
        except Exception as exc:
            logger.error("search_knowledge failed: %s", exc)
            return f"知识库检索失败: {exc}"

    def build_context(self, query: str, top_k: Optional[int] = None) -> dict[str, str] | None:
        normalized = str(query or "").strip()
        if not normalized:
            return None
        if not self.is_configured():
            return None
        return {
            "query": normalized,
            "formatted_text": self.search_text(normalized, top_k),
        }
