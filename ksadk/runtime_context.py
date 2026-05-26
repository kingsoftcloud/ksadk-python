from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Iterator


@dataclass
class PlatformInvocationContext:
    agent_id: str
    user_id: str
    session_id: str
    history: list[dict[str, Any]]
    input_content: list[dict[str, Any]]
    input_messages: list[dict[str, Any]]
    input_parts: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    attachment_results: list[dict[str, Any]]
    current_attachments: list[dict[str, Any]]
    current_attachment_results: list[dict[str, Any]]
    has_current_files: bool
    runner_type: str
    model: str | None = None
    model_options: dict[str, Any] | None = None
    kb_context: dict[str, Any] | None = None
    memory_context: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "history": list(self.history or []),
            "input_content": list(self.input_content or []),
            "input_messages": list(self.input_messages or []),
            "input_parts": list(self.input_parts or []),
            "attachments": list(self.attachments or []),
            "attachment_results": list(self.attachment_results or []),
            "current_attachments": list(self.current_attachments or []),
            "current_attachment_results": list(self.current_attachment_results or []),
            "has_current_files": self.has_current_files,
            "runner_type": self.runner_type,
            "model": self.model,
            "model_options": dict(self.model_options or {}),
        }


_CURRENT_PLATFORM_INVOCATION_CONTEXT: ContextVar[PlatformInvocationContext | None] = ContextVar(
    "ksadk_platform_invocation_context",
    default=None,
)


def get_current_invocation_context() -> PlatformInvocationContext | None:
    return _CURRENT_PLATFORM_INVOCATION_CONTEXT.get()


def set_current_invocation_context(
    context: PlatformInvocationContext | None,
) -> Token[PlatformInvocationContext | None]:
    return _CURRENT_PLATFORM_INVOCATION_CONTEXT.set(context)


def reset_current_invocation_context(
    token: Token[PlatformInvocationContext | None],
) -> None:
    _CURRENT_PLATFORM_INVOCATION_CONTEXT.reset(token)


@contextmanager
def platform_invocation_scope(
    context: PlatformInvocationContext | None,
) -> Iterator[PlatformInvocationContext | None]:
    token = set_current_invocation_context(context)
    try:
        yield context
    finally:
        reset_current_invocation_context(token)
