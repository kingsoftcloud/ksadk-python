# -*- coding: utf-8 -*-
"""command -> RuntimeAdapter 方法映射与 capability 判定（Phase 1 Task 6 Step 5）。

映射表是唯一事实来源：enqueue 只在没有 active Run 时执行 start；
steer/inject 只传给 native adapter method；每条命令产生的
claimed/completed/rejected/discarded ControlEvent 都以 command_id 为
causation_id（claimed/completed 由 AgentKernelStore 在状态迁移时写入）。
"""

from __future__ import annotations

from typing import Callable

from ksadk.kernel.contracts import (
    AgentControlCommand,
    ControlError,
    RuntimeCapabilityMatrix,
    SessionEventEnvelope,
)
from ksadk.kernel.errors import UnsupportedControlError
from ksadk.kernel.store import control_event

# command_type -> RuntimeAdapter 方法名。submit_interaction 不再静态映射到
# adapter.submit：Worker 载入权威 InteractionRecord 并分发给其绑定的
# InteractionProvider（live submit / durable resume / unavailable）。
COMMAND_HANDLERS: dict[str, str] = {
    "enqueue": "start",
    "steer": "steer",
    "inject": "inject",
    "interrupt": "cancel",
    "pause": "pause",
    "resume": "resume",
    "submit_interaction": "submit_interaction",
}

# command_type -> RuntimeCapabilityMatrix 字段；enqueue 无 capability 门槛。
COMMAND_CAPABILITIES: dict[str, str | None] = {
    "enqueue": None,
    "steer": "steer",
    "inject": "inject",
    "interrupt": "cancel",
    "pause": "pause",
    "resume": "resume",
    "submit_interaction": "submit_interaction",
}

# contracts.ResumeTarget.kind -> adapter ResumeTarget.kind。
RESUME_TARGET_KINDS: dict[str, str] = {
    "checkpoint": "checkpoint_id",
    "continuation": "thread_id",
    "run": "invocation_id",
}


def capability_of(
    command_type: str, matrix: RuntimeCapabilityMatrix
) -> tuple[str | None, object]:
    """返回 (capability 字段名, RuntimeCapability)；enqueue 为 (None, None)。"""

    field = COMMAND_CAPABILITIES[command_type]
    if field is None:
        return None, None
    return field, getattr(matrix, field)


def ensure_supported(command_type: str, matrix: RuntimeCapabilityMatrix) -> None:
    """命令动词必须在 capability matrix 中 native supported，否则 fail closed。"""

    field, capability = capability_of(command_type, matrix)
    if field is not None and not capability.supported:
        raise UnsupportedControlError(
            f"runtime capability {field} is unavailable: {capability.reason}",
            details={"capability": field, "reason": capability.reason},
        )


def rejection_receipt_error(
    *, status: str, code: str, message: str, retryable: bool
) -> ControlError:
    return ControlError(code=code, message=message, retryable=retryable)


def command_rejected_event(
    command: AgentControlCommand, *, status: str, reason: str
) -> SessionEventEnvelope:
    """脱敏审计事件：只引用 command_id/status/reason，不携带 permit 内容。"""

    return control_event(
        session_id=command.session_id,
        event_type="control.command_rejected",
        payload={
            "command_id": str(command.command_id),
            "status": status,
            "reason": reason,
        },
        causation_id=str(command.command_id),
    )


CapabilityProvider = Callable[[], RuntimeCapabilityMatrix]

__all__ = [
    "COMMAND_HANDLERS",
    "COMMAND_CAPABILITIES",
    "RESUME_TARGET_KINDS",
    "CapabilityProvider",
    "capability_of",
    "command_rejected_event",
    "ensure_supported",
    "rejection_receipt_error",
]
