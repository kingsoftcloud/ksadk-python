"""Explicit compatibility dependencies shared by extracted route modules.

The default app keeps a few historically monkeypatchable exports. Providers are
callbacks so route modules observe those replacements without importing the app
module back into the domain layer.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator


@dataclass(frozen=True)
class ServerRouteDependencies:
    resolve_session_service: Callable[[], Any]
    describe_session_backend: Callable[[], dict[str, Any]]
    resolve_agent_ui_spec: Callable[[], dict[str, Any]]
    conversation: Callable[[], Any]
    detached_streaming_response: Callable[..., Any]
    detached_stream_class: Callable[[], type[Any]]
    heartbeat_interval: Callable[[], float]
    runtime_app: Callable[[], Any]


_dependencies: ServerRouteDependencies | None = None
_session_service_override: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "ksadk_route_session_service", default=None
)
_session_backend_override: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "ksadk_route_session_backend", default=None
)


def configure(dependencies: ServerRouteDependencies) -> None:
    global _dependencies
    _dependencies = dependencies


def current() -> ServerRouteDependencies:
    if _dependencies is None:
        raise RuntimeError("server route dependencies are not configured")
    return _dependencies


def resolve_session_service() -> Any:
    override = _session_service_override.get()
    if override is not None:
        return override
    return current().resolve_session_service()


@contextmanager
def bind_session_service(service: Any, *, backend: dict[str, Any] | None = None) -> Iterator[None]:
    """Bind an app-owned session service for one request context."""
    from ksadk.sessions import bind_session_service as bind_runtime_session_service

    service_token = _session_service_override.set(service)
    backend_token = _session_backend_override.set(backend)
    try:
        with bind_runtime_session_service(service):
            yield
    finally:
        _session_backend_override.reset(backend_token)
        _session_service_override.reset(service_token)


def describe_session_backend() -> dict[str, Any]:
    override = _session_backend_override.get()
    if override is not None:
        return dict(override)
    return current().describe_session_backend()


def resolve_agent_ui_spec() -> dict[str, Any]:
    return current().resolve_agent_ui_spec()


def conversation() -> Any:
    return current().conversation()


def detached_streaming_response(*args: Any, **kwargs: Any) -> Any:
    return current().detached_streaming_response(*args, **kwargs)


def detached_stream_class() -> type[Any]:
    return current().detached_stream_class()


def heartbeat_interval() -> float:
    return current().heartbeat_interval()


def runtime_app() -> Any:
    return current().runtime_app()
