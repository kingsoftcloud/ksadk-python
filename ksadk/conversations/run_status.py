"""Agent run 状态枚举。

run_status 事件 content.status / metadata.status 的合法值集合。
用 Literal + frozenset 常量（非 Enum），因为值要直接序列化进 JSON 事件，
Enum 还需 .value 转换。

此模块刻意不依赖 ksadk 任何其他模块，避免 conversations.runtime ↔ server.app
之间的循环导入。canonical 定义在这里，runtime.py 和 server/app.py 都从本模块导入。
"""

from __future__ import annotations

from typing import Literal

RunStatus = Literal[
    # active
    "in_progress",
    "running",
    "resuming",
    "starting",
    # terminal
    "completed",
    "failed",
    "cancelled",
    "interrupted",
    "resume_failed",
    # 派生 / 兼容别名
    "checkpointed",
    "error",
    "canceled",
    "aborted",
]

RUN_STATUS_TERMINAL: frozenset[str] = frozenset(
    {
        "completed",
        "failed",
        "error",
        "cancelled",
        "canceled",
        "aborted",
        "interrupted",
        "resume_failed",
    }
)
RUN_STATUS_ACTIVE: frozenset[str] = frozenset(
    {
        "in_progress",
        "running",
        "resuming",
        "starting",
    }
)
