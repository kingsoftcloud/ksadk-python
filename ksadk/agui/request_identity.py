"""Request-scoped identity supplied by the Runtime's private HTTP carrier."""

from contextvars import ContextVar

from ksadk.runtime_context import PlatformIdentityContext

agui_request_identity: ContextVar[PlatformIdentityContext | None] = ContextVar(
    "agui_request_identity", default=None
)

agui_admission: ContextVar[dict | None] = ContextVar("agui_admission", default=None)
