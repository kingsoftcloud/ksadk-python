# Agent Kernel 稳定错误码。code 是 wire 合同，禁止改写既有语义。
from __future__ import annotations

from typing import Any

ERROR_CODES = frozenset(
    {
        "invalid_command",
        "invalid_permit",
        "unsupported",
        "queue_full",
        "stale_fence",
        "persistence_uncertain",
        "contract_mismatch",
        "runtime_interaction_unavailable",
    }
)

RETRYABLE_CODES = frozenset({"queue_full", "persistence_uncertain"})


class AgentKernelError(Exception):
    """AgentKernel 层统一错误。code 必须取自 ERROR_CODES。"""

    def __init__(self, code: str, message: str, *, retryable: bool | None = None, details: dict[str, Any] | None = None):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown agent kernel error code: {code}")
        self.code = code
        self.message = message
        self.retryable = RETRYABLE_CODES.get(code, False) if retryable is None else retryable
        # details 禁止携带 Secret 或 authorization token 原文，只放引用或摘要。
        self.details = details or {}
        super().__init__(f"{code}: {message}")


class InvalidCommandError(AgentKernelError):
    def __init__(self, message: str, **kwargs):
        super().__init__("invalid_command", message, retryable=False, **kwargs)


class InvalidPermitError(AgentKernelError):
    def __init__(self, message: str, **kwargs):
        super().__init__("invalid_permit", message, retryable=False, **kwargs)


class UnsupportedError(AgentKernelError):
    def __init__(self, message: str, **kwargs):
        super().__init__("unsupported", message, retryable=False, **kwargs)


class UnsupportedControlError(AgentKernelError, RuntimeError):
    """Control 动词在 capability matrix 中声明为 unsupported 时的 fail-closed 异常。

    继承 ``RuntimeError`` 以保持既有 ``except RuntimeError`` 调用点兼容;
    wire 错误码复用稳定的 ``unsupported``,不新增 code。
    """

    def __init__(self, message: str, **kwargs):
        AgentKernelError.__init__(self, "unsupported", message, retryable=False, **kwargs)


class QueueFullError(AgentKernelError):
    def __init__(self, message: str, **kwargs):
        super().__init__("queue_full", message, retryable=True, **kwargs)


class StaleFenceError(AgentKernelError):
    def __init__(self, message: str, **kwargs):
        super().__init__("stale_fence", message, retryable=False, **kwargs)


class PersistenceUncertainError(AgentKernelError):
    def __init__(self, message: str, **kwargs):
        super().__init__("persistence_uncertain", message, retryable=True, **kwargs)


class ContractMismatchError(AgentKernelError):
    def __init__(self, message: str, **kwargs):
        super().__init__("contract_mismatch", message, retryable=False, **kwargs)
