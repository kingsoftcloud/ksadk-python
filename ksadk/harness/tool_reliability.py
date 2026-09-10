"""Tool 执行可靠性合同。

Harness 不把所有 Tool 包装成虚假的 ``exactly-once``。本模块根据 Tool 的
副作用、Receipt 和 Transport 幂等能力，给出可审计的交付语义：

- ``replay_safe``：只读或无副作用，重放不会产生业务副作用；
- ``effectively_once``：Receipt 关闭提交后的重放窗口，Transport 幂等键关闭
  外部成功但 Receipt 尚未提交的窗口；
- ``at_least_once``：Receipt 只能关闭提交后的窗口，提交前崩溃可能重复副作用；
- ``best_effort``：没有 Receipt，且工具副作用合同未知或不可重试。

这里刻意不使用 ``exactly_once``：跨网络、跨存储事务没有统一原子提交时，平台
只能声明经过验证的效果语义，不能承诺数学意义上的恰好一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ToolDeliverySemantics(str, Enum):
    """Harness 对单次 Tool Call 的诚实交付语义。"""

    REPLAY_SAFE = "replay_safe"
    EFFECTIVELY_ONCE = "effectively_once"
    AT_LEAST_ONCE = "at_least_once"
    BEST_EFFORT = "best_effort"


_REPLAY_SAFE_SIDE_EFFECTS = frozenset({"none", "read"})


@dataclass(frozen=True)
class ToolReliability:
    """一项 Tool 在本次装配中的可靠性声明。"""

    semantics: ToolDeliverySemantics
    side_effect: str
    receipt_enabled: bool
    transport_idempotent: bool

    def to_event_payload(self) -> dict[str, object]:
        """生成 metadata-only 事件投影，不包含幂等键或业务参数。"""
        return {
            "semantics": self.semantics.value,
            "side_effect": self.side_effect,
            "receipt_enabled": self.receipt_enabled,
            "transport_idempotent": self.transport_idempotent,
        }


def classify_tool_reliability(
    *,
    side_effect: str = "unknown",
    receipt_enabled: bool = False,
    transport_idempotent: bool = False,
) -> ToolReliability:
    """根据真实能力分类，不因配置缺失抬高承诺等级。"""
    normalized = str(side_effect or "unknown").lower()
    if normalized in _REPLAY_SAFE_SIDE_EFFECTS:
        semantics = ToolDeliverySemantics.REPLAY_SAFE
    elif receipt_enabled and transport_idempotent:
        semantics = ToolDeliverySemantics.EFFECTIVELY_ONCE
    elif receipt_enabled:
        semantics = ToolDeliverySemantics.AT_LEAST_ONCE
    else:
        semantics = ToolDeliverySemantics.BEST_EFFORT
    return ToolReliability(
        semantics=semantics,
        side_effect=normalized,
        receipt_enabled=receipt_enabled,
        transport_idempotent=transport_idempotent,
    )


__all__ = [
    "ToolDeliverySemantics",
    "ToolReliability",
    "classify_tool_reliability",
]
