"""Memory v2 数据模型 —— MemoryRecord / MemoryCandidate / 检索请求与结果。

对齐技术实现方案 §10.2 / §10.3 / §10.6。本期支持三类长期记忆（profile/fact/episode），
跨 Session 精选事实，检索后才进入 Context。Skill/流程规则不作为 Memory；当前目标、当前
计划、近期文件、pending state 属于 Session WorkingState（见 ``semantic_summary``），不直接
写入长期记忆。

平台记忆与原生记忆边界（方案 §10.1）：
- **Platform portable memory**：由 ``MemoryProvider``/云端 Memory Service 管理，跨 Runtime、
  跨 Agent Revision/Build、跨部署的长期事实源，支持 scope、权限、版本、TTL、删除和审计。
- **Runtime-native memory**：Codex/Claude/OpenClaw/Hermes 自行维护的 thread/file/internal
  state，属 Runtime 局部能力，不能替代平台事实源。

公开类型从第一批开始版本化（``MEMORY_MODEL_VERSION``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MEMORY_MODEL_VERSION = "v1"

MemoryScope = Literal["user", "agent", "workspace", "org"]
"""记忆作用域。SDK 本地模式可将 tenant/workspace 设为明确本地值，但不能省略隔离字段。"""

MemoryType = Literal["profile", "fact", "episode"]
"""记忆类型（方案 §10.1）：profile=稳定偏好；fact=项目/业务事实；episode=可复用任务经历。"""

MemoryStatus = Literal["active", "superseded", "deleted", "expired"]
"""记忆状态。superseded/deleted/expired 不作为 active 返回。"""

MemoryOperation = Literal["add", "update", "delete", "ignore"]
"""候选操作（方案 §10.3）。"""

MemorySearchStatus = Literal["ok", "not_configured", "timeout", "unauthorized", "failed"]
"""检索结果状态（方案 §10.6 / §10.8）。异常不污染模型上下文。"""

# 敏感信息标签（方案 §19 / §10.4）。Candidate 进入 Provider 前必须执行 Secret/PII 检查。
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
    """一条平台长期记忆（方案 §10.2）。

    所有时间字段用 ISO-8601 字符串表达，避免 dataclass 跨进程/序列化携带 ``datetime`` 类型
    的不确定行为；``valid_from`` / ``valid_to`` / ``expires_at`` 为空串表示无界。``version``
    单调递增，``expected_version`` 用于并发更新乐观锁（方案 §10.5）。
    """

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
        """是否当前有效的 active 记录（status=active 且未过期）。"""
        if self.status != "active":
            return False
        if self.expires_at and now_iso and self.expires_at < now_iso:
            return False
        if self.valid_to and now_iso and self.valid_to < now_iso:
            return False
        return True


@dataclass(frozen=True)
class MemoryCandidate:
    """尚未提交的记忆写入候选（方案 §10.3 / §10.4）。

    ``conflicts_with`` 指向被该候选替代的旧 ``memory_id`` 列表；``sensitive_labels``
    非空且含任何非 ``none`` 的硬拒绝标签时，``MemoryPolicy.evaluate`` 必须返回 ``reject``。
    """

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

    def is_hard_rejected(self) -> bool:
        """含硬拒绝敏感标签（方案 §19）→ 必须拒绝，不得写入 Provider。"""
        return any(label != "none" for label in self.sensitive_labels)


@dataclass(frozen=True)
class MemorySearchRequest:
    """检索请求（方案 §10.6）。

    ``scopes`` 为 ``(MemoryScope, scope_id)`` 列表，Provider 必须按 scope 隔离返回。
    ``max_tokens`` 与 ``top_k`` 同时生效；按 ``max_tokens`` 装箱而非仅按 top_k。
    """

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
    """检索结果（方案 §10.6 / §10.8）。

    ``status`` 非 ``ok`` 时 ``records`` 为空且 ``error_code`` 非空；调用方据此降级，
    **不把错误字符串塞进模型上下文**（方案 §10.8）。
    """

    status: MemorySearchStatus
    records: list[MemoryRecord]
    error_code: str | None
    provider: str
    latency_ms: int
    accounting_accuracy: str
    truncated_by_budget: bool = False


@dataclass(frozen=True)
class MemoryCapabilities:
    """Provider 能力声明（方案 §10.5）。

    用于 Coordinator 决定是否可用语义检索、版本化更新、hard delete、TTL 等；不支持的能力
    必须诚实返回 ``False``，Coordinator 据此降级而非编造行为。
    """

    semantic_search: bool
    keyword_search: bool
    metadata_filter: bool
    versioned_update: bool
    hard_delete: bool
    ttl: bool
    max_record_chars: int


@dataclass(frozen=True)
class CoreMemoryBlock:
    """Core Memory block（方案 §10.7）：少量始终可用的 profile/fact。

    默认最多 8 个 block、总预算 4K tokens。block 更新也走候选、版本和安全检查。文件只是本地
    调试投影，不是云端事实源。
    """

    name: str
    description: str
    content: str
    max_tokens: int
    writable: bool
    source_memory_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MemoryDeleteRequest:
    """删除请求。支持 hard delete 能力声明（方案 §10.5 / §19）。"""

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
    """列出 Core Memory block 的请求。"""

    scopes: list[tuple[MemoryScope, str]]
    max_blocks: int = 8
    max_tokens: int = 4096
