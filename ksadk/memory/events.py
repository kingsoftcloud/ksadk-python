"""Memory 失败可观测的结构化事件（方案 §3）。

不记录记忆正文和敏感信息。Studio 普通界面只提示"记忆保存失败"，
详细错误放 Trace。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MemoryEventType = Literal[
    "memory.recall.completed",
    "memory.recall.projected",
    "memory.recall.empty",
    "memory.recall.failed",
    "memory.candidate.created",
    "memory.candidate.rejected",
    "memory.flush.completed",
    "memory.flush.failed",
]


@dataclass(frozen=True)
class MemoryEvent:
    """结构化 Memory 事件（不记录正文/敏感信息）。"""

    type: MemoryEventType
    run_id: str
    session_id: str
    provider: str
    policy_rollout: str
    candidate_count: int = 0
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 plain dict（不含正文/敏感信息）。"""
        return {
            "type": self.type,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "provider": self.provider,
            "policy_rollout": self.policy_rollout,
            "candidate_count": self.candidate_count,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "metadata": self.metadata,
        }


def recall_completed(
    *, run_id: str, session_id: str, provider: str, rollout: str, count: int
) -> MemoryEvent:
    return MemoryEvent(
        type="memory.recall.completed",
        run_id=run_id,
        session_id=session_id,
        provider=provider,
        policy_rollout=rollout,
        candidate_count=count,
    )


def recall_projected(
    *,
    run_id: str,
    session_id: str,
    provider: str,
    rollout: str,
    count: int,
    runtime_type: str,
    target: str,
) -> MemoryEvent:
    """记录召回结果已交付 Runner；不代表模型一定采纳了相关事实。"""
    return MemoryEvent(
        type="memory.recall.projected",
        run_id=run_id,
        session_id=session_id,
        provider=provider,
        policy_rollout=rollout,
        candidate_count=count,
        metadata={"runtime_type": runtime_type, "target": target},
    )


def recall_empty(*, run_id: str, session_id: str, provider: str, rollout: str) -> MemoryEvent:
    return MemoryEvent(
        type="memory.recall.empty",
        run_id=run_id,
        session_id=session_id,
        provider=provider,
        policy_rollout=rollout,
    )


def recall_failed(
    *,
    run_id: str,
    session_id: str,
    provider: str,
    rollout: str,
    error_code: str,
    error_message: str,
    retryable: bool = True,
) -> MemoryEvent:
    return MemoryEvent(
        type="memory.recall.failed",
        run_id=run_id,
        session_id=session_id,
        provider=provider,
        policy_rollout=rollout,
        error_code=error_code,
        error_message=error_message,
        retryable=retryable,
    )


def candidate_created(
    *, run_id: str, session_id: str, provider: str, rollout: str, count: int
) -> MemoryEvent:
    return MemoryEvent(
        type="memory.candidate.created",
        run_id=run_id,
        session_id=session_id,
        provider=provider,
        policy_rollout=rollout,
        candidate_count=count,
    )


def candidate_rejected(
    *, run_id: str, session_id: str, provider: str, rollout: str, count: int
) -> MemoryEvent:
    return MemoryEvent(
        type="memory.candidate.rejected",
        run_id=run_id,
        session_id=session_id,
        provider=provider,
        policy_rollout=rollout,
        candidate_count=count,
    )


def flush_completed(
    *,
    run_id: str,
    session_id: str,
    provider: str,
    rollout: str,
    proposed: int,
    committed: int,
    rejected: int,
) -> MemoryEvent:
    return MemoryEvent(
        type="memory.flush.completed",
        run_id=run_id,
        session_id=session_id,
        provider=provider,
        policy_rollout=rollout,
        candidate_count=proposed,
        metadata={"committed": committed, "rejected": rejected},
    )


def flush_failed(
    *,
    run_id: str,
    session_id: str,
    provider: str,
    rollout: str,
    error_code: str,
    error_message: str,
    retryable: bool = True,
) -> MemoryEvent:
    return MemoryEvent(
        type="memory.flush.failed",
        run_id=run_id,
        session_id=session_id,
        provider=provider,
        policy_rollout=rollout,
        error_code=error_code,
        error_message=error_message,
        retryable=retryable,
    )


__all__ = [
    "MemoryEvent",
    "MemoryEventType",
    "candidate_created",
    "candidate_rejected",
    "flush_completed",
    "flush_failed",
    "recall_completed",
    "recall_empty",
    "recall_failed",
    "recall_projected",
]
