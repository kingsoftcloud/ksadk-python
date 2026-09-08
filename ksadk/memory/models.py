"""长期记忆结构化模型、操作结果与稳定异常类型。

按《Hermes × KsADK 长期记忆实时性与纠错能力技术改造方案》§7.1/§7.2/§7.8 设计：

- LongTermMemoryRecord: 结构化记忆记录（memory_id 来自服务端返回值）
- MemoryWriteResult: 写入受理结果（accepted + queued/failed）
- MemoryMutationResult: update/delete 结果（updated/deleted/already_absent/not_found/failed）
- MemoryExtractionStatus: 后台提取状态
  （queued/extracting/extracted/duplicate_skipped/failed/unknown）
- MemoryOperationError 族: 稳定异常类型，供上层归一化处理

设计要点：
- memory_id 不能由正文 hash 临时生成；大部分情况稳定，但融合/人工编辑可能变化，
  不能作为永久业务主键长期缓存。
- 写入受理（accepted）与提取状态（extraction status）使用不同类型，
  避免布尔值同时表示"请求成功"和"已经可检索"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "LongTermMemoryRecord",
    "MemoryWriteResult",
    "MemoryMutationResult",
    "MemoryExtractionStatus",
    "MemoryOperationError",
    "MemoryNotFoundError",
    "UnsupportedMemoryOperation",
    "MemoryPermissionError",
    "MemoryConflictError",
    "SESSION_STATE_PENDING",
    "SESSION_STATE_EXTRACTING",
    "SESSION_STATE_EXTRACTED",
    "SESSION_STATE_DUPLICATE_SKIPPED",
    "SESSION_STATE_EXTRACT_FAILED",
    "map_session_state",
]

# ---- AICP ListSessions State 枚举（服务端已确认） ----
SESSION_STATE_PENDING = 0  # 待提取
SESSION_STATE_EXTRACTING = 50  # 提取中
SESSION_STATE_EXTRACTED = 100  # 提取成功
SESSION_STATE_DUPLICATE_SKIPPED = -50  # 重复跳过
SESSION_STATE_EXTRACT_FAILED = -100  # 提取失败


@dataclass(frozen=True)
class LongTermMemoryRecord:
    """结构化长期记忆记录。

    Attributes:
        memory_id: 服务端返回的记忆 ID。大部分情况稳定，但系统融合或
            人工编辑可能改变 ID，不能作为永久业务主键长期缓存。
        content: 记忆正文。
        score: 相关度得分；后端没有 score 时为 None。
        user_id: 归属用户 ID。
        session_id: 来源 Session ID（后端能提供时）。
        created_at: 创建时间（后端原始字符串，通常为 ISO 时间或毫秒时间戳）。
        updated_at: 更新时间（后端原始字符串）。
        metadata: 其他必要元数据。禁止放入 AK/SK、token、内部 endpoint 等敏感信息。
    """

    memory_id: str
    content: str
    score: float | None = None
    user_id: str = ""
    session_id: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryWriteResult:
    """写入受理结果。

    status 只表达受理层状态：queued（已入队）/ failed（受理失败）。
    后续提取进展用 MemoryExtractionStatus 查询，不在写入热路径等待。
    """

    accepted: bool
    status: str  # queued | failed
    session_id: str = ""
    message: str = ""


@dataclass(frozen=True)
class MemoryMutationResult:
    """update/delete 操作结果。

    status:
        updated          - 原地更新成功；new_memory_id 为服务端返回的新句柄
        deleted          - 软删除成功
        already_absent   - 目标已不存在（如重复删除），与 deleted 分开审计
        not_found        - 目标记录不存在
        failed           - 操作失败
    """

    ok: bool
    memory_id: str
    new_memory_id: str = ""
    status: str = ""  # updated | deleted | already_absent | not_found | failed
    message: str = ""


@dataclass(frozen=True)
class MemoryExtractionStatus:
    """后台提取状态查询结果。

    status:
        queued           - 写请求已被服务端接受，等待后台处理
        extracting       - 后台已经开始提取
        extracted        - ListSessions.State=100，提取完成
        duplicate_skipped- State=-50，本次内容因重复被跳过（不是系统失败）
        failed           - State=-100 或查询过程失败
        unknown          - 状态未知（如 Session 未在 ListSessions 返回中）

    searchable 单独用布尔标志表达：State=100 后通过 ListMemories 确认目标
    记录可见时才为 True（见方案 §7.2：状态与 searchable 分离建模）。
    """

    session_id: str
    state: int | None
    status: str
    searchable: bool = False
    message: str = ""


def map_session_state(state: int | None) -> str:
    """将 AICP Session State 映射为统一提取状态字符串。"""
    mapping = {
        SESSION_STATE_PENDING: "queued",
        SESSION_STATE_EXTRACTING: "extracting",
        SESSION_STATE_EXTRACTED: "extracted",
        SESSION_STATE_DUPLICATE_SKIPPED: "duplicate_skipped",
        SESSION_STATE_EXTRACT_FAILED: "failed",
    }
    if state is None:
        return "unknown"
    return mapping.get(int(state), "unknown")


# ---- 稳定异常类型（§7.8） ----


class MemoryOperationError(RuntimeError):
    """长期记忆操作基础异常。子类供上层按类型归一化处理。"""


class MemoryNotFoundError(MemoryOperationError):
    """目标记忆记录不存在。"""


class UnsupportedMemoryOperation(MemoryOperationError):
    """当前 backend 不支持该操作。

    不支持某能力的 backend 必须显式抛出本异常（或返回 unsupported 结果），
    不能静默追加一条新记忆来模拟 update。
    """


class MemoryPermissionError(MemoryOperationError):
    """跨用户或越权访问记忆资源。"""


class MemoryConflictError(MemoryOperationError):
    """并发修改冲突（如记录已被融合导致 ID 变化）。"""


# ---- PCM v2 数据模型（feature-prompt-context-optimize 分支）----
# 与 master 的 LongTermMemoryRecord 共存；PCM 模块用这些类型

MEMORY_MODEL_VERSION = "v1"

MemoryScope = Literal["user", "agent", "workspace", "org"]
MemoryType = Literal["profile", "fact", "episode"]
MemoryStatus = Literal["active", "superseded", "deleted", "expired"]
MemoryOperation = Literal["add", "update", "delete", "ignore"]
MemorySearchStatus = Literal["ok", "not_configured", "timeout", "unauthorized", "failed"]
SensitiveLabel = Literal[
    "api_key",
    "secret_key",
    "access_key",
    "cookie",
    "auth_header",
    "signed_url",
    "dsn",
    "pii",
    "token",
    "binary",
    "none",
]


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    tenant_id: str
    workspace_id: str
    scope: MemoryScope
    scope_id: str
    memory_type: MemoryType
    content: str
    summary: str
    status: MemoryStatus
    confidence: float
    importance: float
    valid_from: str
    valid_to: str
    expires_at: str
    source_session_id: str
    source_event_ids: list[str]
    source_seq_range: tuple[int, int] | None
    content_hash: str
    version: int
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def is_active_now(self, *, now_iso: str = "") -> bool:
        if self.status != "active":
            return False
        if self.expires_at and now_iso and self.expires_at < now_iso:
            return False
        if self.valid_to and now_iso and self.valid_to < now_iso:
            return False
        return True


@dataclass(frozen=True)
class MemoryCandidate:
    candidate_id: str
    operation: MemoryOperation
    memory_type: MemoryType
    scope: MemoryScope
    scope_id: str
    content: str
    confidence: float
    importance: float
    source_event_ids: list[str]
    conflicts_with: list[str] = field(default_factory=list)
    sensitive_labels: list[SensitiveLabel] = field(default_factory=list)
    reason: str = ""
    slot_key: str = ""

    def is_hard_rejected(self) -> bool:
        return any(label != "none" for label in self.sensitive_labels)


@dataclass(frozen=True)
class MemorySearchRequest:
    query: str
    scopes: list[tuple[MemoryScope, str]]
    memory_types: list[MemoryType]
    top_k: int = 8
    max_tokens: int = 4000
    min_score: float = 0.45
    as_of: str = ""
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemorySearchResult:
    status: MemorySearchStatus
    records: list[MemoryRecord]
    error_code: str | None
    provider: str
    latency_ms: int
    accounting_accuracy: str
    truncated_by_budget: bool = False


@dataclass(frozen=True)
class MemoryCapabilities:
    semantic_search: bool
    keyword_search: bool
    metadata_filter: bool
    versioned_update: bool
    hard_delete: bool
    ttl: bool
    max_record_chars: int


@dataclass(frozen=True)
class CoreMemoryBlock:
    name: str
    description: str
    content: str
    max_tokens: int
    writable: bool
    source_memory_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MemoryDeleteRequest:
    memory_id: str
    scope: MemoryScope
    scope_id: str
    hard: bool = False


@dataclass(frozen=True)
class MemoryDeleteResult:
    status: MemorySearchStatus
    deleted: bool
    error_code: str | None = None


@dataclass(frozen=True)
class CoreMemoryRequest:
    scopes: list[tuple[MemoryScope, str]]
    max_blocks: int = 8
    max_tokens: int = 4096
