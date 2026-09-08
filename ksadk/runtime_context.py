from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from ksadk.session_context import SessionContext

TRUSTED_IDENTITY_METADATA_KEY = "_agentengine_verified_identity"


def _identity_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class PlatformIdentityContext:
    """Verified application identity carried through one runtime invocation.

    Authentication adapters create this value after validating credentials and
    tenant membership.  It is deliberately separate from caller metadata and
    from the cloud ``account_id``/``user_id`` compatibility fields.
    """

    identity_namespace: str = ""
    tenant_id: str = ""
    subject_type: str = ""
    subject_id: str = ""
    actor_type: str = ""
    actor_id: str = ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "PlatformIdentityContext":
        if not isinstance(payload, Mapping):
            return cls()
        return cls(
            identity_namespace=_identity_value(payload.get("identity_namespace")),
            tenant_id=_identity_value(payload.get("tenant_id")),
            subject_type=_identity_value(payload.get("subject_type")),
            subject_id=_identity_value(payload.get("subject_id")),
            actor_type=_identity_value(payload.get("actor_type")),
            actor_id=_identity_value(payload.get("actor_id")),
        )

    @property
    def is_empty(self) -> bool:
        return not any(self.to_payload().values())

    @property
    def is_complete(self) -> bool:
        """Whether the four authorization-scoping fields are all present."""

        return all(self.user_scope)

    @property
    def tenant_scope(self) -> tuple[str, str]:
        return self.identity_namespace, self.tenant_id

    @property
    def user_scope(self) -> tuple[str, str, str, str]:
        return (
            self.identity_namespace,
            self.tenant_id,
            self.subject_type,
            self.subject_id,
        )

    def to_payload(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "identity_namespace": self.identity_namespace,
                "tenant_id": self.tenant_id,
                "subject_type": self.subject_type,
                "subject_id": self.subject_id,
                "actor_type": self.actor_type,
                "actor_id": self.actor_id,
            }.items()
            if value
        }


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
    account_id: str = ""
    identity: PlatformIdentityContext = field(default_factory=PlatformIdentityContext)
    metadata: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    model_options: dict[str, Any] | None = None
    kb_context: dict[str, Any] | None = None
    memory_context: dict[str, Any] | None = None
    # Request-scoped runtime control. It is intentionally separate from
    # public caller metadata so built-in tools can enforce it consistently.
    tool_approval_mode: str = ""
    session: SessionContext = field(default_factory=SessionContext)

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "account_id": self.account_id,
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
            "metadata": dict(self.metadata or {}),
            "model": self.model,
            "model_options": dict(self.model_options or {}),
        }

        if self.session.tags or self.session.revision:
            payload["session"] = self.session.to_payload()
        if not self.identity.is_empty:
            payload["identity"] = self.identity.to_payload()
        return payload


@dataclass
class ToolExecutionContext:
    session_id: str = ""
    run_id: str = ""
    invocation_id: str = ""


_CURRENT_PLATFORM_INVOCATION_CONTEXT: ContextVar[PlatformInvocationContext | None] = ContextVar(
    "ksadk_platform_invocation_context",
    default=None,
)

_CURRENT_TOOL_EXECUTION_CONTEXT: ContextVar[ToolExecutionContext | None] = ContextVar(
    "ksadk_tool_execution_context",
    default=None,
)


def get_current_invocation_context() -> PlatformInvocationContext | None:
    return _CURRENT_PLATFORM_INVOCATION_CONTEXT.get()


def get_current_invocation_context_or_default() -> PlatformInvocationContext:
    context = get_current_invocation_context()
    if context is not None:
        return context
    return PlatformInvocationContext(
        agent_id="",
        user_id="",
        account_id="",
        session_id="",
        history=[],
        input_content=[],
        input_messages=[],
        input_parts=[],
        attachments=[],
        attachment_results=[],
        current_attachments=[],
        current_attachment_results=[],
        has_current_files=False,
        runner_type="",
    )


