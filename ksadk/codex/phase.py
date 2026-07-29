"""codex_phase — Codex app-server agent message 的相位(phase)翻译 (goal-09 契约 2)。

镜像 `Wegent/executor/src/codex_phase.rs`:

- 流式 live 事件**不在每个 delta 上重复相位**——``item/started`` 带
  ``params.item.id`` + ``params.item.phase``;后续 ``item/agentMessage/delta`` 只带
  ``params.itemId`` + ``params.delta``(无 phase)。
- 因此必须**按 itemId 追踪相位,按解析出的相位路由 delta**;**不从文本内容推断相位**,
  不等 ``item/completed`` 才更新 UI。
- 映射到 RuntimeEvent schema 的 ``phase`` 字段(commentary/final_answer),
  **相位翻译独立成模块,不揉进 CodexRuntime 主逻辑,不混入最终答案**。

Codex 相位:``commentary`` / ``analysis`` 属过程解说(process),``final_answer`` 是
最终答案。映射到 RuntimeEvent:process → ``commentary``,final_answer → ``final_answer``。
"""

from __future__ import annotations

from typing import Any, Optional

#: Codex 相位 → RuntimeEvent phase 字段。analysis/commentary 都是过程解说。
_PROCESS_PHASES = frozenset({"analysis", "commentary"})

_PHASE_KEYS = ("phase", "channel")
_ITEM_ID_KEYS = ("itemId", "item_id", "id", "messageId", "message_id")


def _normalize_phase(value: str) -> str:
    # Pydantic's Python-mode dump renders enums as ``MessagePhase.final_answer``.
    # JSON-mode is used by the production client, while accepting this spelling
    # keeps replayed samples from older KSADK builds readable.
    return value.rsplit(".", 1)[-1].replace("_", "").replace("-", "").strip().lower()


def codex_phase_name(value: dict[str, Any]) -> Optional[str]:
    """从事件 params 里读相位字段(phase / channel)并归一化。"""
    if not isinstance(value, dict):
        return None
    for key in _PHASE_KEYS:
        raw = value.get(key)
        if isinstance(raw, str):
            normalized = _normalize_phase(raw)
            if normalized:
                return normalized
    return None


def codex_item_id(value: dict[str, Any]) -> Optional[str]:
    """从事件 params 里读 itemId(itemId/item_id/id/messageId/message_id)。"""
    if not isinstance(value, dict):
        return None
    for key in _ITEM_ID_KEYS:
        raw = value.get(key)
        if isinstance(raw, str) and raw:
            return raw
    return None


def runtime_phase_for(codex_phase: Optional[str]) -> Optional[str]:
    """把 codex 相位映射为 RuntimeEvent phase(commentary / final_answer)。"""
    if codex_phase is None:
        return None
    if codex_phase in _PROCESS_PHASES:
        return "commentary"
    if codex_phase == "finalanswer" or codex_phase == "final_answer":
        return "final_answer"
    return None


def _item_params(params: dict[str, Any]) -> dict[str, Any]:
    """取事件里嵌套的 ``item`` 子对象(若无则返回 params 本身),类型收窄为 dict。"""
    item = params.get("item")
    return item if isinstance(item, dict) else params


class CodexPhaseTracker:
    """按 itemId 追踪 assistant message 的相位,按相位路由流式 delta。"""

    def __init__(self) -> None:
        self._phases_by_item_id: dict[str, str] = {}

    def observe_item(self, params: dict[str, Any]) -> None:
        """item/started(或 reload/completed):记录 itemId -> phase。

        codex 真实流 agentMessage 的 phase 常为 null,但 agentMessage 就是最终回复
        (final_answer),reasoning 才是 commentary。phase 为 null 时按 item type 兜底:
        agentMessage → final_answer,reasoning → commentary。
        """
        item = _item_params(params)
        item_id = codex_item_id(item) or codex_item_id(params)
        phase = codex_phase_name(item) or codex_phase_name(params)
        if not phase and item_id:
            item_type = item.get("type") if isinstance(item, dict) else None
            if item_type == "agentMessage":
                phase = "final_answer"
            elif item_type == "reasoning":
                phase = "commentary"
        if item_id and phase:
            self._phases_by_item_id[item_id] = phase

    def phase_for_delta(self, params: dict[str, Any]) -> Optional[str]:
        """item/agentMessage/delta:优先事件自带 phase,否则按 itemId 查追踪表。"""
        direct = codex_phase_name(params)
        if direct:
            return direct
        item_id = codex_item_id(params)
        if item_id:
            return self._phases_by_item_id.get(item_id)
        return None

    def phase_for_item(self, params: dict[str, Any]) -> Optional[str]:
        """item/completed / reload:事件或 item 内的 phase,再退回追踪表。"""
        item = _item_params(params)
        direct = codex_phase_name(item) or codex_phase_name(params)
        if direct:
            return direct
        item_id = codex_item_id(item) or codex_item_id(params)
        if item_id:
            return self._phases_by_item_id.get(item_id)
        return None

    def runtime_phase_for_delta(self, params: dict[str, Any]) -> Optional[str]:
        """delta 的 RuntimeEvent phase(commentary/final_answer)。"""
        return runtime_phase_for(self.phase_for_delta(params))

    def runtime_phase_for_item(self, params: dict[str, Any]) -> Optional[str]:
        """item 的 RuntimeEvent phase(commentary/final_answer)。"""
        return runtime_phase_for(self.phase_for_item(params))

    def forget_item(self, params: dict[str, Any]) -> None:
        """item/completed 后清理追踪项。"""
        item = _item_params(params)
        item_id = codex_item_id(item) or codex_item_id(params)
        if item_id:
            self._phases_by_item_id.pop(item_id, None)


__all__ = [
    "CodexPhaseTracker",
    "codex_item_id",
    "codex_phase_name",
    "runtime_phase_for",
]
