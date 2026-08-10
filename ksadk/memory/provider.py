"""MemoryProvider Protocol 与能力声明（方案 §10.5）。

Provider 合同是本地与云端一致性的运行时边界：本地用 SQLite Provider，云端用 HTTP/SDK
Provider，二者共用同一套契约测试（方案 §17.4）。``expected_version`` 用于并发更新乐观锁，
避免多个 Run 并发更新同一偏好时静默覆盖。
"""

from __future__ import annotations

from typing import Protocol

from ksadk.memory.models import (
    CoreMemoryRequest,
    MemoryCapabilities,
    MemoryDeleteRequest,
    MemoryDeleteResult,
    MemoryRecord,
    MemorySearchRequest,
    MemorySearchResult,
)


class MemoryProvider(Protocol):
    """平台长期记忆 Provider 合同（方案 §10.5）。

    实现必须按 ``scope`` 隔离；``scope_id`` 由可信 Principal/Runtime Projection 决定，
    不能信任用户自行提交的 scope_id（方案 §19）。异常路径不得把错误文本塞进检索结果正文。
    """

    def capabilities(self) -> MemoryCapabilities: ...

    def search(self, request: MemorySearchRequest) -> MemorySearchResult: ...

    def get(self, memory_id: str) -> MemoryRecord | None: ...

    def upsert(
        self, record: MemoryRecord, *, expected_version: int | None
    ) -> MemoryRecord: ...

    def delete(self, request: MemoryDeleteRequest) -> MemoryDeleteResult: ...

    def list_core(self, request: CoreMemoryRequest) -> list[MemoryRecord]: ...


__all__ = [
    "MemoryProvider",
]
