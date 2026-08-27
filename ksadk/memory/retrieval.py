"""混合检索管线（长任务方案 §7.5 P2）。

检索策略分层（可插拔，KsADK MemoryRecord 始终是公共协议）：

.. code-block:: text

    Tenant / ACL / Scope Filter   （Provider SQL 已完成）
      -> Time / Status / TTL Filter（Coordinator.recall 已完成）
      -> Keyword + Vector Search   （keyword：词项覆盖率；vector：可选注入）
      -> Rerank                    （分数融合：coverage 为主，vector 加权）
      -> Deduplicate / Diversity   （同 slot_key 只留最高分）
      -> Token Budget Truncation   （按分数顺序装箱）

本地 SQLite Provider 无向量索引，``vector_score`` 由外部注入（企业
Memory Service / 向量库适配器提供）；缺失时退化为纯关键词管线，不阻塞。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from ksadk.memory.models import MemoryRecord

#: 多样性控制：同 slot_key 保留的最高分条数。
_PER_SLOT_LIMIT = 1


@dataclass(frozen=True)
class RetrievalConfig:
    """管线参数（Token 截断与多样性，避免硬编码全局常数）。"""

    top_k: int = 8
    max_tokens: int = 4000
    #: vector 分数权重（0 = 纯关键词）。
    vector_weight: float = 0.0
    per_slot_limit: int = _PER_SLOT_LIMIT


def keyword_coverage(query: str, content: str) -> float:
    """查询词项在内容中的覆盖率（0~1；ASCII 词 + CJK 2-gram）。"""
    terms = _query_terms(query)
    if not terms:
        return 0.0
    lowered = content.lower()
    hits = sum(1 for t in terms if t in lowered)
    return round(hits / len(terms), 4)


def _query_terms(query: str) -> list[str]:
    from ksadk.memory.providers.local_sqlite import _keyword_query_terms

    ascii_terms, cjk_terms = _keyword_query_terms(query)
    terms = [t.lower() for t in ascii_terms]
    # CJK 词项直接作为覆盖判据（provider 侧已做 2-gram 切分）。
    terms.extend(cjk_terms)
    return terms


def rerank_records(
    records: Sequence[MemoryRecord],
    *,
    query: str,
    config: RetrievalConfig | None = None,
    vector_score: Callable[[MemoryRecord], float] | None = None,
) -> list[MemoryRecord]:
    """Rerank + 去重/多样性 + Token 截断（§7.5 默认管线）。

    分数 = (1-w) * keyword_coverage + w * vector_score；无 vector 注入时
    w 强制为 0（退化为纯关键词）。排序后按 slot_key 去重（保留最高分），
    最后按分数顺序做 Token 装箱截断。
    """
    cfg = config or RetrievalConfig()
    weight = cfg.vector_weight if vector_score is not None else 0.0

    scored: list[tuple[float, MemoryRecord]] = []
    for record in records:
        kw = keyword_coverage(query, record.content)
        vec = vector_score(record) if vector_score is not None and weight > 0 else 0.0
        score = round((1 - weight) * kw + weight * vec, 4)
        # 综合重要性微调（同分时 importance 高者在前，不改变主序）。
        scored.append((score + record.importance * 1e-6, record))
    scored.sort(key=lambda pair: -pair[0])

    # Deduplicate / Diversity：同 slot_key 只留最高分（slot_key 取 metadata）。
    seen_slots: dict[str, int] = {}
    diverse: list[MemoryRecord] = []
    for score, record in scored:
        slot = str(record.metadata.get("slot_key") or "")
        if slot:
            count = seen_slots.get(slot, 0)
            if count >= cfg.per_slot_limit:
                continue
            seen_slots[slot] = count + 1
        diverse.append(record)
        if len(diverse) >= cfg.top_k:
            break

    return _box_by_tokens(diverse, cfg.max_tokens)


def _box_by_tokens(records: list[MemoryRecord], max_tokens: int) -> list[MemoryRecord]:
    """按顺序装箱（§10.6：Token 预算截断）。"""
    from ksadk.context_engine.tokenizer import get_default_token_counter

    counter = get_default_token_counter()
    total = 0
    out: list[MemoryRecord] = []
    for record in records:
        n = counter.count_text(record.content)
        if total + n > max_tokens:
            break
        total += n
        out.append(record)
    return out


__all__ = ["RetrievalConfig", "keyword_coverage", "rerank_records"]
