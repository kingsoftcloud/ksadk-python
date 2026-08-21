"""Context 投影语义信封（方案 6.3 / 8.8）。

第一个 PR 只定义最小结构，供 shadow 计划和后续 Projection 复用；不实现任何投影逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ksadk.context_engine.capabilities import ContextAccuracy, ContextIntegrationMode

PROJECTION_VERSION = "v1"


@dataclass(frozen=True)
class ProjectionResult:
    """Runner 投影结果信封。

    仅在 ``accounting_accuracy=exact`` 时才代表最终模型输入；native/assisted 路径
    需结合 Runner 回报生成实际使用记录。
    """

    projection_id: str
    runner_type: str
    integration_mode: ContextIntegrationMode
    projection_version: str
    accounting_accuracy: ContextAccuracy
    estimated_tokens: int | None = None
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
