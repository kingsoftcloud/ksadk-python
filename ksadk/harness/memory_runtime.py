"""Harness Memory Runtime（plan §9）——受控写入 + Core Memory Block 读取。

Phase 2 交付（plan §17）：
- Core Memory Block：常驻 Block 数量与总预算有限（§9.2）；
- 长期 Memory 检索与受控写入（§9.3 写入管线）。

复用 master PCM 模块（本分支已移植）：
- ``MemoryCoordinator``——recall / flush_candidates / propose_and_commit 编排；
- ``MemoryPolicy.evaluate``——来源/敏感标签/阈值决策表；
- ``SqliteMemoryProvider``——本地 SQLite 后端（租户隔离）。

Harness 侧新增：
- 写入管线的 Source Validation + 权限检查（writable_by / scope allowlist 与
  HarnessSpec.memory_policy 对齐）+ 审计事件（MEMORY_WRITE / MEMORY_CONFLICT）；
- 引擎事件桥（RuntimeEvent 投影）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.spec import HarnessSpec
from ksadk.memory.coordinator import MemoryCoordinator
from ksadk.memory.models import (
    CoreMemoryRequest,
    MemoryCandidate,
    MemoryOperation,
    MemoryRecord,
    MemoryScope,
    MemorySearchRequest,
    MemorySearchResult,
)
from ksadk.memory.policy import MemoryEvaluation
from ksadk.memory.providers.local_sqlite import SqliteMemoryProvider

#: MemoryScope（PCM）→ Harness memory_policy scope allowlist 值。
_SCOPE_MAP: dict[str, str] = {
    "user": "user",
    "agent": "agent",
    "workspace": "org",
    "org": "org",
}


class HarnessMemoryError(RuntimeError):
    """违反受控写入约束（§9.3 禁止清单）。"""


@dataclass(frozen=True)
class MemoryWriteRequest:
    """一次受控写入请求（来源 + 目标 scope + 审计锚点）。"""

    operation: MemoryOperation
    content: str
    scope: MemoryScope
    scope_id: str
    source: str  # 来源标识（如 user_explicit / tool_result / extraction）
    source_event_id: str = ""
    confidence: float = 0.8
    importance: float = 0.6
    memory_type: str = "fact"
    slot_key: str = ""
    sensitive_labels: tuple[str, ...] = ()
    reason: str = ""
    # ---- 长任务方案 §7.2 兼容扩展 ----
    source_artifact_refs: tuple[str, ...] = ()
    sensitivity: str = "none"
    write_policy: str = "auto"
    #: TTL（ISO 日期；空 = 不过期）。时效数据必须有来源和 TTL。
    expires_at: str = ""


class HarnessMemoryRuntime:
    """Core Memory 读取 + 受控写入（§9.2 / §9.3）。"""

    def __init__(
        self,
        coordinator: MemoryCoordinator,
        *,
        max_core_blocks: int = 8,
        max_core_tokens: int = 4096,
    ) -> None:
        self._coordinator = coordinator
        self._max_core_blocks = max_core_blocks
        self._max_core_tokens = max_core_tokens

    @classmethod
    def local_sqlite(
        cls,
        *,
        db_path: str = ":memory:",
        tenant_id: str = "local",
        workspace_id: str = "local",
        **kwargs: Any,
    ) -> "HarnessMemoryRuntime":
        """本地 SQLite 默认装配（租户隔离由 provider 强制）。"""
        provider = SqliteMemoryProvider(
            db_path=db_path, tenant_id=tenant_id, workspace_id=workspace_id
        )
        coordinator = MemoryCoordinator(provider, tenant_id=tenant_id, workspace_id=workspace_id)
        return cls(coordinator, **kwargs)

    @property
    def coordinator(self) -> MemoryCoordinator:
        return self._coordinator

    # ------------------------------------------------------------- 读取

    def get(self, memory_id: str) -> MemoryRecord | None:
        return self._coordinator.provider.get(memory_id)

    # ------------------------------------------------- 用户纠错 Runtime API
    # 长任务方案 §10 P1：「用户纠错、删除和锁定所需 Runtime API 合同」。
    # 三个动作全部带 reason（审计）、走乐观版本（expected_version）、
    # 返回审计事件；失败不静默。

    def correct(
        self,
        *,
        memory_id: str,
        new_content: str,
        reason: str,
        actor: str,
        expected_version: int | None = None,
    ) -> tuple[MemoryRecord, RuntimeEvent | None]:
        """用户纠错：同槽位更新（旧记录 superseded，审计链保留）。"""
        from dataclasses import replace

        from ksadk.memory.policy import content_hash as _hash

        record = self._require_record(memory_id)
        if expected_version is not None and int(expected_version) != record.version:
            raise HarnessMemoryError(
                f"version_conflict:expected={expected_version},actual={record.version}"
            )
        existing_hash = record.content_hash or _hash(record.content)
        # 与 Coordinator update 语义一致：新事实用新 memory_id，旧记录 superseded
        # 移出 active 召回集合但保留审计行。
        new_id = f"mem_{uuid.uuid4().hex[:24]}"
        updated = replace(
            record,
            memory_id=new_id,
            content=new_content,
            summary=new_content[:200],
            content_hash=_hash(new_content),
            version=record.version + 1,
            status="active",
            supersedes=(record.memory_id,),
            metadata={
                **record.metadata,
                "corrected_by": actor,
                "correction_reason": reason,
            },
        )
        if existing_hash == updated.content_hash:
            raise HarnessMemoryError("纠错内容与现有记录相同（no-op 拒绝）")
        self._coordinator.provider.upsert(updated, expected_version=expected_version)
        # 旧记录标记 superseded（移出 active 召回集合，保留审计）。
        self._mark_superseded(record, superseded_by=updated.memory_id, reason=reason)
        return updated, self._correction_event("update", updated, actor, reason)

    def forget(
        self,
        *,
        memory_id: str,
        reason: str,
        actor: str,
        hard: bool = False,
    ) -> tuple[bool, RuntimeEvent | None]:
        """用户遗忘：默认逻辑删除（tombstone，§12 可审计）。"""
        record = self._require_record(memory_id)
        if record.write_policy == "locked":
            raise HarnessMemoryError("locked 记录禁止删除（先解锁）")
        deleted = self._coordinator.delete(
            memory_id, scope=record.scope, scope_id=record.scope_id, hard=hard
        )
        event = self._correction_event("delete", record, actor, reason)
        return deleted, event

    def set_lock(
        self,
        *,
        memory_id: str,
        locked: bool,
        reason: str,
        actor: str,
        expected_version: int | None = None,
    ) -> tuple[MemoryRecord, RuntimeEvent | None]:
        """锁定/解锁（write_policy locked ↔ auto）。locked 记录对写入管线只读。"""
        from dataclasses import replace

        record = self._require_record(memory_id)
        if expected_version is not None and int(expected_version) != record.version:
            raise HarnessMemoryError(
                f"version_conflict:expected={expected_version},actual={record.version}"
            )
        updated = replace(
            record,
            write_policy="locked" if locked else "auto",
            version=record.version + 1,
            metadata={
                **record.metadata,
                "lock_changed_by": actor,
                "lock_reason": reason,
            },
        )
        self._coordinator.provider.upsert(updated, expected_version=expected_version)
        operation = "lock" if locked else "unlock"
        return updated, self._correction_event(operation, updated, actor, reason)

    # ------------------------------------------------------------- 校验辅助

    def _require_record(self, memory_id: str) -> MemoryRecord:
        record = self._coordinator.provider.get(memory_id)
        if record is None:
            raise HarnessMemoryError(f"memory not found: {memory_id}")
        return record

    def _mark_superseded(self, record: MemoryRecord, *, superseded_by: str, reason: str) -> None:
        """旧记录落 superseded（移出 active 召回集合，保留审计；provider 直写）。"""
        from dataclasses import replace

        superseded = replace(
            record,
            status="superseded",
            metadata={
                **record.metadata,
                "superseded_by": superseded_by,
                "supersede_reason": reason,
            },
        )
        self._coordinator.provider.upsert(superseded, expected_version=None)

    @staticmethod
    def _correction_event(
        operation: str, record: MemoryRecord, actor: str, reason: str
    ) -> RuntimeEvent | None:
        payload = {
            "scope": str(record.scope),
            "memory_ref": record.memory_id,
            "decision": "commit",
            "operation": operation,
            "source": f"user_correction:{actor}",
            "reason": reason,
        }
        return RuntimeEvent.create(
            EventType.MEMORY_WRITE,
            agent_id="",
            user_id="",
            session_id="",
            invocation_id=record.memory_id,
            seq_id=1,
            payload=payload,
        )

    def recall(
        self, *, query: str, scopes: list[tuple[MemoryScope, str]], top_k: int = 8
    ) -> MemorySearchResult:
        """检索长期 Memory（§9.1）。Provider 故障返回空结果，不抛错污染模型输入。"""
        request = MemorySearchRequest(
            query=query, scopes=scopes, memory_types=["fact", "profile"], top_k=top_k
        )
        return self._coordinator.recall(request)

    def list_core(self, *, scopes: list[tuple[MemoryScope, str]]) -> list[MemoryRecord]:
        """Core Memory Block（§9.2）：常驻块数量与预算有限，超出部分交由按需检索。"""
        request = CoreMemoryRequest(
            scopes=scopes,
            max_blocks=self._max_core_blocks,
            max_tokens=self._max_core_tokens,
        )
        return self._coordinator.list_core(request)

    # ------------------------------------------------------------- 写入

    def write(
        self, request: MemoryWriteRequest, spec: HarnessSpec, *, run_id: str
    ) -> tuple[MemoryEvaluation, RuntimeEvent | None]:
        """受控写入（§9.3 管线：校验 → 权限 → 决策 → commit → 审计事件）。

        返回 (评估结果, 审计事件)。违反禁止清单直接抛 :class:`HarnessMemoryError`
        （不静默丢弃——调用方必须能感知）。
        """
        self._validate_source(request)
        self._validate_scope(request, spec)
        self._validate_sensitivity(request)

        candidate = MemoryCandidate(
            candidate_id=f"memc_{run_id}",
            operation=request.operation,
            memory_type=request.memory_type,
            scope=request.scope,
            scope_id=request.scope_id,
            content=request.content,
            confidence=request.confidence,
            importance=request.importance,
            source_event_ids=[request.source_event_id] if request.source_event_id else [],
            sensitive_labels=list(request.sensitive_labels),  # type: ignore[arg-type]
            reason=request.reason or f"source={request.source}",
            slot_key=request.slot_key,
            source_artifact_refs=request.source_artifact_refs,
            sensitivity=request.sensitivity,  # type: ignore[arg-type]
            write_policy=request.write_policy,  # type: ignore[arg-type]
            expires_at=request.expires_at,
        )
        evaluation = self._commit_controlled(candidate)
        event = self._audit_event(run_id, request, evaluation)
        return evaluation, event

    def _commit_controlled(self, candidate: MemoryCandidate) -> MemoryEvaluation:
        """去重与纠错（缺口 3）：同槽位已有 active 事实时——

        - 内容一致 → ``duplicate_content`` 拒绝（不重复写入）；
        - 内容不一致 → add 升级为 update（supersede，保留旧事实审计链）。
        """
        from dataclasses import replace

        from ksadk.memory.policy import content_hash

        existing = self._coordinator.find_existing_for_candidate(candidate)
        if existing is None:
            return self._coordinator.propose_and_commit(candidate)
        if existing.write_policy == "locked":
            # 长任务方案 §7.2：locked 记录禁止管线自动更新（须显式解锁）。
            return MemoryEvaluation(
                decision="reject",
                operation="ignore",
                reason="locked_record",
                conflicts_with=[existing.memory_id],
            )
        if existing.content_hash == content_hash(candidate.content):
            return MemoryEvaluation(
                decision="reject",
                operation="ignore",
                reason="duplicate_content",
                conflicts_with=[existing.memory_id],
            )
        if candidate.operation == "add":
            candidate = replace(candidate, operation="update")
        return self._coordinator.propose_and_commit(candidate, existing=existing)

    # ------------------------------------------------------------- 校验

    @staticmethod
    def _validate_source(request: MemoryWriteRequest) -> None:
        if not request.source:
            raise HarnessMemoryError("Memory 写入必须有来源标识（§9.3：无来源事实禁止写入）")
        if request.source == "raw_transcript":
            raise HarnessMemoryError("禁止把整段聊天直接写入长期 Memory（§9.3）")

    @staticmethod
    def _validate_scope(request: MemoryWriteRequest, spec: HarnessSpec) -> None:
        allowed = set(spec.memory_policy.scopes) if spec.memory_policy.enabled else set()
        mapped = _SCOPE_MAP.get(request.scope, request.scope)
        if request.scope in {"user", "workspace", "org"} and not spec.memory_policy.enabled:
            # session/run 作用域不落长期 Memory，不经此管线的 org/user 写入一律拒绝。
            raise HarnessMemoryError(
                f"Memory 未启用（memory_policy.enabled=false），拒绝写入 scope={request.scope}"
            )
        if mapped not in allowed:
            raise HarnessMemoryError(
                f"scope {request.scope!r} 不在 HarnessSpec 允许列表 {sorted(allowed)}（越权写入）"
            )
        if request.scope == "org" and request.source != "user_explicit":
            raise HarnessMemoryError(
                "组织级 Memory 只允许用户明确指令写入（§9.3：无来源事实禁入 org）"
            )

    @staticmethod
    def _validate_sensitivity(request: MemoryWriteRequest) -> None:
        if request.sensitive_labels:
            raise HarnessMemoryError(
                f"敏感标签 {list(request.sensitive_labels)} 禁止写入长期 Memory（须先脱敏）"
            )

    @staticmethod
    def _audit_event(
        run_id: str, request: MemoryWriteRequest, evaluation: MemoryEvaluation
    ) -> RuntimeEvent | None:
        """审计事件（§9.3：写入必留痕）。拒绝也是审计（MEMORY_WRITE 带决策）。"""
        if evaluation.conflicts_with:
            payload = {
                "scope": request.scope,
                "memory_ref": request.scope_id,
                "conflicting_ref": evaluation.conflicts_with[0],
                "decision": evaluation.decision,
                "operation": evaluation.operation,
                "source": request.source,
                "reason": evaluation.reason,
            }
        elif evaluation.decision == "commit":
            payload = {
                "scope": request.scope,
                "memory_ref": request.scope_id,
                "decision": evaluation.decision,
                "operation": evaluation.operation,
                "source": request.source,
                "reason": evaluation.reason,
            }
        else:
            payload = {
                "scope": request.scope,
                "memory_ref": request.scope_id,
                "decision": evaluation.decision,
                "source": request.source,
                "reason": evaluation.reason,
            }
        event_type = (
            EventType.MEMORY_CONFLICT if evaluation.conflicts_with else EventType.MEMORY_WRITE
        )
        required = {"scope"}
        if event_type == EventType.MEMORY_CONFLICT:
            required = {"scope", "conflicting_ref"}
        if not required <= set(payload):
            return None
        return RuntimeEvent.create(
            event_type,
            agent_id="",
            user_id="",
            session_id="",
            invocation_id=run_id,
            seq_id=1,
            payload=payload,
        )


__all__ = [
    "HarnessMemoryError",
    "HarnessMemoryRuntime",
    "MemoryWriteRequest",
]
