"""ResolvedMemoryPolicy —— Memory 最终运行策略的统一解析入口（方案 §10）。

统一优先级（方案 §2）：
  memory.enabled=false → recall=false, write=off
  memory.enabled=true → recall 由 memory.recall.enabled 决定
    → write 由 rollout 和 write.mode 共同决定

  rollout=off → 不提取、不写入
  rollout=shadow → 生成 Candidate 和审计，不提交 Provider
  rollout=enabled + mode=explicit_only → 只保存用户明确要求记住的内容
  rollout=enabled + mode=candidate → 按 Candidate + Policy 判断是否提交

环境变量只作为旧 AgentVersion 缺少字段时的兼容 fallback。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedMemoryPolicy:
    """Memory 最终运行策略的统一解析结果。

    所有 Memory 相关决策（recall/flush/extraction）都应从此结构读取，
    不应分别从 memory.enabled / rollout / write.mode 各自判断。
    """

    enabled: bool
    recall_enabled: bool
    write_rollout: str  # off / shadow / enabled
    write_mode: str  # off / explicit_only / candidate
    flush_before_compaction: bool
    provider_ref: str

    @property
    def should_recall(self) -> bool:
        """是否执行 recall。"""
        return self.enabled and self.recall_enabled

    @property
    def should_extract_candidates(self) -> bool:
        """是否生成 Candidate（shadow 也生成，但不提交 Provider）。"""
        return self.enabled and self.write_rollout in ("shadow", "enabled")

    @property
    def should_flush(self) -> bool:
        """是否提交 Candidate 到 Provider。"""
        return (
            self.enabled
            and self.write_rollout == "enabled"
            and self.write_mode in ("explicit_only", "candidate")
        )

    @property
    def is_explicit_only(self) -> bool:
        """是否只保存用户明确要求记住的内容。"""
        return self.should_flush and self.write_mode == "explicit_only"


def resolve_memory_policy(
    *,
    memory_enabled: bool | None = None,
    recall_enabled: bool | None = None,
    write_rollout: str | None = None,
    write_mode: str | None = None,
    flush_before_compaction: bool | None = None,
    provider_ref: str | None = None,
) -> ResolvedMemoryPolicy:
    """统一解析 Memory 运行策略。

    Args:
        memory_enabled: MemorySpec.enabled
        recall_enabled: MemorySpec.recall.enabled
        write_rollout: ContextSpec.rollout.memoryWrite（off/shadow/enabled）
        write_mode: MemorySpec.write.mode（off/explicit_only/candidate）
        flush_before_compaction: MemorySpec.write.flushBeforeCompaction
        provider_ref: MemorySpec.providerRef
    """
    legacy_flush_enabled = str(
        os.environ.get("KSADK_MEMORY_FLUSH_ENABLED", "")
    ).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    enabled = legacy_flush_enabled if memory_enabled is None else memory_enabled
    recall = enabled if recall_enabled is None else recall_enabled
    flush_before = True if flush_before_compaction is None else flush_before_compaction
    provider = str(provider_ref or "local-default")

    if not enabled:
        return ResolvedMemoryPolicy(
            enabled=False,
            recall_enabled=False,
            write_rollout="off",
            write_mode="off",
            flush_before_compaction=False,
            provider_ref=provider,
        )

    # rollout 优先于 write_mode
    rollout = str(write_rollout or "").strip().lower()
    mode = str(write_mode or "").strip().lower()

    if rollout not in ("off", "shadow", "enabled"):
        rollout = "enabled" if legacy_flush_enabled else "off"

    if rollout == "off":
        return ResolvedMemoryPolicy(
            enabled=True,
            recall_enabled=recall,
            write_rollout="off",
            write_mode="off",
            flush_before_compaction=flush_before,
            provider_ref=provider,
        )

    if rollout == "shadow":
        return ResolvedMemoryPolicy(
            enabled=True,
            recall_enabled=recall,
            write_rollout="shadow",
            write_mode=mode if mode in ("explicit_only", "candidate") else "candidate",
            flush_before_compaction=flush_before,
            provider_ref=provider,
        )

    # rollout == "enabled"
    if mode not in ("explicit_only", "candidate"):
        mode = "candidate"

    return ResolvedMemoryPolicy(
        enabled=True,
        recall_enabled=recall,
        write_rollout="enabled",
        write_mode=mode,
        flush_before_compaction=flush_before,
        provider_ref=provider,
    )


__all__ = ["ResolvedMemoryPolicy", "resolve_memory_policy"]
