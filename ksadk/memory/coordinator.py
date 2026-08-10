"""Memory Coordinator —— core/recall/flush/commit 编排（方案 §10 / §11.1）。

Coordinator 是本地与云端一致的运行时编排层：负责召回（recall）、压缩前 best-effort Flush、
候选评估与提交（commit）。Provider 故障返回结构化空结果或标准错误，不污染模型输入
（方案 §10.8）。

本模块不依赖具体 Provider 实现，只依赖 ``MemoryProvider`` Protocol 与 ``MemoryPolicy``，
便于本地 SQLite 与云端 HTTP/SDK 共用同一套编排逻辑与契约测试。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from ksadk.memory.models import (
    CoreMemoryRequest,
    MemoryCandidate,
    MemoryCapabilities,
    MemoryDeleteRequest,
    MemoryRecord,
    MemoryScope,
    MemorySearchRequest,
    MemorySearchResult,
)
from ksadk.memory.policy import MemoryEvaluation, MemoryPolicy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlushResult:
    """一次压缩前 Memory Flush 的结果（方案 §9.2）。"""

    status: str  # succeeded / partial / failed / skipped
    proposed: int = 0
    committed: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "proposed": self.proposed,
            "committed": self.committed,
            "rejected": self.rejected,
        }


class MemoryCoordinator:
    """core/recall/flush/commit 编排（方案 §10）。

    ``MemoryCoordinator`` 持有一个 ``MemoryProvider`` 与一个 ``MemoryPolicy``。本地与云端用
    不同 Provider 实现，但编排逻辑与契约一致（方案 §12）。
    """

    def __init__(
        self,
        provider: Any,
        *,
        policy: MemoryPolicy | None = None,
        tenant_id: str = "local",
        workspace_id: str = "local",
    ) -> None:
        self._provider = provider
        self._policy = policy or MemoryPolicy()
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id

    @property
    def provider(self) -> Any:
        return self._provider

    @property
    def policy(self) -> MemoryPolicy:
        return self._policy

    def capabilities(self) -> MemoryCapabilities:
        try:
            caps = self._provider.capabilities()
            if isinstance(caps, MemoryCapabilities):
                return caps
        except Exception as exc:  # noqa: BLE001
            logger.debug("memory capabilities failed: %s", exc)
        return MemoryCapabilities(
            semantic_search=False,
            keyword_search=False,
            metadata_filter=False,
            versioned_update=False,
            hard_delete=False,
            ttl=False,
            max_record_chars=0,
        )

    # ---- recall（方案 §10.6）----

    def recall(self, request: MemorySearchRequest) -> MemorySearchResult:
        """召回长期记忆，失败返回结构化空结果，不抛异常文本进模型上下文。"""
        if not request.query.strip() or not request.scopes:
            return MemorySearchResult(
                status="not_configured",
                records=[],
                error_code="empty_query_or_scope",
                provider=self._provider_name(),
                latency_ms=0,
                accounting_accuracy="opaque",
            )
        try:
            result = self._provider.search(request)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory recall failed: %s", exc)
            return MemorySearchResult(
                status="failed",
                records=[],
                error_code="provider_error",
                provider=self._provider_name(),
                latency_ms=0,
                accounting_accuracy="opaque",
            )
        return result

    def list_core(self, request: CoreMemoryRequest) -> list[MemoryRecord]:
        try:
            return list(self._provider.list_core(request))
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory list_core failed: %s", exc)
            return []

    # ---- flush / commit（方案 §9.2 / §10.3）----

    def flush_candidates(
        self,
        candidates: list[MemoryCandidate],
        *,
        existing_index: Mapping[str, MemoryRecord] | None = None,
    ) -> FlushResult:
        """压缩前 best-effort Memory Flush（方案 §9.2）。

        失败不阻止紧急 compaction（方案 §9.2 失败语义）。逐条评估 → commit/reject，不批量抛。
        """
        if not candidates:
            return FlushResult(status="skipped")
        committed = 0
        rejected = 0
        errors: list[str] = []
        for candidate in candidates:
            try:
                existing = None
                if existing_index and candidate.conflicts_with:
                    existing = existing_index.get(candidate.conflicts_with[0])
                evaluation = self._policy.evaluate(candidate, existing=existing)
                if evaluation.decision == "reject":
                    rejected += 1
                    continue
                if evaluation.decision == "pending":
                    # pending 不在本轮 flush 提交（留 Coordinator 后台聚合）。
                    rejected += 1
                    continue
                self._commit(candidate, evaluation, existing)
                committed += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                logger.warning("memory flush candidate failed: %s", exc)
        status = "succeeded" if not errors else "partial"
        return FlushResult(
            status=status,
            proposed=len(candidates),
            committed=committed,
            rejected=rejected,
            errors=errors,
        )

    def propose_and_commit(
        self,
        candidate: MemoryCandidate,
        *,
        existing: MemoryRecord | None = None,
    ) -> MemoryEvaluation:
        """同步提交单个候选（用户明确"记住/忘掉"路径，方案 §10.4）。"""
        evaluation = self._policy.evaluate(candidate, existing=existing)
        if evaluation.decision == "commit":
            self._commit(candidate, evaluation, existing)
        return evaluation

    def delete(
        self, memory_id: str, *, scope: MemoryScope, scope_id: str, hard: bool = False
    ) -> bool:
        """用户明确遗忘（方案 §10.4 / §19）：不支持 hard delete 时明确返回失败。"""
        caps = self.capabilities()
        if hard and not caps.hard_delete:
            logger.warning("hard delete requested but provider lacks hard_delete capability")
            return False
        try:
            result = self._provider.delete(
                MemoryDeleteRequest(memory_id=memory_id, scope=scope, scope_id=scope_id, hard=hard)
            )
            return bool(result.deleted)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory delete failed: %s", exc)
            return False

    # ---- internals ----

    def _commit(
        self,
        candidate: MemoryCandidate,
        evaluation: MemoryEvaluation,
        existing: MemoryRecord | None,
    ) -> None:
        from ksadk.memory.policy import content_hash

        if evaluation.operation == "delete":
            if candidate.conflicts_with:
                self._provider.delete(
                    MemoryDeleteRequest(
                        memory_id=candidate.conflicts_with[0],
                        scope=candidate.scope,
                        scope_id=candidate.scope_id,
                        hard=self.capabilities().hard_delete,
                    )
                )
            return

        now_iso = ""
        memory_id = existing.memory_id if existing is not None else f"mem_{uuid.uuid4().hex[:24]}"
        record = MemoryRecord(
            memory_id=memory_id,
            tenant_id=self._tenant_id,
            workspace_id=self._workspace_id,
            scope=candidate.scope,
            scope_id=candidate.scope_id,
            memory_type=candidate.memory_type,
            content=candidate.content,
            summary=candidate.content[:200],
            status="active",
            confidence=candidate.confidence,
            importance=candidate.importance,
            valid_from=now_iso,
            valid_to="",
            expires_at="",
            source_session_id="",
            source_event_ids=list(candidate.source_event_ids),
            source_seq_range=None,
            content_hash=content_hash(candidate.content),
            version=evaluation.new_version or 1,
            metadata={"reason": candidate.reason, "operation": evaluation.operation},
            created_at=now_iso,
            updated_at=now_iso,
        )
        self._provider.upsert(
            record,
            expected_version=(existing.version if existing is not None else None),
        )

    def _provider_name(self) -> str:
        return type(self._provider).__name__


def build_search_request(
    *,
    query: str,
    user_id: str = "",
    agent_id: str = "",
    workspace_id: str = "",
    top_k: int = 8,
    max_tokens: int = 4000,
    min_score: float = 0.45,
) -> MemorySearchRequest:
    """便捷构造检索请求，按方案 §10.6 组装 scopes（user > agent > workspace > org）。

    scope_id 由可信 Principal 决定，不信任用户自行提交（方案 §19）。
    """
    scopes: list[tuple[MemoryScope, str]] = []
    if user_id:
        scopes.append(("user", user_id))
    if agent_id:
        scopes.append(("agent", agent_id))
    if workspace_id:
        scopes.append(("workspace", workspace_id))
    return MemorySearchRequest(
        query=query,
        scopes=scopes,
        memory_types=["profile", "fact", "episode"],
        top_k=top_k,
        max_tokens=max_tokens,
        min_score=min_score,
    )


def recall_to_context_item(result: MemorySearchResult) -> dict[str, Any] | None:
    """把检索结果投影成可注入模型的 ambient context（方案 §10.6 第 5 条）。

    失败（status != ok）返回 ``None``，不把错误字符串塞进正文（方案 §10.8）。无结果返回
    ``None``（不注入"未找到…"噪声，方案 §10.8 第 4 条）。每条结果保留 memory_id/scope/score
    的安全短引用。
    """
    if result.status != "ok" or not result.records:
        return None
    lines: list[str] = []
    for index, record in enumerate(result.records, 1):
        lines.append(f"[{index}] {record.summary or record.content}")
    return {
        "formatted_text": "\n\n".join(lines),
        "recall_count": len(result.records),
        "accounting_accuracy": result.accounting_accuracy,
    }


__all__ = [
    "FlushResult",
    "MemoryCoordinator",
    "build_search_request",
    "recall_to_context_item",
]
