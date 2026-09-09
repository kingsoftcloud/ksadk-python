"""Bound knowledge retrieval, owned by one resource worker activation."""

from __future__ import annotations

from threading import Lock
from typing import Literal

from pydantic import Field

from ksadk.knowledge_base.client import KnowledgeBaseClient
from ksadk.plugins.contracts import PluginContractModel
from ksadk.resource_runtime.contracts import ResourceConfig, RetrievalPolicy


class KnowledgeQuery(PluginContractModel):
    query: str = Field(strict=True, min_length=1, max_length=16000)
    top_k: int | None = Field(default=None, strict=True, ge=1)


class KnowledgeItem(PluginContractModel):
    document_id: str = Field(max_length=4096)
    document_name: str = Field(max_length=4096)
    segment_id: str = Field(max_length=4096)
    content: str
    score: float = Field(allow_inf_nan=False)


class KnowledgeReply(PluginContractModel):
    status: Literal["ok", "empty", "failed"]
    binding_id: str
    items: tuple[KnowledgeItem, ...] = ()
    truncated: bool = False
    request_id: str = Field(default="", max_length=256)
    error_code: Literal["KNOWLEDGE_RETRIEVAL_FAILED", "RESOURCE_FORBIDDEN"] | None = None

    def text(self) -> str:
        if self.status == "failed":
            return "Knowledge retrieval failed."
        if self.status == "empty":
            return "No matching knowledge found."
        return "\n\n".join(
            f"[{item.document_name or item.document_id}] {item.content}" for item in self.items
        )


class BoundKnowledgeService:
    """The worker owns this service and its client exclusively.

    The lock includes both the blocking SDK call and copying its mutable
    last_* fields. IPC concurrency must not interleave those two steps.
    Identity/lease authorization occurs in the broker before this service.
    """

    def __init__(self, config: ResourceConfig, client: KnowledgeBaseClient):
        resource = config.binding.resource
        if resource.kind != "knowledge-base":
            raise ValueError("Knowledge service requires a knowledge-base binding")
        if client.dataset_id != resource.id or client.region != resource.region:
            raise ValueError("Knowledge client does not match its frozen binding")
        self._config = config
        self._client = client
        self._lock = Lock()

    def search(self, arguments: dict) -> KnowledgeReply:
        query = KnowledgeQuery.model_validate(arguments)
        policy = self._config.retrieval or RetrievalPolicy()
        top_k = min(query.top_k if query.top_k is not None else policy.top_k, policy.top_k)
        with self._lock:
            try:
                records = self._client.search(query.query, top_k=top_k)
                if self._client.last_error:
                    return self._failed()
                budget = policy.max_chars
                items = []
                truncated = len(records) > top_k
                for record in records[:top_k]:
                    if budget == 0:
                        truncated = True
                        break
                    content = record.content[:budget]
                    truncated = truncated or len(content) < len(record.content)
                    budget -= len(content)
                    items.append(
                        KnowledgeItem(
                            document_id=record.document_id,
                            document_name=record.document_name,
                            segment_id=record.segment_id,
                            content=content,
                            score=record.score,
                        )
                    )
                return KnowledgeReply(
                    status="ok" if records else "empty",
                    binding_id=self._config.binding.id,
                    items=tuple(items),
                    truncated=truncated,
                    request_id=self._client.last_request_id,
                )
            except Exception:
                # Raw upstream exceptions may include credentials, signed URLs or
                # knowledge text. Only the fixed public failure code crosses IPC.
                return self._failed()

    def _failed(self) -> KnowledgeReply:
        return KnowledgeReply(
            status="failed",
            binding_id=self._config.binding.id,
            request_id=self._client.last_request_id,
            error_code=(
                "RESOURCE_FORBIDDEN"
                if self._client.last_http_status in {401, 403}
                else "KNOWLEDGE_RETRIEVAL_FAILED"
            ),
        )
