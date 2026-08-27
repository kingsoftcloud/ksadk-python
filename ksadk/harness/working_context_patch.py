"""WorkingContextPatch（长任务方案 §6.1 / P1）。

Working Context 更新必须走结构化 Patch——带版本（乐观并发）、来源事件和
理由，禁止每轮无条件全量重写：

- ``PatchOperation``：set / append / remove 单字段操作；
- ``WorkingContextPatch``：base_version + operations + 来源 + 理由；
- :func:`apply_patch`：校验版本 → 逐操作应用 → version+1。版本冲突抛
  :class:`WorkingContextVersionError`（不静默覆盖）。

确定性维护函数（``record_tool_result`` 等）内部也走 Patch——Working Context
的每次变化都有版本和审计锚点，可从 Transcript 重放重建。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ksadk.harness.state import WorkingContext

#: 单个 Patch 的操作数上限——防御异常膨胀的全量重写。
_MAX_OPERATIONS = 32


class WorkingContextVersionError(RuntimeError):
    """Patch 的 base_version 与当前 version 不一致（并发更新冲突）。"""


@dataclass(frozen=True)
class PatchOperation:
    """单个字段操作。"""

    op: str  # "set" | "append" | "remove"
    field_name: str
    value: Any = None


@dataclass(frozen=True)
class WorkingContextPatch:
    """一次 Working Context 更新（版本 + 操作 + 来源 + 理由）。"""

    base_version: int
    operations: tuple[PatchOperation, ...] = field(default=())
    source_event_ids: tuple[str, ...] = field(default=())
    reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "base_version": self.base_version,
            "operations": [
                {"op": o.op, "field": o.field_name, "value": o.value}
                for o in self.operations
            ],
            "source_event_ids": list(self.source_event_ids),
            "reason": self.reason,
        }


def apply_patch(working: WorkingContext, patch: WorkingContextPatch) -> WorkingContext:
    """应用 Patch：版本校验 → 逐操作应用（走 pydantic 校验）→ version+1。

    版本不匹配抛 :class:`WorkingContextVersionError`；未知字段/非法值由
    WorkingContext 的 pydantic 校验兜底（``ValidationError``）。
    """
    if patch.base_version != working.version:
        raise WorkingContextVersionError(
            f"working context version conflict: patch base={patch.base_version}, "
            f"current={working.version}"
        )
    if len(patch.operations) > _MAX_OPERATIONS:
        raise ValueError(f"patch operations exceed limit {_MAX_OPERATIONS}")
    updates: dict[str, Any] = {}
    for operation in patch.operations:
        if operation.field_name not in WorkingContext.model_fields:
            raise ValueError(f"unknown working context field: {operation.field_name!r}")
        current = getattr(working, operation.field_name, updates.get(operation.field_name))
        if operation.op == "set":
            new_value = operation.value
        elif operation.op == "append":
            if not isinstance(current, tuple):
                raise ValueError(f"append 仅支持 tuple 字段: {operation.field_name}")
            new_value = current + (operation.value,)
        elif operation.op == "remove":
            if not isinstance(current, tuple):
                raise ValueError(f"remove 仅支持 tuple 字段: {operation.field_name}")
            new_value = tuple(v for v in current if v != operation.value)
        else:
            raise ValueError(f"unknown patch op: {operation.op!r}")
        updates[operation.field_name] = new_value
    return working.model_copy(update={**updates, "version": working.version + 1})


__all__ = [
    "PatchOperation",
    "WorkingContextPatch",
    "WorkingContextVersionError",
    "apply_patch",
]
