"""HarnessState v1 — Checkpoint 之外的影子运行状态（plan §6.2/§6.2.1）。

引擎图 State 只存最小路由信息；本状态由 Harness Runtime 持有，持久化到
Session/Transcript 存储。恢复时先恢复图 Checkpoint（路由位置），再从
Transcript 重建本状态；不一致以 Transcript 为准并记录 context.recovered。
大 Tool Result / Artifact 只存引用，正文不进 State。
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    PAUSED = "paused"
    COMPACTING = "compacting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: MessageRole
    content: str = Field(default="", max_length=1_048_576)
    tool_call_id: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=256)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: 大结果外置为引用（plan §6.2：Checkpoint 不无限膨胀）。
    result_ref: str | None = Field(default=None, max_length=512)
    result_preview: str = Field(default="", max_length=2048)
    status: str = Field(default="pending", pattern=r"^(pending|succeeded|failed|denied)$")


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1, max_length=128)
    call_id: str = Field(min_length=1, max_length=128)
    risk_level: str = Field(default="medium", max_length=32)
    reason: str = Field(default="", max_length=4096)
    requested_at: float = Field(default_factory=time.time)


class MemoryRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_ref: str = Field(min_length=1, max_length=512)
    scope: str = Field(default="session", max_length=32)


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_ref: str = Field(min_length=1, max_length=512)
    digest: str | None = Field(default=None, max_length=128)
    mime_type: str | None = Field(default=None, max_length=128)


class RetryState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consecutive_failures: int = Field(default=0, ge=0, le=32)
    emergency_compaction_retries: int = Field(default=0, ge=0, le=3)
    last_error_kind: str | None = Field(default=None, max_length=64)


class WorkingContext(BaseModel):
    """当前任务工作上下文（plan §8.5）：Session/Task 生命周期，非长期 Memory。"""

    model_config = ConfigDict(extra="forbid")

    #: 结构化 Patch 版本（长任务方案 §6.1）：每次成功 Patch +1，乐观并发控制。
    version: int = Field(default=0, ge=0)
    goal: str = Field(default="", max_length=8192)
    confirmed_constraints: tuple[str, ...] = Field(default=(), max_length=64)
    open_questions: tuple[str, ...] = Field(default=(), max_length=64)
    plan: str = Field(default="", max_length=16384)
    verified_facts: tuple[str, ...] = Field(default=(), max_length=256)
    artifact_refs: tuple[ArtifactRef, ...] = Field(default=(), max_length=64)
    recent_tool_failures: tuple[str, ...] = Field(default=(), max_length=32)


class CapabilitySnapshot(BaseModel):
    """本 Run 实际可见的能力清单（含降级标记）。"""

    model_config = ConfigDict(extra="forbid")

    entries: tuple[str, ...] = Field(default=(), max_length=256)
    degraded: tuple[str, ...] = Field(default=(), max_length=256)


class HarnessState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(default_factory=lambda: f"hr-{uuid.uuid4().hex[:16]}")

    messages: list[Message] = Field(default_factory=list)
    working_context: WorkingContext = Field(default_factory=WorkingContext)
    memory_refs: list[MemoryRef] = Field(default_factory=list)
    capability_snapshot: CapabilitySnapshot = Field(default_factory=CapabilitySnapshot)
    pending_tool_calls: list[ToolCall] = Field(default_factory=list)
    pending_approval: ApprovalRequest | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    retry_state: RetryState = Field(default_factory=RetryState)
    status: RunStatus = Field(default=RunStatus.PENDING)
    turn_count: int = Field(default=0, ge=0, le=1024)

    def checkpoint_key(self) -> str:
        """与图 Checkpoint 关联的键（plan §6.2.1 一致性规则）。"""
        return f"{self.tenant_id}/{self.agent_id}/{self.session_id}/{self.run_id}"


__all__ = [
    "ApprovalRequest",
    "ArtifactRef",
    "CapabilitySnapshot",
    "HarnessState",
    "MemoryRef",
    "Message",
    "MessageRole",
    "RetryState",
    "RunStatus",
    "ToolCall",
    "WorkingContext",
]