def get_current_tool_execution_context_or_default() -> ToolExecutionContext:
    context = _CURRENT_TOOL_EXECUTION_CONTEXT.get()
    if context is not None:
        return context
    return ToolExecutionContext()


def get_current_user_id(default: str = "") -> str:
    context = get_current_invocation_context()
    if context is None:
        return default
    return str(context.user_id or default)


def get_current_account_id(default: str = "") -> str:
    context = get_current_invocation_context()
    if context is None:
        return default
    return str(context.account_id or default)


def get_current_identity_context() -> PlatformIdentityContext:
    context = get_current_invocation_context()
    return context.identity if context is not None else PlatformIdentityContext()


def get_current_tenant_id(default: str = "") -> str:
    return str(get_current_identity_context().tenant_id or default)


def get_current_subject_id(default: str = "") -> str:
    return str(get_current_identity_context().subject_id or default)


def set_current_invocation_context(
    context: PlatformInvocationContext | None,
) -> Token[PlatformInvocationContext | None]:
    return _CURRENT_PLATFORM_INVOCATION_CONTEXT.set(context)


def reset_current_invocation_context(
    token: Token[PlatformInvocationContext | None],
) -> None:
    try:
        _CURRENT_PLATFORM_INVOCATION_CONTEXT.reset(token)
    except ValueError:
        # ASGI streaming may resume an async generator in a descendant
        # Context after it yielded an interrupt. Tokens cannot be reset from
        # that different Context; clearing the active descendant avoids
        # converting a successfully persisted approval pause into a failed run.
        _CURRENT_PLATFORM_INVOCATION_CONTEXT.set(None)


def set_current_tool_execution_context(
    context: ToolExecutionContext | None,
) -> Token[ToolExecutionContext | None]:
    return _CURRENT_TOOL_EXECUTION_CONTEXT.set(context)


def reset_current_tool_execution_context(
    token: Token[ToolExecutionContext | None],
) -> None:
    try:
        _CURRENT_TOOL_EXECUTION_CONTEXT.reset(token)
    except ValueError:
        _CURRENT_TOOL_EXECUTION_CONTEXT.set(None)


@contextmanager
def platform_invocation_scope(
    context: PlatformInvocationContext | None,
) -> Iterator[PlatformInvocationContext | None]:
    token = set_current_invocation_context(context)
    try:
        yield context
    finally:
        reset_current_invocation_context(token)


@contextmanager
def tool_execution_scope(
    session_id: str,
    run_id: str | None = None,
    invocation_id: str | None = None,
) -> Iterator[ToolExecutionContext]:
    context = ToolExecutionContext(
        session_id=str(session_id or ""),
        run_id=str(run_id or ""),
        invocation_id=str(invocation_id or ""),
    )
    token = set_current_tool_execution_context(context)
    try:
        yield context
    finally:
        reset_current_tool_execution_context(token)


def get_current_session_context() -> SessionContext:
    """Read the current immutable snapshot in LangChain, LangGraph or ADK code."""
    context = get_current_invocation_context()
    return context.session if context is not None else SessionContext()


def get_current_session_tags() -> Mapping[str, str]:
    """Return read-only business labels; these are not authorization claims."""
    return get_current_session_context().tags


def session_invocation_context(
    snapshot,
    *,
    agent_id="",
    user_id="",
    session_id="",
    runner_type="",
    identity: PlatformIdentityContext | Mapping[str, Any] | None = None,
):
    """Build the narrow context for kernel starts without conversation preprocessing."""
    return PlatformInvocationContext(
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        runner_type=runner_type,
        history=[],
        input_content=[],
        input_messages=[],
        input_parts=[],
        attachments=[],
        attachment_results=[],
        current_attachments=[],
        current_attachment_results=[],
        has_current_files=False,
        session=SessionContext.from_payload(snapshot),
        identity=(
            identity
            if isinstance(identity, PlatformIdentityContext)
            else PlatformIdentityContext.from_payload(identity)
        ),
    )
